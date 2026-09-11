"""Bootstrap Acquisition executor (V9 Phase 2.5 §10, §11, §16-20, §27-29).

Runs a bounded slice of one job's claimable tasks:

    claim -> fetch (source-type dispatched) -> parse/validate
          -> the existing, unmodified Phase 2 import boundary
             (market_prior_import.normalize + persistence.market_priors.upsert_priors)
          -> mark the task result
          -> repeat, until the run's own request budget is spent, the job has
             no more claimable tasks, or a stop condition fires.

**Idempotency (§27).** A record is written to ``market_priors`` — an
UPSERT-by-market-key, never an increment — *before* its task is marked
terminal. If the process dies in between, the task's lease simply expires and
a later run re-claims it (§19); re-running ``fetch_one`` and re-normalizing
the same market/bucket only ever replaces the same row, never doubles a
sample count. A task already marked terminal is never reclaimed at all, so
the ordinary (non-crash) path never imports the same observation twice
either.

**Fail closed mid-run (§20).** Authorization is re-checked before *every*
claim, not just once at the start — a source revoked while a job is running
must stop the next task, not just the next job.

**Hard budget under concurrency, and under retries (§16, §17, §59).**
``requests_used`` is reserved atomically
(``persistence.market_prior_acquisition.reserve_request`` — one
``UPDATE ... WHERE requests_used < request_budget``) rather than
read-then-written-back across separate statements — this closes a QA-caught
race where two concurrent ``run_job_slice`` calls against the same job could
each pass a stale check and both increment, driving the total past the
configured ceiling.

For a ``FILE_IMPORT``/``MANUAL_DATASET`` source (no internal HTTP retry
layer) that reservation happens once per claimed task, here, before
``fetch_one`` is called — one task is one real "request" by construction. For
an ``AUTHORIZED_WEB_SOURCE``/``API_SOURCE``, reserving once per *task* would
be wrong in the other direction: ``fetch_one`` can cause several real HTTP
attempts via :class:`~detoura.providers.http.RetryingHttpClient`'s retry
loop, and each one is its own real request. For those, the reservation
happens once per *raw attempt*, inside the HTTP transport itself
(``network_adapter._budget_gated_client`` — see
:class:`~detoura.services.network_adapter.AuthorizedHttpFetcher``'s
``db``/``job_id`` wiring), and this module never reserves for them at all —
doing both would double-charge. ``result.requests_used`` is read back as a
delta on the job's persisted counter at the end of the slice, so it is
correct regardless of which layer did the charging, and a mid-retry budget
exhaustion (:class:`~detoura.services.network_adapter.BudgetExhausted`)
releases the task's claim back to ``PENDING`` rather than losing or
double-counting it.

Source-wide rate limiting for network-capable sources is likewise persisted
(``reserve_rate_limit_slot``), not an in-process lock — correct across every
thread, job *and OS process* sharing the database file, which an in-memory
lock could never be.

**Search independence (§28).** This module is reachable only from
Ops-triggered job control; nothing in the search/booking path imports it.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from ..models.market_prior import HorizonBucket
from ..models.market_prior_acquisition import SourceRegistration, StopReason, TaskStatus
from ..persistence import market_prior_acquisition as store
from ..persistence import market_priors as prior_store
from ..persistence.db import Database
from .bootstrap_parser import SchemaChanged, TaskContext, validate_record_matches_task
from .market_prior_import import PriorImportError, normalize
from .network_adapter import AccessBlocked, BudgetExhausted


class SourceFetcher(Protocol):
    """Produces a raw prior record for one task, or raises.

    Source-type specific: a ``FILE_IMPORT``/``MANUAL_DATASET`` fetcher reads
    from a pre-loaded dataset (never touches the network); an
    ``AUTHORIZED_WEB_SOURCE``/``API_SOURCE`` fetcher goes through
    :class:`~detoura.services.network_adapter.AuthorizedHttpFetcher`. Both
    shapes satisfy this same protocol so the executor never branches on
    source type itself (§7).
    """

    parser_version: str

    def fetch_one(self, task: TaskContext) -> dict | None:
        """``None`` = the source answered but has no fare for this cell
        (NO_DATA — a real, honest answer). Raise :class:`AccessBlocked` or
        :class:`SchemaChanged` to signal a stop condition;
        :class:`BudgetExhausted` when the request budget ran out mid-retry;
        any other exception is treated as one task's retryable failure."""
        ...


@dataclass
class ExecutionResult:
    tasks_attempted: int = 0
    succeeded: int = 0
    no_data: int = 0
    failed: int = 0
    blocked: int = 0
    requests_used: int = 0
    rows_imported: int = 0
    rows_updated: int = 0
    stopped_reason: str = ""
    job_completed: bool = False

    def as_dict(self) -> dict:
        return {
            "tasks_attempted": self.tasks_attempted, "succeeded": self.succeeded,
            "no_data": self.no_data, "failed": self.failed, "blocked": self.blocked,
            "requests_used": self.requests_used, "rows_imported": self.rows_imported,
            "rows_updated": self.rows_updated, "stopped_reason": self.stopped_reason,
            "job_completed": self.job_completed,
        }


def _is_network_capable_and_blocked(db: Database, source: SourceRegistration) -> str | None:
    """Re-reads the source's *current* authorization from persistence (not
    the possibly-stale ``source`` object the caller was constructed with) —
    the exact check that makes "authorization revoked mid-job" stop the very
    next task rather than only the next job invocation."""
    if not source.is_network_capable:
        return None
    current = store.get_source(db, source.source_id)
    if current is None or not current.network_allowed:
        return StopReason.AUTHORIZATION_DISABLED.value
    return None


def run_job_slice(
    db: Database, job_id: str, *, fetcher: SourceFetcher, source: SourceRegistration,
    max_tasks: int, lease_owner: str | None = None, lease_seconds: int = 120,
    now: datetime | None = None,
) -> ExecutionResult:
    now = now or datetime.now(timezone.utc)
    # A fresh, unique token per *invocation* when the caller does not supply
    # one - never a fixed literal like "executor". Two concurrent slices
    # (two threads, two processes, two Ops "start" clicks) must never share
    # a lease_owner, or mark_task_result/release_task_claim's lease-scoped
    # updates (§19, §59) could not tell them apart and the exact race they
    # exist to close would reopen.
    lease_owner = lease_owner or f"executor-{secrets.token_hex(8)}"
    result = ExecutionResult()

    job = store.get_job(db, job_id)
    if job is None:
        raise ValueError(f"no such job: {job_id}")
    if job["status"] in ("CANCELLED", "COMPLETED"):
        result.stopped_reason = f"job already {job['status']}"
        return result

    blocked_reason = _is_network_capable_and_blocked(db, source)
    if blocked_reason:
        store.set_job_status(db, job_id, "PAUSED", stopped_reason=blocked_reason, now=now)
        result.stopped_reason = blocked_reason
        return result

    store.set_job_status(db, job_id, "RUNNING", now=now)
    requests_used_start = job["requests_used"]

    for _ in range(max(max_tasks, 0)):
        blocked_reason = _is_network_capable_and_blocked(db, source)
        if blocked_reason:
            store.set_job_status(db, job_id, "PAUSED", stopped_reason=blocked_reason, now=now)
            result.stopped_reason = blocked_reason
            break

        reserved_here = False
        if not source.is_network_capable:
            # No internal HTTP retry layer for these - one reservation per
            # task IS one real request. A network-capable source reserves
            # per raw HTTP attempt instead, inside the transport itself
            # (see module docstring) - reserving again here would
            # double-charge every retry.
            if not store.reserve_request(db, job_id, now=now):
                result.stopped_reason = result.stopped_reason or "request_budget_exhausted"
                break
            reserved_here = True
        else:
            current_job = store.get_job(db, job_id)
            if current_job["requests_used"] >= current_job["request_budget"]:
                result.stopped_reason = result.stopped_reason or "request_budget_exhausted"
                break

        task = store.claim_next_task(db, job_id, lease_owner=lease_owner, lease_seconds=lease_seconds, now=now)
        if task is None:
            if reserved_here:
                store.release_request(db, job_id, now=now)  # reserved but nothing to spend it on
            break

        result.tasks_attempted += 1
        ctx = TaskContext(
            origin=task["origin"], destination=task["destination"],
            horizon_days=HorizonBucket(task["horizon_bucket"]).days or 0,
        )

        try:
            record = fetcher.fetch_one(ctx)
        except BudgetExhausted:
            # Discovered deep inside one logical fetch_one() call (a retry
            # attempt ran out of budget) - the claim is given back, not
            # lost or double-counted, since no result was ever produced
            # for it (§16, §27).
            store.release_task_claim(db, task["task_id"], lease_owner=lease_owner, now=now)
            result.stopped_reason = result.stopped_reason or "request_budget_exhausted"
            break
        except AccessBlocked as exc:
            store.mark_task_result(db, task["task_id"], status=TaskStatus.BLOCKED,
                                   failure_reason=str(exc), lease_owner=lease_owner, now=now)
            result.blocked += 1
            result.stopped_reason = exc.reason.value
            store.set_job_status(db, job_id, "PAUSED", stopped_reason=exc.reason.value, now=now)
            break
        except SchemaChanged as exc:
            store.mark_task_result(db, task["task_id"], status=TaskStatus.SOURCE_CHANGED,
                                   failure_reason=str(exc), parser_version=fetcher.parser_version,
                                   lease_owner=lease_owner, now=now)
            result.blocked += 1
            result.stopped_reason = StopReason.SOURCE_CHANGED.value
            store.set_job_status(db, job_id, "PAUSED", stopped_reason=StopReason.SOURCE_CHANGED.value, now=now)
            break
        except Exception as exc:  # noqa: BLE001 - one task's problem, not the run's
            store.mark_task_result(db, task["task_id"], status=TaskStatus.RETRYABLE_FAILURE,
                                   failure_reason=str(exc)[:500], lease_owner=lease_owner, now=now)
            result.failed += 1
            continue

        if record is None:
            store.mark_task_result(db, task["task_id"], status=TaskStatus.NO_DATA,
                                   parser_version=fetcher.parser_version, lease_owner=lease_owner, now=now)
            result.no_data += 1
            continue

        try:
            validate_record_matches_task(record, task=ctx)
            prior = normalize(
                record, source=source.source_id, source_version=source.adapter_version,
                imported_at=now, default_source_date=None,
            )
        except (SchemaChanged, PriorImportError) as exc:
            status = TaskStatus.SOURCE_CHANGED if isinstance(exc, SchemaChanged) else TaskStatus.FAILED
            store.mark_task_result(db, task["task_id"], status=status, failure_reason=str(exc),
                                   parser_version=fetcher.parser_version, lease_owner=lease_owner, now=now)
            if status is TaskStatus.SOURCE_CHANGED:
                result.blocked += 1
                result.stopped_reason = StopReason.SOURCE_CHANGED.value
                store.set_job_status(db, job_id, "PAUSED", stopped_reason=StopReason.SOURCE_CHANGED.value, now=now)
                break
            result.failed += 1
            continue

        # Write the prior BEFORE marking the task terminal (§27): a crash
        # between these two lines just means a stale-lease resume re-does an
        # idempotent upsert, never a lost result and never a duplicate one.
        ins, upd = prior_store.upsert_priors(db, [prior])
        result.rows_imported += ins
        result.rows_updated += upd
        store.mark_task_result(db, task["task_id"], status=TaskStatus.SUCCEEDED,
                               parser_version=fetcher.parser_version, lease_owner=lease_owner, now=now)
        result.succeeded += 1

    # A stop condition (AccessBlocked / SchemaChanged / authorization
    # revoked / budget exhaustion) already set the job to PAUSED (or, for
    # budget exhaustion, just recorded stopped_reason) above; a plain
    # nothing-left-to-claim slice end leaves the job RUNNING, since it needs
    # no operator decision - the next slice invocation simply resumes it
    # (§18).
    counters = store.job_counters(db, job_id)
    current = store.get_job(db, job_id)
    result.requests_used = current["requests_used"] - requests_used_start
    if counters["remaining"] == 0 and current["status"] not in ("PAUSED", "CANCELLED"):
        store.set_job_status(db, job_id, "COMPLETED", now=now)
        result.job_completed = True

    return result
