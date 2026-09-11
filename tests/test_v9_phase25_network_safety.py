"""V9 Phase 2.5 — the authorized network adapter's safety controls
(§6, §20, §29, §59): domain allowlist, SSRF guard, redirect validation,
response-size limit, CAPTCHA/challenge detection, rate limiting."""

from __future__ import annotations

import multiprocessing
from datetime import datetime, timezone

import pytest

from detoura.models.market_prior_acquisition import (
    AuthorizationStatus, RateLimitPolicy, SourceRegistration, SourceType, StopReason,
)
from detoura.persistence import market_prior_acquisition as store
from detoura.persistence.db import Database
from detoura.providers.http import HttpResponse, ProviderHttpError, RateLimitExceeded
from detoura.services.network_adapter import AccessBlocked, AuthorizedHttpFetcher, BudgetExhausted

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _reg(**kw) -> SourceRegistration:
    base = dict(source_id="web1", source_name="Web One", source_type=SourceType.AUTHORIZED_WEB_SOURCE,
                authorization_status=AuthorizationStatus.APPROVED, base_domain="example.com",
                rate_limit_policy=RateLimitPolicy(requests_per_minute=6000, min_delay_seconds=0),
                created_at=NOW, updated_at=NOW)
    base.update(kw)
    return SourceRegistration(**base)


class _StubClient:
    def __init__(self, plan):
        self.plan = list(plan)
        self.calls: list[str] = []

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls.append(url)
        item = self.plan.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ======================================================================
# Authorization gate (fail closed) - §4/§20/§59
# ======================================================================
@pytest.mark.parametrize("status", [
    AuthorizationStatus.REVIEW_REQUIRED, AuthorizationStatus.PROHIBITED, AuthorizationStatus.DISABLED,
])
def test_unauthorized_source_never_makes_a_request(status):
    stub = _StubClient([HttpResponse(200, "ok")])
    fetcher = AuthorizedHttpFetcher(_reg(authorization_status=status), stub)
    with pytest.raises(AccessBlocked) as exc:
        fetcher.fetch("https://example.com/x")
    assert exc.value.reason is StopReason.AUTHORIZATION_DISABLED
    assert stub.calls == []


def test_approved_source_can_fetch():
    stub = _StubClient([HttpResponse(200, "fine")])
    fetcher = AuthorizedHttpFetcher(_reg(), stub)
    assert fetcher.fetch("https://example.com/x").status == 200


# ======================================================================
# Domain allowlist / SSRF guard - §29, §59
# ======================================================================
def test_off_domain_url_is_blocked():
    fetcher = AuthorizedHttpFetcher(_reg(), _StubClient([HttpResponse(200, "x")]))
    with pytest.raises(AccessBlocked) as exc:
        fetcher.fetch("https://evil.com/x")
    assert exc.value.reason is StopReason.DOMAIN_NOT_ALLOWED


def test_subdomain_of_allowed_domain_is_allowed():
    stub = _StubClient([HttpResponse(200, "ok")])
    fetcher = AuthorizedHttpFetcher(_reg(), stub)
    assert fetcher.fetch("https://api.example.com/x").status == 200


def test_lookalike_domain_is_not_allowed():
    # "notexample.com" must not match base_domain "example.com" via naive
    # substring/suffix matching without a dot boundary.
    fetcher = AuthorizedHttpFetcher(_reg(), _StubClient([HttpResponse(200, "x")]))
    with pytest.raises(AccessBlocked):
        fetcher.fetch("https://notexample.com/x")


def test_ip_literal_host_is_blocked():
    fetcher = AuthorizedHttpFetcher(_reg(base_domain="127.0.0.1"), _StubClient([HttpResponse(200, "x")]))
    with pytest.raises(AccessBlocked) as exc:
        fetcher.fetch("https://127.0.0.1/x")
    assert exc.value.reason is StopReason.DOMAIN_NOT_ALLOWED


def test_non_https_scheme_is_blocked():
    fetcher = AuthorizedHttpFetcher(_reg(), _StubClient([HttpResponse(200, "x")]))
    with pytest.raises(AccessBlocked):
        fetcher.fetch("http://example.com/x")


def test_redirect_escaping_the_allowed_domain_is_blocked():
    stub = _StubClient([HttpResponse(302, "", headers={"Location": "https://evil.com/steal"})])
    fetcher = AuthorizedHttpFetcher(_reg(), stub)
    with pytest.raises(AccessBlocked) as exc:
        fetcher.fetch("https://example.com/x")
    assert exc.value.reason is StopReason.DOMAIN_NOT_ALLOWED
    assert stub.calls == ["https://example.com/x"]  # never followed onto evil.com


def test_redirect_staying_on_domain_is_followed_once():
    stub = _StubClient([
        HttpResponse(301, "", headers={"Location": "https://example.com/final"}),
        HttpResponse(200, "final content"),
    ])
    fetcher = AuthorizedHttpFetcher(_reg(), stub)
    resp = fetcher.fetch("https://example.com/x")
    assert resp.status == 200 and resp.body == "final content"
    assert stub.calls == ["https://example.com/x", "https://example.com/final"]


def test_too_many_redirects_is_blocked():
    hops = [HttpResponse(302, "", headers={"Location": f"https://example.com/{i}"}) for i in range(6)]
    fetcher = AuthorizedHttpFetcher(_reg(), _StubClient(hops), max_redirects=3)
    with pytest.raises(AccessBlocked):
        fetcher.fetch("https://example.com/0")


# ======================================================================
# Response-size limit - §23, §29, §59
# ======================================================================
def test_oversized_response_is_blocked():
    big = "x" * 50_000
    fetcher = AuthorizedHttpFetcher(_reg(), _StubClient([HttpResponse(200, big)]), max_response_bytes=1000)
    with pytest.raises(AccessBlocked) as exc:
        fetcher.fetch("https://example.com/x")
    assert exc.value.reason is StopReason.RESPONSE_TOO_LARGE


# ======================================================================
# CAPTCHA / access-denied detection - §20, §59
# ======================================================================
def test_captcha_body_is_detected():
    stub = _StubClient([HttpResponse(200, "<html>Please verify you are human</html>")])
    fetcher = AuthorizedHttpFetcher(_reg(), stub)
    with pytest.raises(AccessBlocked) as exc:
        fetcher.fetch("https://example.com/x")
    assert exc.value.reason is StopReason.CAPTCHA_DETECTED


def test_persistent_403_without_challenge_markers_is_access_denied():
    stub = _StubClient([HttpResponse(403, "no thanks")])
    fetcher = AuthorizedHttpFetcher(_reg(), stub)
    with pytest.raises(AccessBlocked) as exc:
        fetcher.fetch("https://example.com/x")
    assert exc.value.reason is StopReason.ACCESS_DENIED


def test_persistent_429_after_retry_budget_is_rate_limited():
    # RetryingHttpClient exhausts its retry budget on 429 and raises
    # RateLimitExceeded; the fetcher must translate that into AccessBlocked.
    stub = _StubClient([HttpResponse(429, "slow down")] * 10)
    fetcher = AuthorizedHttpFetcher(_reg(), stub)
    with pytest.raises(AccessBlocked) as exc:
        fetcher.fetch("https://example.com/x")
    assert exc.value.reason is StopReason.RATE_LIMITED


def test_connection_failure_surfaces_as_access_denied_not_a_crash():
    stub = _StubClient([ProviderHttpError("could not reach host")] * 10)
    fetcher = AuthorizedHttpFetcher(_reg(), stub)
    with pytest.raises(AccessBlocked) as exc:
        fetcher.fetch("https://example.com/x")
    assert exc.value.reason is StopReason.ACCESS_DENIED


# ======================================================================
# Rate limiting - §17
# ======================================================================
def test_rate_limit_policy_effective_interval_takes_the_stricter_floor():
    fast = RateLimitPolicy(requests_per_minute=6000, min_delay_seconds=0.0)
    assert fast.effective_min_interval_seconds < 0.02
    slow = RateLimitPolicy(requests_per_minute=6000, min_delay_seconds=2.5)
    assert slow.effective_min_interval_seconds == 2.5
    by_rate = RateLimitPolicy(requests_per_minute=30, min_delay_seconds=0.0)
    assert by_rate.effective_min_interval_seconds == pytest.approx(2.0)


def test_fetcher_paces_requests_by_the_configured_rate_limit():
    clock = {"t": 0.0}
    waited = []

    def fake_clock():
        return clock["t"]

    def fake_sleep(s):
        waited.append(s)
        clock["t"] += s

    reg = _reg(rate_limit_policy=RateLimitPolicy(requests_per_minute=60, min_delay_seconds=0))
    fetcher = AuthorizedHttpFetcher(reg, _StubClient([HttpResponse(200, "a"), HttpResponse(200, "b")]))
    fetcher._rate_limiter._clock = fake_clock
    fetcher._rate_limiter._sleep = fake_sleep
    fetcher.fetch("https://example.com/1")
    fetcher.fetch("https://example.com/2")
    assert sum(waited) >= 0.9  # ~1s minimum interval enforced between the two calls


# ======================================================================
# Retry request accounting - every real outbound attempt (including
# RetryingHttpClient retries) must be individually budget-gated and counted,
# not just once per logical task (§16, §59 hard invariant follow-up).
# ======================================================================
def _job_with_source(request_budget: int, *, requests_per_minute: int = 6000) -> tuple[Database, SourceRegistration, str]:
    d = Database(":memory:")
    reg = _reg(rate_limit_policy=RateLimitPolicy(requests_per_minute=requests_per_minute, min_delay_seconds=0))
    store.upsert_source(d, reg)
    job_id = store.create_job(d, source_id=reg.source_id, origins=["LHR"], destinations=["BCN"],
                              horizon_days=[30], request_budget=request_budget, dry_run=False, now=NOW)
    return d, reg, job_id


def test_retry_attempts_all_succeed_within_budget_three():
    d, reg, job_id = _job_with_source(request_budget=3)
    stub = _StubClient([
        HttpResponse(503, "try again"), HttpResponse(503, "try again"), HttpResponse(200, "ok"),
    ])
    fetcher = AuthorizedHttpFetcher(reg, stub, db=d, job_id=job_id)
    fetcher._client._sleep = lambda s: None  # no real backoff wait in the test

    response = fetcher.fetch("https://example.com/x")

    assert response.status == 200
    assert len(stub.calls) == 3  # exactly 3 outbound attempts were sent
    assert store.get_job(d, job_id)["requests_used"] == 3


def test_third_retry_is_never_sent_once_the_budget_is_two():
    d, reg, job_id = _job_with_source(request_budget=2)
    stub = _StubClient([
        HttpResponse(503, "try again"), HttpResponse(503, "try again"), HttpResponse(200, "ok"),
    ])
    fetcher = AuthorizedHttpFetcher(reg, stub, db=d, job_id=job_id)
    fetcher._client._sleep = lambda s: None

    with pytest.raises((BudgetExhausted, ProviderHttpError)):
        fetcher.fetch("https://example.com/x")

    assert len(stub.calls) == 2  # the third (would-be-successful) retry was never sent
    assert store.get_job(d, job_id)["requests_used"] == 2


def test_budget_gate_raises_a_type_retrying_http_client_never_retries_around():
    # BudgetExhausted must not be a ProviderHttpError, or RetryingHttpClient
    # would treat it as retryable and burn through the rest of its own
    # backoff loop for no reason instead of stopping immediately.
    assert not issubclass(BudgetExhausted, ProviderHttpError)


# ======================================================================
# Source-wide rate limiting must be enforced via shared persistence, not an
# in-process/in-thread lock - correct across two jobs on the same source,
# and (below) across two independent OS processes.
# ======================================================================
def test_two_jobs_on_the_same_source_share_one_persisted_rate_limit():
    # Two AuthorizedHttpFetcher instances for two DIFFERENT jobs on the same
    # source both ultimately call reserve_rate_limit_slot keyed only by
    # source_id (never job_id) - so exercising that shared entry point
    # directly is exactly what "two jobs, one source" reduces to, without
    # the noise of simulating the full retry/HTTP stack twice.
    d = Database(":memory:")
    reg = _reg(rate_limit_policy=RateLimitPolicy(requests_per_minute=1200, min_delay_seconds=0))
    store.upsert_source(d, reg)
    store.create_job(d, source_id=reg.source_id, origins=["LHR"], destinations=["BCN"],
                     horizon_days=[30], request_budget=10, dry_run=False, now=NOW)
    store.create_job(d, source_id=reg.source_id, origins=["LHR"], destinations=["MAD"],
                     horizon_days=[30], request_budget=10, dry_run=False, now=NOW)

    interval = reg.rate_limit_policy.effective_min_interval_seconds
    assert interval == pytest.approx(0.05, abs=1e-6)

    # Simulate job A and job B's fetchers both "arriving" at the same
    # instant, interleaved, ten times in a row. If rate limiting were only
    # in-process/per-fetcher (the pre-fix design), every one of these would
    # return wait=0. Because the gate is persisted per source_id, the Nth
    # reservation - no matter which job's fetcher made it - must queue up
    # strictly behind the (N-1) before it.
    waits = [store.reserve_rate_limit_slot(d, reg.source_id, min_interval_seconds=interval, now=NOW)
             for _ in range(10)]

    assert waits[0] == pytest.approx(0.0, abs=1e-6)
    for i in range(1, 10):
        assert waits[i] == pytest.approx(i * interval, abs=1e-6)


def _subprocess_worker(db_path: str, source_id: str, n: int, out_path: str) -> None:
    """Runs in a genuinely separate OS process (see the test below): makes
    ``n`` real reservation calls against the same on-disk SQLite file, so the
    parent process can verify the combined sequence across *processes* — not
    just threads — respects the configured minimum interval."""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    from detoura.persistence import market_prior_acquisition as _store
    from detoura.persistence.db import Database as _Database

    db = _Database(db_path)
    waits = [
        _store.reserve_rate_limit_slot(db, source_id, min_interval_seconds=0.05, now=_dt.now(_tz.utc))
        for _ in range(n)
    ]
    with open(out_path, "w") as f:
        _json.dump(waits, f)


def test_rate_limit_is_enforced_across_two_independent_processes(tmp_path):
    db_path = str(tmp_path / "cross_process.db")
    d = Database(db_path)
    now = datetime.now(timezone.utc)
    reg = _reg(source_id="cross-proc-src")
    store.upsert_source(d, reg)
    d.close()

    out1, out2 = str(tmp_path / "out1.json"), str(tmp_path / "out2.json")
    p1 = multiprocessing.Process(target=_subprocess_worker, args=(db_path, "cross-proc-src", 15, out1))
    p2 = multiprocessing.Process(target=_subprocess_worker, args=(db_path, "cross-proc-src", 15, out2))
    p1.start(); p2.start()
    p1.join(timeout=60); p2.join(timeout=60)
    assert p1.exitcode == 0 and p2.exitcode == 0

    import json
    waits1 = json.load(open(out1))
    waits2 = json.load(open(out2))
    all_waits = sorted(waits1 + waits2)
    assert len(all_waits) == 30

    # The defining cross-process property: 30 combined reservations at a
    # 0.05s minimum interval, from two OS processes that share no in-memory
    # state whatsoever, must together occupy at least 29 * 0.05s of
    # persisted slot time - proven by reading next_allowed_at back after
    # both processes exit.
    d2 = Database(db_path)
    row = d2.query_one(
        "SELECT next_allowed_at FROM market_prior_sources WHERE source_id=?", ("cross-proc-src",),
    )
    final_next_allowed = datetime.fromisoformat(row["next_allowed_at"])
    assert (final_next_allowed - now).total_seconds() >= 29 * 0.05 - 0.5  # small clock-skew tolerance
    # and the individual waits actually form a spread sequence, not 30 zeros
    assert max(all_waits) >= 29 * 0.05 - 0.5
