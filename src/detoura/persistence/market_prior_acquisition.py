"""Persistence for Authorized Market-Prior Acquisition (V9 Phase 2.5).

Three tables, all upstream of ``market_priors``: a source's authorization
record, a bounded job, and its individual bounded tasks. Nothing here writes
``market_priors`` directly — a task's successful result is handed to the
existing :mod:`detoura.services.market_prior_import` boundary, unchanged from
Phase 2.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Sequence

from ..models.market_prior import HorizonBucket
from ..models.market_prior_acquisition import (
    RETRYABLE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    RateLimitPolicy,
    SourceRegistration,
    TaskCell,
    TaskStatus,
)
from .db import Database


def new_job_id() -> str:
    return "job_" + secrets.token_urlsafe(12)


def new_task_id() -> str:
    return "task_" + secrets.token_urlsafe(10)


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


# ======================================================================
# Sources — authorization records
# ======================================================================
def upsert_source(db: Database, reg: SourceRegistration) -> None:
    row = (
        reg.source_id, reg.source_name, reg.source_type.value,
        reg.authorization_status.value, reg.authorization_basis,
        reg.allowed_scope, reg.commercial_reuse_status,
        int(reg.persistence_allowed), reg.base_domain,
        reg.rate_limit_policy.model_dump_json(), reg.adapter_version,
        _iso(reg.reviewed_at), reg.reviewed_by, reg.notes,
        _iso(reg.robots_checked_at),
        None if reg.robots_allowed is None else int(reg.robots_allowed),
        reg.request_cost_minor, reg.created_at.isoformat(), reg.updated_at.isoformat(),
    )
    with db.write() as conn:
        conn.execute(
            "INSERT INTO market_prior_sources (source_id, source_name, source_type,"
            " authorization_status, authorization_basis, allowed_scope,"
            " commercial_reuse_status, persistence_allowed, base_domain,"
            " rate_limit_json, adapter_version, reviewed_at, reviewed_by, notes,"
            " robots_checked_at, robots_allowed, request_cost_minor, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source_id) DO UPDATE SET"
            " source_name=excluded.source_name, source_type=excluded.source_type,"
            " authorization_status=excluded.authorization_status,"
            " authorization_basis=excluded.authorization_basis,"
            " allowed_scope=excluded.allowed_scope,"
            " commercial_reuse_status=excluded.commercial_reuse_status,"
            " persistence_allowed=excluded.persistence_allowed,"
            " base_domain=excluded.base_domain, rate_limit_json=excluded.rate_limit_json,"
            " adapter_version=excluded.adapter_version, reviewed_at=excluded.reviewed_at,"
            " reviewed_by=excluded.reviewed_by, notes=excluded.notes,"
            " robots_checked_at=excluded.robots_checked_at,"
            " robots_allowed=excluded.robots_allowed,"
            " request_cost_minor=excluded.request_cost_minor,"
            " updated_at=excluded.updated_at",
            row,
        )


def _source_from_row(r) -> SourceRegistration:
    return SourceRegistration(
        source_id=r["source_id"], source_name=r["source_name"],
        source_type=r["source_type"], authorization_status=r["authorization_status"],
        authorization_basis=r["authorization_basis"], allowed_scope=r["allowed_scope"],
        commercial_reuse_status=r["commercial_reuse_status"],
        persistence_allowed=bool(r["persistence_allowed"]), base_domain=r["base_domain"],
        rate_limit_policy=RateLimitPolicy(**json.loads(r["rate_limit_json"] or "{}")),
        adapter_version=r["adapter_version"],
        reviewed_at=r["reviewed_at"] and datetime.fromisoformat(r["reviewed_at"]),
        reviewed_by=r["reviewed_by"], notes=r["notes"],
        robots_checked_at=r["robots_checked_at"] and datetime.fromisoformat(r["robots_checked_at"]),
        robots_allowed=None if r["robots_allowed"] is None else bool(r["robots_allowed"]),
        request_cost_minor=r["request_cost_minor"],
        created_at=datetime.fromisoformat(r["created_at"]),
        updated_at=datetime.fromisoformat(r["updated_at"]),
    )


def get_source(db: Database, source_id: str) -> SourceRegistration | None:
    row = db.query_one("SELECT * FROM market_prior_sources WHERE source_id = ?", (source_id,))
    return _source_from_row(row) if row else None


def list_sources(db: Database) -> list[SourceRegistration]:
    return [_source_from_row(r) for r in db.query(
        "SELECT * FROM market_prior_sources ORDER BY source_id"
    )]


def set_authorization(
    db: Database, source_id: str, *, status, basis: str = "",
    reviewed_by: str = "", now: datetime | None = None,
) -> bool:
    """Record an authorization decision. Returns ``False`` if the source does
    not exist. The software records the decision; it does not conclude the
    decision is legally correct (§4)."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE market_prior_sources SET authorization_status=?,"
            " authorization_basis=?, reviewed_by=?, reviewed_at=?, updated_at=?"
            " WHERE source_id=?",
            (status.value if hasattr(status, "value") else status, basis,
             reviewed_by, ts, ts, source_id),
        )
        return cur.rowcount > 0


def set_robots_check(db: Database, source_id: str, *, allowed: bool | None, now: datetime | None = None) -> None:
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        conn.execute(
            "UPDATE market_prior_sources SET robots_allowed=?, robots_checked_at=?,"
            " updated_at=? WHERE source_id=?",
            (None if allowed is None else int(allowed), ts, ts, source_id),
        )


def reserve_rate_limit_slot(
    db: Database, source_id: str, *, min_interval_seconds: float, now: datetime | None = None,
) -> float:
    """Atomically reserves the next available request slot for ``source_id``
    and returns how many seconds the caller must sleep before actually
    making the request (``0.0`` = go now).

    Correct across every thread, job, *and OS process* sharing this database
    file — not just within one Python process. The read of the source's
    current ``next_allowed_at``, the decision, and the write of the new
    value all happen inside one :meth:`Database.write` transaction: within a
    process that is one lock acquisition (:class:`Database` serialises every
    caller on ``self._lock`` already); across processes it is one SQLite
    write transaction, and SQLite's own file-level locking (plus the
    ``PRAGMA busy_timeout`` already configured in :class:`Database`) means a
    second process's transaction simply waits for the first to commit rather
    than racing it — the same guarantee :func:`reserve_request` relies on for
    the hard request budget (§16, §17, §59).
    """
    now = now or datetime.now(timezone.utc)
    with db.write() as conn:
        row = conn.execute(
            "SELECT next_allowed_at FROM market_prior_sources WHERE source_id=?", (source_id,),
        ).fetchone()
        next_allowed = now
        if row is not None and row["next_allowed_at"]:
            try:
                next_allowed = max(now, datetime.fromisoformat(row["next_allowed_at"]))
            except ValueError:
                next_allowed = now
        new_next_allowed = next_allowed + timedelta(seconds=max(min_interval_seconds, 0.0))
        conn.execute(
            "UPDATE market_prior_sources SET next_allowed_at=?, updated_at=? WHERE source_id=?",
            (new_next_allowed.isoformat(), now.isoformat(), source_id),
        )
    return max(0.0, (next_allowed - now).total_seconds())


# ======================================================================
# Jobs
# ======================================================================
def create_job(
    db: Database, *, source_id: str, origins: Sequence[str], destinations: Sequence[str],
    horizon_days: Sequence[int], request_budget: int, dry_run: bool,
    now: datetime | None = None,
) -> str:
    job_id = new_job_id()
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO market_prior_jobs (job_id, source_id, created_at, updated_at,"
            " status, origin_scope_json, destination_scope_json, horizon_scope_json,"
            " request_budget, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job_id, source_id, ts, ts, "PLANNED",
             json.dumps(sorted(set(origins))), json.dumps(sorted(set(destinations))),
             json.dumps(sorted(set(horizon_days))), int(request_budget), int(dry_run)),
        )
    return job_id


def _job_row(r) -> dict:
    d = dict(r)
    d["origin_scope"] = json.loads(d.pop("origin_scope_json"))
    d["destination_scope"] = json.loads(d.pop("destination_scope_json"))
    d["horizon_scope"] = json.loads(d.pop("horizon_scope_json"))
    d["dry_run"] = bool(d["dry_run"])
    return d


def get_job(db: Database, job_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM market_prior_jobs WHERE job_id = ?", (job_id,))
    return _job_row(row) if row else None


def list_jobs(db: Database, *, source_id: str | None = None, limit: int = 50) -> list[dict]:
    if source_id:
        rows = db.query(
            "SELECT * FROM market_prior_jobs WHERE source_id=? ORDER BY created_at DESC LIMIT ?",
            (source_id, max(1, min(limit, 500))),
        )
    else:
        rows = db.query(
            "SELECT * FROM market_prior_jobs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        )
    return [_job_row(r) for r in rows]


def set_job_status(db: Database, job_id: str, status, *, stopped_reason: str = "",
                    now: datetime | None = None) -> None:
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        conn.execute(
            "UPDATE market_prior_jobs SET status=?, stopped_reason=?, updated_at=?"
            " WHERE job_id=?",
            (status.value if hasattr(status, "value") else status, stopped_reason, ts, job_id),
        )


def _bump_job(conn, job_id: str, **deltas: int) -> None:
    sets = ", ".join(f"{k} = {k} + ?" for k in deltas)
    conn.execute(
        f"UPDATE market_prior_jobs SET {sets}, updated_at=? WHERE job_id=?",
        (*deltas.values(), datetime.now(timezone.utc).isoformat(), job_id),
    )


def reserve_request(db: Database, job_id: str, *, now: datetime | None = None) -> bool:
    """Atomically reserves one unit of the job's hard request budget.

    This is the *single* choke point that makes ``request_budget`` genuinely
    hard even under concurrent executor invocations against the same job
    (§16, §59) — a QA-caught gap: reading ``requests_used`` and later writing
    it back in separate statements left a check-then-act race where two
    concurrent ``run_job_slice`` calls could each pass a stale check and both
    increment, driving the total past the configured ceiling. The check and
    the increment are now one ``UPDATE ... WHERE requests_used < request_budget``
    statement, which SQLite (behind :class:`Database`'s single serialised
    connection) can never apply twice past the limit — a second caller's
    ``UPDATE`` simply matches zero rows once the first has committed.

    Returns ``False`` — no request was reserved — once the budget is spent;
    the caller must not fetch or claim anything in that case."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE market_prior_jobs SET requests_used = requests_used + 1, updated_at=?"
            " WHERE job_id=? AND requests_used < request_budget",
            (ts, job_id),
        )
        return cur.rowcount == 1


def release_request(db: Database, job_id: str, *, now: datetime | None = None) -> None:
    """Gives back a reservation that turned out not to be used (no task was
    claimable, or the source was found blocked before any fetch was
    attempted) — keeps ``requests_used`` an honest count of requests actually
    made, not merely attempted."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        conn.execute(
            "UPDATE market_prior_jobs SET requests_used = MAX(0, requests_used - 1),"
            " updated_at=? WHERE job_id=?",
            (ts, job_id),
        )


# ======================================================================
# Tasks — planning, claiming, resolving
# ======================================================================
def plan_tasks(
    db: Database, *, job_id: str, source_id: str, cells: Sequence[TaskCell],
    now: datetime | None = None,
) -> dict:
    """Insert one PENDING task per cell, deduplicated by the job's own
    ``(origin, destination, horizon_bucket)`` UNIQUE key — planning the same
    job twice (or resuming after a crash) never doubles the task set."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    inserted = 0
    with db.write() as conn:
        for cell in cells:
            bucket = HorizonBucket.for_days(cell.horizon_days).value
            cur = conn.execute(
                "INSERT OR IGNORE INTO market_prior_tasks (task_id, job_id, source_id,"
                " origin, destination, horizon_bucket, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (new_task_id(), job_id, source_id, cell.origin, cell.destination,
                 bucket, TaskStatus.PENDING.value, ts),
            )
            inserted += cur.rowcount
        deduped = len(cells) - inserted
        _bump_job(conn, job_id, planned=inserted, deduplicated=deduped)
    return {"planned": inserted, "deduplicated": deduped, "cells_considered": len(cells)}


def job_counters(db: Database, job_id: str) -> dict:
    rows = db.query(
        "SELECT status, COUNT(*) AS n FROM market_prior_tasks WHERE job_id=? GROUP BY status",
        (job_id,),
    )
    by_status = {r["status"]: r["n"] for r in rows}
    total = sum(by_status.values())
    terminal = sum(by_status.get(s.value, 0) for s in TERMINAL_TASK_STATUSES)
    return {
        "total_tasks": total,
        "by_status": by_status,
        "succeeded": by_status.get("SUCCEEDED", 0),
        "no_data": by_status.get("NO_DATA", 0),
        "failed": by_status.get("FAILED", 0),
        "blocked": by_status.get("BLOCKED", 0),
        "cancelled": by_status.get("CANCELLED", 0),
        "pending": by_status.get("PENDING", 0),
        "running": by_status.get("RUNNING", 0),
        "remaining": total - terminal,
    }


def list_tasks(db: Database, job_id: str, *, status: str | None = None, limit: int = 500) -> list[dict]:
    if status:
        rows = db.query(
            "SELECT * FROM market_prior_tasks WHERE job_id=? AND status=? ORDER BY task_id LIMIT ?",
            (job_id, status, max(1, min(limit, 2000))),
        )
    else:
        rows = db.query(
            "SELECT * FROM market_prior_tasks WHERE job_id=? ORDER BY task_id LIMIT ?",
            (job_id, max(1, min(limit, 2000))),
        )
    return [dict(r) for r in rows]


def claim_next_task(
    db: Database, job_id: str, *, lease_owner: str, lease_seconds: int = 120,
    now: datetime | None = None,
) -> dict | None:
    """Atomically claim one task: a fresh ``PENDING`` row, or a ``RUNNING``
    row whose lease has expired (a prior worker died mid-task — §19).

    Uses the same select-then-conditional-UPDATE-with-rowcount-guard pattern
    as the existing Ops ticket-claim code: a lost race just means the caller
    tries the next candidate, never double-processes a task."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
    candidates = db.query(
        "SELECT task_id, status FROM market_prior_tasks WHERE job_id=?"
        " AND (status='PENDING' OR (status='RUNNING' AND (lease_expires_at IS NULL"
        " OR lease_expires_at < ?))) ORDER BY task_id LIMIT 20",
        (job_id, now_iso),
    )
    for c in candidates:
        with db.write() as conn:
            cur = conn.execute(
                "UPDATE market_prior_tasks SET status='RUNNING', lease_owner=?,"
                " lease_expires_at=?, last_attempt_at=?, attempt_count = attempt_count + 1"
                " WHERE task_id=? AND status=?",
                (lease_owner, lease_until, now_iso, c["task_id"], c["status"]),
            )
            if cur.rowcount == 1:
                row = db.query_one("SELECT * FROM market_prior_tasks WHERE task_id=?", (c["task_id"],))
                return dict(row)
    return None


def mark_task_result(
    db: Database, task_id: str, *, status: TaskStatus, failure_reason: str = "",
    parser_version: str = "", lease_owner: str | None = None, now: datetime | None = None,
) -> bool:
    """Records a task's terminal (or retryable-failure) outcome.

    When ``lease_owner`` is given, the update is scoped to
    ``WHERE task_id=? AND lease_owner=?`` — not just ``status='RUNNING'`` —
    so a worker whose lease has since expired and been reclaimed by a
    *different* worker (§19) can never stomp on that other worker's
    in-progress claim; its (stale) call simply matches zero rows and is a
    no-op. Returns whether the update actually applied. Callers that pass no
    ``lease_owner`` (direct/test use) get the previous, unscoped behaviour."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    completed = ts if status in TERMINAL_TASK_STATUSES else None
    with db.write() as conn:
        if lease_owner is not None:
            cur = conn.execute(
                "UPDATE market_prior_tasks SET status=?, failure_reason=?, parser_version=?,"
                " completed_at=?, lease_owner='', lease_expires_at=NULL"
                " WHERE task_id=? AND lease_owner=?",
                (status.value, failure_reason, parser_version, completed, task_id, lease_owner),
            )
        else:
            cur = conn.execute(
                "UPDATE market_prior_tasks SET status=?, failure_reason=?, parser_version=?,"
                " completed_at=?, lease_owner='', lease_expires_at=NULL WHERE task_id=?",
                (status.value, failure_reason, parser_version, completed, task_id),
            )
        # Per-status counts (succeeded/no_data/failed/blocked/cancelled) are
        # derived live from market_prior_tasks by job_counters() rather than
        # duplicated as mutable job-row counters - one source of truth, no
        # risk of the two drifting apart. requests_used is reserved
        # atomically by reserve_request() *before* a task is even claimed
        # (§16, §59) - not bumped again here - so a QA-caught check-then-act
        # race between reading the budget and later recording an attempt
        # cannot let concurrent executor invocations spend more than the
        # configured ceiling.
        return cur.rowcount == 1


def release_task_claim(db: Database, task_id: str, *, lease_owner: str | None = None,
                       now: datetime | None = None) -> bool:
    """Puts a ``RUNNING`` task back to ``PENDING`` and clears its lease,
    without recording any result — used when a claimed task could not be
    attempted at all because the request budget ran out mid-fetch (a
    network-capable source's retry loop can discover this deep inside one
    logical ``fetch_one`` call, after the task was already claimed). The next
    slice (once budget is available again) simply re-claims it; nothing about
    the attempt is lost or double-counted, because no request was actually
    made for it (§16, §27, §59).

    ``lease_owner``, when given, additionally scopes the update to the exact
    worker that holds the claim — the same stale-worker protection as
    :func:`mark_task_result` (§19, §59: a caught QA hardening). Returns
    whether the release actually applied."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        if lease_owner is not None:
            cur = conn.execute(
                "UPDATE market_prior_tasks SET status='PENDING', lease_owner='',"
                " lease_expires_at=NULL, last_attempt_at=?"
                " WHERE task_id=? AND status='RUNNING' AND lease_owner=?",
                (ts, task_id, lease_owner),
            )
        else:
            cur = conn.execute(
                "UPDATE market_prior_tasks SET status='PENDING', lease_owner='',"
                " lease_expires_at=NULL, last_attempt_at=? WHERE task_id=? AND status='RUNNING'",
                (ts, task_id),
            )
        return cur.rowcount == 1


def cancel_pending(db: Database, job_id: str, *, now: datetime | None = None) -> int:
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE market_prior_tasks SET status='CANCELLED', completed_at=?"
            " WHERE job_id=? AND status IN ('PENDING')",
            (ts, job_id),
        )
        return cur.rowcount


# ======================================================================
# Dedup — what does the current Bootstrap Market Prior already, freshly, cover?
# ======================================================================
def already_fresh_cells(
    db: Database, *, origin_airports: Sequence[str], destination_airports: Sequence[str],
    horizon_days: Sequence[int], freshness_days: int, now: datetime | None = None,
) -> set[tuple[str, str, int]]:
    """``(origin, destination, horizon_days)`` cells the Bootstrap Market
    Prior already has fresh coverage for, from *any* source — planning a new
    acquisition run should not re-spend a request re-discovering what is
    already known and current."""
    if not origin_airports or not destination_airports:
        return set()
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=freshness_days)).date().isoformat()
    o_marks = ",".join("?" for _ in origin_airports)
    d_marks = ",".join("?" for _ in destination_airports)
    rows = db.query(
        f"SELECT DISTINCT origin_airport, destination_airport, horizon_bucket"
        f" FROM market_priors WHERE origin_airport IN ({o_marks})"
        f" AND destination_airport IN ({d_marks})"
        f" AND COALESCE(source_date, substr(imported_at,1,10)) >= ?",
        (*[a.upper() for a in origin_airports], *[a.upper() for a in destination_airports], cutoff),
    )
    covered_buckets = {(r["origin_airport"], r["destination_airport"], r["horizon_bucket"]) for r in rows}
    out: set[tuple[str, str, int]] = set()
    for h in horizon_days:
        bucket = HorizonBucket.for_days(h).value
        for o in origin_airports:
            for d in destination_airports:
                if (o.upper(), d.upper(), bucket) in covered_buckets:
                    out.add((o.upper(), d.upper(), h))
    return out
