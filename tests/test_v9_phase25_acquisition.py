"""V9 Phase 2.5 — Authorized Market-Prior Acquisition: domain, planner,
persistence, executor (§4, §10-19, §21-28, §33, §59)."""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from detoura.acquisition_config import AcquisitionConfig
from detoura.models.market_prior_acquisition import (
    AuthorizationStatus,
    RateLimitPolicy,
    SourceRegistration,
    SourceType,
    TaskCell,
    TaskStatus,
)
from detoura.persistence import market_prior_acquisition as store
from detoura.persistence import market_priors as prior_store
from detoura.persistence.db import Database
from detoura.services.bootstrap_executor import ExecutionResult, run_job_slice
from detoura.services.bootstrap_fetchers import FixtureSourceFetcher
from detoura.services.bootstrap_planner import DEFAULT_DEDUP_FRESHNESS_DAYS, dry_run, plan_cells
from detoura.services.network_adapter import AccessBlocked

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _reg(**kw) -> SourceRegistration:
    base = dict(source_id="s1", source_name="Source One", source_type=SourceType.FILE_IMPORT,
                authorization_status=AuthorizationStatus.APPROVED, created_at=NOW, updated_at=NOW)
    base.update(kw)
    return SourceRegistration(**base)


def _db() -> Database:
    return Database(":memory:")


# ======================================================================
# §4 — authorization fail-closed
# ======================================================================
@pytest.mark.parametrize("status", [
    AuthorizationStatus.REVIEW_REQUIRED, AuthorizationStatus.PROHIBITED, AuthorizationStatus.DISABLED,
])
def test_only_approved_may_network_web_source(status):
    reg = _reg(source_type=SourceType.AUTHORIZED_WEB_SOURCE, base_domain="example.com",
               authorization_status=status)
    assert reg.network_allowed is False


def test_approved_web_source_with_domain_is_network_allowed():
    reg = _reg(source_type=SourceType.AUTHORIZED_WEB_SOURCE, base_domain="example.com",
               authorization_status=AuthorizationStatus.APPROVED)
    assert reg.network_allowed is True


def test_approved_web_source_without_domain_is_still_blocked():
    reg = _reg(source_type=SourceType.AUTHORIZED_WEB_SOURCE, base_domain=None,
               authorization_status=AuthorizationStatus.APPROVED)
    assert reg.network_allowed is False


def test_default_authorization_is_review_required():
    reg = SourceRegistration(source_id="s2", source_name="S2", source_type=SourceType.API_SOURCE,
                             base_domain="api.example.com", created_at=NOW, updated_at=NOW)
    assert reg.authorization_status is AuthorizationStatus.REVIEW_REQUIRED
    assert reg.network_allowed is False


def test_robots_disallowed_blocks_even_if_approved():
    reg = _reg(source_type=SourceType.AUTHORIZED_WEB_SOURCE, base_domain="example.com",
               authorization_status=AuthorizationStatus.APPROVED, robots_allowed=False)
    assert reg.network_allowed is False


def test_file_import_and_manual_dataset_are_never_network_capable():
    for st in (SourceType.FILE_IMPORT, SourceType.MANUAL_DATASET):
        reg = _reg(source_type=st, authorization_status=AuthorizationStatus.APPROVED, base_domain=None)
        assert reg.is_network_capable is False
        assert reg.network_allowed is True  # approved + not network-capable = fine to *use* (never touches network)


def test_source_authorization_persists_and_roundtrips():
    d = _db()
    reg = _reg(source_type=SourceType.AUTHORIZED_WEB_SOURCE, base_domain="example.com",
               authorization_status=AuthorizationStatus.REVIEW_REQUIRED,
               rate_limit_policy=RateLimitPolicy(requests_per_minute=12, max_concurrency=2))
    store.upsert_source(d, reg)
    got = store.get_source(d, "s1")
    assert got.network_allowed is False
    ok = store.set_authorization(d, "s1", status=AuthorizationStatus.APPROVED, basis="license on file",
                                 reviewed_by="ops", now=NOW)
    assert ok is True
    got2 = store.get_source(d, "s1")
    assert got2.network_allowed is True
    assert got2.authorization_basis == "license on file"
    assert got2.rate_limit_policy.requests_per_minute == 12


def test_set_authorization_on_missing_source_returns_false():
    d = _db()
    assert store.set_authorization(d, "ghost", status=AuthorizationStatus.APPROVED) is False


# ======================================================================
# §12, §13 — sparse planner, cheap at scale
# ======================================================================
def test_plan_cells_is_sparse_not_a_dense_date_matrix():
    cells = plan_cells(["LHR"], ["BCN", "MAD"], [30, 60])
    assert len(cells) == 4  # 1 origin x 2 dest x 2 horizons, not x 365 days
    assert all(isinstance(c, TaskCell) for c in cells)


def test_plan_cells_excludes_origin_equals_destination():
    cells = plan_cells(["LHR", "BCN"], ["BCN", "MAD"], [30])
    pairs = {(c.origin, c.destination) for c in cells}
    assert ("BCN", "BCN") not in pairs
    assert ("LHR", "BCN") in pairs


def _synthetic_iata_codes(n: int, prefix: str) -> list[str]:
    """``n`` distinct 3-letter uppercase codes, e.g. ``AAA, AAB, ... `` with a
    fixed first letter per scope so origins and destinations never collide."""
    import string
    out = []
    letters = string.ascii_uppercase
    for i in range(n):
        out.append(prefix + letters[i // len(letters)] + letters[i % len(letters)])
    return out


def test_planner_reasons_cheaply_about_30x203x6_scale():
    import time
    origins = _synthetic_iata_codes(30, "O")
    dests = _synthetic_iata_codes(203, "D")
    horizons = [14, 30, 45, 60, 90, 120]
    started = time.perf_counter()
    cells = plan_cells(origins, dests, horizons)
    elapsed = time.perf_counter() - started
    assert len(cells) == 30 * 203 * 6  # no origin==destination collisions in this synthetic set
    assert elapsed < 2.0  # planning must stay cheap, not imply execution


# ======================================================================
# §33 — dry-run: zero network, zero writes
# ======================================================================
def test_dry_run_makes_no_writes_and_reports_zero_network():
    d = _db()
    reg = _reg()
    report = dry_run(d, registration=reg, origins=["LHR"], destinations=["BCN", "MAD"],
                     horizon_days=[30, 60], request_budget=3, now=NOW)
    assert report.as_dict()["network_requests_made"] == 0
    assert prior_store.coverage_summary(d)["rows"] == 0
    assert store.list_jobs(d) == []
    assert report.potential_cells == 4
    assert report.planned_requests == 3  # clamped by the budget
    assert report.truncated_by_budget == 1


def test_dry_run_dedup_skips_already_fresh_markets():
    d = _db()
    from detoura.models.market_prior import BootstrapMarketPrior, HorizonBucket, PriorConfidence, SeasonBucket
    p = BootstrapMarketPrior(
        prior_id="prior_xxxxxxxx", source="other", source_version="1", imported_at=NOW,
        source_date=date(2026, 5, 20), origin_airport="LHR", destination_airport="BCN",
        season=SeasonBucket.SHOULDER, horizon_bucket=HorizonBucket.H30, currency="EUR",
        confidence=PriorConfidence.MEDIUM,
    )
    prior_store.upsert_priors(d, [p])
    reg = _reg()
    report = dry_run(d, registration=reg, origins=["LHR"], destinations=["BCN"], horizon_days=[30],
                     request_budget=10, dedup_freshness_days=DEFAULT_DEDUP_FRESHNESS_DAYS, now=NOW)
    assert report.already_covered == 1
    assert report.eligible_tasks == 0
    assert report.planned_requests == 0


def test_dry_run_cost_unknown_stays_unknown_never_zero():
    d = _db()
    reg = _reg(request_cost_minor=None)
    report = dry_run(d, registration=reg, origins=["LHR"], destinations=["BCN"], horizon_days=[30],
                     request_budget=5, now=NOW)
    assert report.estimated_cost_minor is None  # not 0


def test_dry_run_cost_known_is_computed():
    d = _db()
    reg = _reg(request_cost_minor=2)
    report = dry_run(d, registration=reg, origins=["LHR"], destinations=["BCN", "MAD"], horizon_days=[30],
                     request_budget=10, now=NOW)
    assert report.estimated_cost_minor == 2 * report.planned_requests


# ======================================================================
# §11, §27 — job/task persistence, idempotent planning
# ======================================================================
def test_planning_a_job_twice_is_idempotent():
    d = _db()
    store.upsert_source(d, _reg())
    job_id = store.create_job(d, source_id="s1", origins=["LHR"], destinations=["BCN", "MAD"],
                              horizon_days=[30], request_budget=100, dry_run=False, now=NOW)
    cells = plan_cells(["LHR"], ["BCN", "MAD"], [30])
    r1 = store.plan_tasks(d, job_id=job_id, source_id="s1", cells=cells, now=NOW)
    r2 = store.plan_tasks(d, job_id=job_id, source_id="s1", cells=cells, now=NOW)
    assert r1["planned"] == 2 and r2["planned"] == 0 and r2["deduplicated"] == 2
    assert store.job_counters(d, job_id)["total_tasks"] == 2


def test_claim_is_atomic_no_double_claim():
    d = _db()
    store.upsert_source(d, _reg())
    job_id = store.create_job(d, source_id="s1", origins=["LHR"], destinations=["BCN"],
                              horizon_days=[30], request_budget=10, dry_run=False, now=NOW)
    store.plan_tasks(d, job_id=job_id, source_id="s1", cells=plan_cells(["LHR"], ["BCN"], [30]), now=NOW)
    t1 = store.claim_next_task(d, job_id, lease_owner="w1", lease_seconds=60, now=NOW)
    t2 = store.claim_next_task(d, job_id, lease_owner="w2", lease_seconds=60, now=NOW)
    assert t1 is not None and t2 is None  # only one task existed


def test_stale_running_task_is_recovered_after_lease_expiry():
    d = _db()
    store.upsert_source(d, _reg())
    job_id = store.create_job(d, source_id="s1", origins=["LHR"], destinations=["BCN"],
                              horizon_days=[30], request_budget=10, dry_run=False, now=NOW)
    store.plan_tasks(d, job_id=job_id, source_id="s1", cells=plan_cells(["LHR"], ["BCN"], [30]), now=NOW)
    dead = store.claim_next_task(d, job_id, lease_owner="dead", lease_seconds=1, now=NOW)
    # before the lease expires, nobody else can claim it
    assert store.claim_next_task(d, job_id, lease_owner="w2", lease_seconds=60, now=NOW) is None
    later = NOW + timedelta(seconds=5)
    revived = store.claim_next_task(d, job_id, lease_owner="w3", lease_seconds=60, now=later)
    assert revived is not None and revived["task_id"] == dead["task_id"]


def test_stale_worker_cannot_stomp_a_task_reclaimed_by_another_worker():
    # QA-caught hardening (non-blocking observation, closed anyway): a
    # worker whose lease already expired and was reclaimed by a DIFFERENT
    # worker must not be able to mark_task_result/release_task_claim its way
    # into corrupting the new claimant's in-progress work.
    d = _db()
    store.upsert_source(d, _reg())
    job_id = store.create_job(d, source_id="s1", origins=["LHR"], destinations=["BCN"],
                              horizon_days=[30], request_budget=10, dry_run=False, now=NOW)
    store.plan_tasks(d, job_id=job_id, source_id="s1", cells=plan_cells(["LHR"], ["BCN"], [30]), now=NOW)

    stale = store.claim_next_task(d, job_id, lease_owner="stale-worker", lease_seconds=1, now=NOW)
    later = NOW + timedelta(seconds=5)
    reclaimed = store.claim_next_task(d, job_id, lease_owner="new-worker", lease_seconds=60, now=later)
    assert reclaimed["task_id"] == stale["task_id"]

    # The original (now-stale) worker finally gets around to reporting a
    # result - using ITS OWN lease_owner. Neither call should touch the
    # task, which the new worker legitimately owns.
    applied = store.mark_task_result(d, stale["task_id"], status=TaskStatus.SUCCEEDED,
                                     lease_owner="stale-worker", now=later)
    assert applied is False
    row = store.list_tasks(d, job_id)[0]
    assert row["status"] == "RUNNING" and row["lease_owner"] == "new-worker"

    released = store.release_task_claim(d, stale["task_id"], lease_owner="stale-worker", now=later)
    assert released is False
    row = store.list_tasks(d, job_id)[0]
    assert row["status"] == "RUNNING" and row["lease_owner"] == "new-worker"

    # The legitimate new worker's own call succeeds normally.
    applied2 = store.mark_task_result(d, stale["task_id"], status=TaskStatus.SUCCEEDED,
                                      lease_owner="new-worker", now=later)
    assert applied2 is True
    assert store.list_tasks(d, job_id)[0]["status"] == "SUCCEEDED"


def test_run_job_slice_generates_a_unique_lease_owner_per_invocation(monkeypatch):
    d = _db()
    store.upsert_source(d, _reg())
    job_id = store.create_job(d, source_id="s1", origins=["LHR"], destinations=["BCN", "MAD"],
                              horizon_days=[30], request_budget=10, dry_run=False, now=NOW)
    store.plan_tasks(d, job_id=job_id, source_id="s1",
                     cells=plan_cells(["LHR"], ["BCN", "MAD"], [30]), now=NOW)
    reg = store.get_source(d, "s1")

    seen_owners = []
    real_claim = store.claim_next_task

    def spy_claim(db, job_id, *, lease_owner, **kw):
        seen_owners.append(lease_owner)
        return real_claim(db, job_id, lease_owner=lease_owner, **kw)

    import detoura.services.bootstrap_executor as executor_mod
    monkeypatch.setattr(executor_mod.store, "claim_next_task", spy_claim)

    run_job_slice(d, job_id, fetcher=FixtureSourceFetcher(), source=reg, max_tasks=1, now=NOW)
    run_job_slice(d, job_id, fetcher=FixtureSourceFetcher(), source=reg, max_tasks=1, now=NOW)

    assert len(seen_owners) == 2
    assert seen_owners[0] != seen_owners[1]  # never a shared fixed literal like "executor"
    assert store.job_counters(d, job_id)["succeeded"] == 2


def test_cancel_pending_leaves_running_and_terminal_tasks_alone():
    d = _db()
    store.upsert_source(d, _reg())
    job_id = store.create_job(d, source_id="s1", origins=["LHR"], destinations=["BCN", "MAD"],
                              horizon_days=[30], request_budget=10, dry_run=False, now=NOW)
    store.plan_tasks(d, job_id=job_id, source_id="s1", cells=plan_cells(["LHR"], ["BCN", "MAD"], [30]), now=NOW)
    claimed = store.claim_next_task(d, job_id, lease_owner="w1", lease_seconds=60, now=NOW)
    n = store.cancel_pending(d, job_id, now=NOW)
    assert n == 1
    counters = store.job_counters(d, job_id)
    assert counters["running"] == 1 and counters["cancelled"] == 1


# ======================================================================
# §16, §18, §27 — executor: budget, resume, idempotency
# ======================================================================
def _make_job(d, *, dests=("BCN", "MAD", "FCO"), budget=100):
    store.upsert_source(d, _reg())
    job_id = store.create_job(d, source_id="s1", origins=["LHR"], destinations=list(dests),
                              horizon_days=[30], request_budget=budget, dry_run=False, now=NOW)
    store.plan_tasks(d, job_id=job_id, source_id="s1", cells=plan_cells(["LHR"], list(dests), [30]), now=NOW)
    return job_id


def test_executor_runs_to_completion_and_imports_priors():
    d = _db()
    job_id = _make_job(d)
    res = run_job_slice(d, job_id, fetcher=FixtureSourceFetcher(), source=store.get_source(d, "s1"),
                        max_tasks=100, now=NOW)
    assert res.tasks_attempted == res.succeeded == 3
    assert res.job_completed is True
    assert prior_store.coverage_summary(d)["rows"] == 3
    assert store.get_job(d, job_id)["status"] == "COMPLETED"


def test_hard_request_budget_stops_a_slice_and_job_resumes():
    d = _db()
    job_id = _make_job(d, budget=2)
    reg = store.get_source(d, "s1")
    res1 = run_job_slice(d, job_id, fetcher=FixtureSourceFetcher(), source=reg, max_tasks=100, now=NOW)
    assert res1.requests_used == 2
    assert res1.stopped_reason == "request_budget_exhausted"
    assert store.get_job(d, job_id)["status"] != "COMPLETED"

    with d.write() as c:
        c.execute("UPDATE market_prior_jobs SET request_budget=100 WHERE job_id=?", (job_id,))
    res2 = run_job_slice(d, job_id, fetcher=FixtureSourceFetcher(), source=reg, max_tasks=100, now=NOW)
    assert res2.job_completed is True
    assert prior_store.coverage_summary(d)["rows"] == 3  # not doubled


def test_resume_after_crash_never_double_counts_priors():
    d = _db()
    job_id = _make_job(d)
    reg = store.get_source(d, "s1")
    # simulate a crash: claim one task, never resolve it
    crashed = store.claim_next_task(d, job_id, lease_owner="dead", lease_seconds=1, now=NOW)
    later = NOW + timedelta(seconds=5)
    res = run_job_slice(d, job_id, fetcher=FixtureSourceFetcher(), source=reg, max_tasks=100, now=later)
    assert res.job_completed is True
    assert prior_store.coverage_summary(d)["rows"] == 3  # exactly one row per market, no duplicate


def test_authorization_revoked_before_run_stops_with_zero_requests():
    d = _db()
    reg = _reg(source_type=SourceType.AUTHORIZED_WEB_SOURCE, base_domain="example.com",
               authorization_status=AuthorizationStatus.APPROVED)
    store.upsert_source(d, reg)
    job_id = store.create_job(d, source_id="s1", origins=["LHR"], destinations=["BCN"],
                              horizon_days=[30], request_budget=10, dry_run=False, now=NOW)
    store.plan_tasks(d, job_id=job_id, source_id="s1", cells=plan_cells(["LHR"], ["BCN"], [30]), now=NOW)
    store.set_authorization(d, "s1", status=AuthorizationStatus.DISABLED, now=NOW)

    class NeverCalledFetcher:
        parser_version = "x"
        def fetch_one(self, task):
            raise AssertionError("must never be called - authorization is revoked")

    res = run_job_slice(d, job_id, fetcher=NeverCalledFetcher(), source=reg, max_tasks=10, now=NOW)
    assert res.requests_used == 0
    assert res.stopped_reason == "AUTHORIZATION_DISABLED"
    assert store.get_job(d, job_id)["status"] == "PAUSED"


def test_authorization_revoked_mid_run_stops_before_the_next_task():
    d = _db()
    reg = _reg(source_type=SourceType.AUTHORIZED_WEB_SOURCE, base_domain="example.com",
               authorization_status=AuthorizationStatus.APPROVED)
    store.upsert_source(d, reg)
    job_id = store.create_job(d, source_id="s1", origins=["LHR"], destinations=["BCN", "MAD", "FCO"],
                              horizon_days=[30], request_budget=100, dry_run=False, now=NOW)
    store.plan_tasks(d, job_id=job_id, source_id="s1", cells=plan_cells(["LHR"], ["BCN", "MAD", "FCO"], [30]), now=NOW)

    calls = {"n": 0}

    class RevokeAfterOne:
        parser_version = "x"
        def fetch_one(self, task):
            calls["n"] += 1
            if calls["n"] == 1:
                store.set_authorization(d, "s1", status=AuthorizationStatus.DISABLED, now=NOW)
                return {"origin_airport": task.origin, "destination_airport": task.destination,
                        "currency": "EUR", "median": 90.0, "horizon_days": task.horizon_days}
            raise AssertionError("must not fetch again after authorization is revoked")

    res = run_job_slice(d, job_id, fetcher=RevokeAfterOne(), source=reg, max_tasks=10, now=NOW)
    assert calls["n"] == 1
    assert res.succeeded == 1
    assert res.stopped_reason == "AUTHORIZATION_DISABLED"
    assert store.get_job(d, job_id)["status"] == "PAUSED"


def test_malformed_fetched_record_fails_the_task_not_the_run():
    d = _db()
    job_id = _make_job(d, dests=("BCN", "MAD"))
    reg = store.get_source(d, "s1")

    class OneBadOneGood:
        parser_version = "x"
        def fetch_one(self, task):
            if task.destination == "BCN":
                return {"origin_airport": task.origin, "destination_airport": task.destination,
                        "currency": "EUR", "observed_low": 200, "observed_high": 50}  # low > high
            return {"origin_airport": task.origin, "destination_airport": task.destination,
                    "currency": "EUR", "median": 80.0, "horizon_days": task.horizon_days}

    res = run_job_slice(d, job_id, fetcher=OneBadOneGood(), source=reg, max_tasks=10, now=NOW)
    assert res.failed == 1 and res.succeeded == 1
    assert prior_store.coverage_summary(d)["rows"] == 1


def test_no_data_is_an_honest_outcome_not_a_failure():
    d = _db()
    store.upsert_source(d, _reg())
    job_id = store.create_job(d, source_id="s1", origins=["LHR"], destinations=["BCN"],
                              horizon_days=[30], request_budget=10, dry_run=False, now=NOW)
    store.plan_tasks(d, job_id=job_id, source_id="s1", cells=plan_cells(["LHR"], ["BCN"], [30]), now=NOW)
    fetcher = FixtureSourceFetcher(no_data_cells=frozenset({("LHR", "BCN", 30)}))
    res = run_job_slice(d, job_id, fetcher=fetcher, source=store.get_source(d, "s1"), max_tasks=10, now=NOW)
    assert res.no_data == 1 and res.failed == 0 and res.blocked == 0
    assert prior_store.coverage_summary(d)["rows"] == 0


# ======================================================================
# §24, §30 — currency truth, no PII
# ======================================================================
def test_mixed_currency_records_are_not_merged():
    d = _db()
    job_id = _make_job(d, dests=("BCN", "MAD"))
    reg = store.get_source(d, "s1")

    class MixedCurrency:
        parser_version = "x"
        def fetch_one(self, task):
            ccy = "GBP" if task.destination == "BCN" else "EUR"
            return {"origin_airport": task.origin, "destination_airport": task.destination,
                    "currency": ccy, "median": 80.0, "horizon_days": task.horizon_days}

    run_job_slice(d, job_id, fetcher=MixedCurrency(), source=reg, max_tasks=10, now=NOW)
    rows = d.query("SELECT destination_airport, currency FROM market_priors")
    by_dest = {r["destination_airport"]: r["currency"] for r in rows}
    assert by_dest["BCN"] == "GBP" and by_dest["MAD"] == "EUR"


def test_no_pii_columns_in_acquisition_tables():
    d = _db()
    for tbl in ("market_prior_sources", "market_prior_jobs", "market_prior_tasks"):
        cols = {r["name"] for r in d.query(f"PRAGMA table_info({tbl})")}
        for bad in ("name", "email", "phone", "passport", "traveler", "user_id", "account_id"):
            assert not any(bad in c for c in cols if c != "source_name"), (tbl, bad)


# ======================================================================
# §28 — search independence
# ======================================================================
def test_search_and_booking_paths_never_import_acquisition_modules():
    import detoura.services.live_search as live_search
    import detoura.services.real_supply as real_supply
    import detoura.services.search_intel_recorder as recorder
    import detoura.services.booking_flow as booking_flow
    src = "".join(open(m.__file__).read() for m in (live_search, real_supply, recorder, booking_flow))
    for bad in ("bootstrap_executor", "bootstrap_planner", "network_adapter", "bootstrap_registry",
                "market_prior_acquisition"):
        assert bad not in src


# ======================================================================
# §16, §59 — hard request budget under CONCURRENT executor invocations
# (QA-caught regression: reserve_request/release_request must make the
# check-then-increment atomic across threads, not just within one call)
# ======================================================================
def test_concurrent_slices_never_exceed_the_hard_request_budget():
    d = _db()
    reg = _reg(source_id="race")
    store.upsert_source(d, reg)
    dests = _synthetic_iata_codes(200, "D")
    job_id = store.create_job(d, source_id="race", origins=["LHR"], destinations=dests,
                              horizon_days=[30], request_budget=20, dry_run=False, now=NOW)
    store.plan_tasks(d, job_id=job_id, source_id="race", cells=plan_cells(["LHR"], dests, [30]), now=NOW)

    errors = []

    def worker():
        try:
            run_job_slice(d, job_id, fetcher=FixtureSourceFetcher(), source=reg, max_tasks=100, now=NOW)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    job = store.get_job(d, job_id)
    assert job["requests_used"] <= job["request_budget"] == 20
    assert job["requests_used"] == 20  # the budget is fully, exactly spent - not over, not silently under


def test_high_contention_budget_race_still_never_exceeds_the_budget():
    # A much harsher version of the race above: 25 threads, a tighter
    # budget relative to the thread count (so almost every thread loses the
    # race for at least one slot), and each thread runs several slices back
    # to back rather than one - maximises the number of check-then-act
    # windows an unfixed implementation would have raced through.
    d = _db()
    reg = _reg(source_id="race-heavy")
    store.upsert_source(d, reg)
    dests = _synthetic_iata_codes(200, "D")
    job_id = store.create_job(d, source_id="race-heavy", origins=["LHR"], destinations=dests,
                              horizon_days=[30], request_budget=30, dry_run=False, now=NOW)
    store.plan_tasks(d, job_id=job_id, source_id="race-heavy",
                     cells=plan_cells(["LHR"], dests, [30]), now=NOW)

    errors = []

    def worker():
        try:
            for _ in range(3):
                run_job_slice(d, job_id, fetcher=FixtureSourceFetcher(), source=reg, max_tasks=5, now=NOW)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    job = store.get_job(d, job_id)
    assert job["requests_used"] <= job["request_budget"] == 30
    assert job["requests_used"] == 30
    # every succeeded task actually produced exactly one prior row - no
    # under- or over-counting hiding behind the aggregate budget number
    assert prior_store.coverage_summary(d)["rows"] == store.job_counters(d, job_id)["succeeded"]


def test_concurrent_slices_completing_a_job_never_double_count_priors():
    d = _db()
    reg = _reg(source_id="race2")
    store.upsert_source(d, reg)
    dests = ["BCN", "MAD", "FCO", "LIS"]
    job_id = store.create_job(d, source_id="race2", origins=["LHR"], destinations=dests,
                              horizon_days=[30], request_budget=100, dry_run=False, now=NOW)
    store.plan_tasks(d, job_id=job_id, source_id="race2", cells=plan_cells(["LHR"], dests, [30]), now=NOW)

    def worker():
        run_job_slice(d, job_id, fetcher=FixtureSourceFetcher(), source=reg, max_tasks=10, now=NOW)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert prior_store.coverage_summary(d)["rows"] == 4  # one row per market, never doubled
    assert store.get_job(d, job_id)["status"] == "COMPLETED"
    assert store.job_counters(d, job_id)["succeeded"] == 4


# ======================================================================
# Config
# ======================================================================
def test_acquisition_config_defaults_are_conservative():
    cfg = AcquisitionConfig()
    assert cfg.max_requests_per_run <= 100
    assert cfg.dedup_freshness_days > 0
    assert cfg.task_lease_seconds >= 10
