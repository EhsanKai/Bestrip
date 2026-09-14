"""V9 Phase 6 slice 1 — login abuse-control hardening.

Replaces the email-only login rate limit (which let an unauthenticated
attacker who merely knows a victim's address deny that victim's own logins)
with two independent budgets: a coarse per-IP volume control and a short,
soft per-(IP, email) throttle. Also covers the bounded, self-evicting
``RateLimiter`` primitive and the ``client_ip`` trust boundary.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from detoura.auth_config import AuthConfig
from detoura.persistence.db import Database
from detoura.services import auth_service
from detoura.services.auth_service import AuthError, RateLimitedError
from detoura.services.client_ip import UNKNOWN, resolve_client_ip
from detoura.services.rate_limit import RateLimiter, rate_limiter

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
VICTIM_EMAIL = "victim@example.com"


def _db() -> Database:
    return Database(":memory:")


def _register(db: Database, email: str = VICTIM_EMAIL, password: str = "correct horse battery") -> None:
    auth_service.register(db, email=email, password=password, now=NOW)


@pytest.fixture(autouse=True)
def _clean_limiter():
    rate_limiter().clear()
    yield
    rate_limiter().clear()


# ======================================================================
# 1 & 2 & 3 — never keyed by email alone; a victim on their own IP is safe
# ======================================================================
def test_attacker_cannot_hard_lock_victim_globally_by_email_alone():
    """Many failed attempts against victim@example.com from ONE attacker IP
    throttle that (attacker_ip, email) pair - but the victim, logging in
    correctly from a DIFFERENT IP, is never blocked."""
    d = _db()
    _register(d)
    cfg = AuthConfig(login_max_attempts=3, login_window_seconds=60.0)

    for _ in range(3):
        with pytest.raises(AuthError):
            auth_service.login(d, email=VICTIM_EMAIL, password="wrong",
                                client_ip="203.0.113.9", now=NOW, cfg=cfg)
    with pytest.raises(RateLimitedError):
        auth_service.login(d, email=VICTIM_EMAIL, password="wrong",
                            client_ip="203.0.113.9", now=NOW, cfg=cfg)

    # The victim, from their own IP, logs in successfully - unaffected by
    # the attacker's (different-IP) failures against the same email.
    result = auth_service.login(d, email=VICTIM_EMAIL, password="correct horse battery",
                                 client_ip="198.51.100.7", now=NOW, cfg=cfg)
    assert result.user_id


def test_same_email_different_legitimate_ip_not_blocked_by_email_only_lock():
    """Repeated failures for one email from IP A do not throttle the same
    email from IP B at all - there is no email-only scope any more."""
    d = _db()
    _register(d)
    cfg = AuthConfig(login_max_attempts=2, login_window_seconds=60.0)

    for _ in range(2):
        with pytest.raises(AuthError):
            auth_service.login(d, email=VICTIM_EMAIL, password="wrong",
                                client_ip="10.0.0.1", now=NOW, cfg=cfg)
    with pytest.raises(RateLimitedError):
        auth_service.login(d, email=VICTIM_EMAIL, password="wrong",
                            client_ip="10.0.0.1", now=NOW, cfg=cfg)

    # Different IP, same email, first attempt - not pre-throttled.
    result = auth_service.login(d, email=VICTIM_EMAIL, password="correct horse battery",
                                 client_ip="10.0.0.2", now=NOW, cfg=cfg)
    assert result.user_id


# ======================================================================
# 4 — per-IP abuse control catches one source cycling through many emails
# ======================================================================
def test_one_ip_cycling_through_many_emails_hits_per_ip_control():
    d = _db()
    cfg = AuthConfig(login_ip_max_attempts=3, login_ip_window_seconds=60.0,
                      login_max_attempts=100, login_window_seconds=60.0)
    for i in range(3):
        with pytest.raises(AuthError):
            auth_service.login(d, email=f"nobody{i}@example.com", password="whatever",
                                client_ip="198.51.100.50", now=NOW, cfg=cfg)
    with pytest.raises(RateLimitedError):
        auth_service.login(d, email="nobody999@example.com", password="whatever",
                            client_ip="198.51.100.50", now=NOW, cfg=cfg)


def test_per_ip_limit_does_not_starve_a_different_source_ip():
    d = _db()
    _register(d)
    cfg = AuthConfig(login_ip_max_attempts=1, login_ip_window_seconds=60.0)
    with pytest.raises(AuthError):
        auth_service.login(d, email="nobody@example.com", password="whatever",
                            client_ip="198.51.100.50", now=NOW, cfg=cfg)
    with pytest.raises(RateLimitedError):
        auth_service.login(d, email="anyone@example.com", password="whatever",
                            client_ip="198.51.100.50", now=NOW, cfg=cfg)
    # A different IP entirely has its own, untouched per-IP budget.
    result = auth_service.login(d, email=VICTIM_EMAIL, password="correct horse battery",
                                 client_ip="203.0.113.77", now=NOW, cfg=cfg)
    assert result.user_id


# ======================================================================
# 5 — success resets the targeted (pair) budget, not the IP volume budget
# ======================================================================
def test_success_resets_pair_budget_but_not_ip_budget():
    d = _db()
    _register(d)
    cfg = AuthConfig(login_max_attempts=5, login_window_seconds=60.0,
                      login_ip_max_attempts=3, login_ip_window_seconds=60.0)
    ip = "192.0.2.10"

    with pytest.raises(AuthError):
        auth_service.login(d, email=VICTIM_EMAIL, password="wrong", client_ip=ip, now=NOW, cfg=cfg)
    result = auth_service.login(d, email=VICTIM_EMAIL, password="correct horse battery",
                                 client_ip=ip, now=NOW, cfg=cfg)
    assert result.user_id
    # IP budget: 2 of 3 consumed so far (1 failure + 1 success). One more
    # attempt (regardless of outcome) should still be allowed...
    with pytest.raises(AuthError):
        auth_service.login(d, email=VICTIM_EMAIL, password="wrong", client_ip=ip, now=NOW, cfg=cfg)
    # ...but the 4th call from this IP within the window is over budget,
    # proving the IP counter kept accumulating through the earlier success
    # rather than being reset by it.
    with pytest.raises(RateLimitedError):
        auth_service.login(d, email=VICTIM_EMAIL, password="correct horse battery",
                            client_ip=ip, now=NOW, cfg=cfg)


# ======================================================================
# 6 — enumeration safety preserved through the new rate-limit paths
# ======================================================================
def test_enumeration_safe_across_existing_and_nonexistent_email():
    d = _db()
    _register(d)
    # A generous per-IP budget here isolates the assertions below to the
    # per-(ip, email) pair throttle specifically.
    cfg = AuthConfig(login_max_attempts=100, login_window_seconds=60.0,
                      login_ip_max_attempts=1_000, login_ip_window_seconds=60.0)

    with pytest.raises(AuthError) as exc_existing:
        auth_service.login(d, email=VICTIM_EMAIL, password="wrong",
                            client_ip="1.2.3.4", now=NOW, cfg=cfg)
    with pytest.raises(AuthError) as exc_missing:
        auth_service.login(d, email="nobody-at-all@example.com", password="wrong",
                            client_ip="1.2.3.4", now=NOW, cfg=cfg)
    assert str(exc_existing.value) == str(exc_missing.value) == auth_service.GENERIC_LOGIN_FAILURE

    # And once a per-(ip,email) pair is throttled, it is throttled the same
    # (429, no distinguishing detail) whether or not the account is real.
    for _ in range(100):
        try:
            auth_service.login(d, email="another-nobody@example.com", password="wrong",
                                client_ip="1.2.3.4", now=NOW, cfg=cfg)
        except AuthError:
            pass
    with pytest.raises(RateLimitedError):
        auth_service.login(d, email="another-nobody@example.com", password="wrong",
                            client_ip="1.2.3.4", now=NOW, cfg=cfg)


# ======================================================================
# 7 — expired limiter entries are removed (bounded memory, part 1)
# ======================================================================
def test_expired_windows_are_swept():
    rl = RateLimiter(sweep_interval_seconds=10.0)
    assert rl.allow("s", "k1", max_calls=1, window_seconds=5.0, now=0.0)
    assert rl.size() == 1
    # Advance well past both the window and the sweep interval, and touch a
    # different key - the sweep is opportunistic (piggybacks on a call), so
    # a call must actually happen for it to fire.
    assert rl.allow("s", "k2", max_calls=1, window_seconds=5.0, now=100.0)
    assert rl.size() == 1  # k1's now-expired window was swept away
    assert rl.allow("s", "k1", max_calls=1, window_seconds=5.0, now=100.0)  # fresh window


# ======================================================================
# 8 — high-cardinality floods cannot grow limiter storage indefinitely
# ======================================================================
def test_bounded_entries_under_high_cardinality_flood():
    rl = RateLimiter(max_entries=100, sweep_interval_seconds=1_000_000.0)
    for i in range(10_000):
        rl.allow("login_ip", f"attacker-controlled-id-{i}", max_calls=1,
                 window_seconds=1_000_000.0, now=0.0)
    assert rl.size() <= 100


def test_eviction_is_least_recently_used_not_arbitrary():
    rl = RateLimiter(max_entries=2, sweep_interval_seconds=1_000_000.0)
    assert rl.allow("s", "a", max_calls=10, window_seconds=100.0, now=0.0)
    assert rl.allow("s", "b", max_calls=10, window_seconds=100.0, now=0.0)
    # Touch "a" again so "b" becomes the least-recently-used entry.
    assert rl.allow("s", "a", max_calls=10, window_seconds=100.0, now=1.0)
    assert rl.allow("s", "c", max_calls=10, window_seconds=100.0, now=2.0)  # evicts "b"
    assert rl.size() == 2
    # "a" survived (was touched most recently); "b" was evicted and gets a
    # brand-new window rather than resuming a stale count.
    assert rl.allow("s", "a", max_calls=1, window_seconds=100.0, now=3.0) is False  # already at cap from before
    assert rl.allow("s", "b", max_calls=1, window_seconds=100.0, now=3.0) is True


# ======================================================================
# 9 — concurrency does not corrupt limiter state
# ======================================================================
def test_concurrent_calls_never_exceed_max_calls():
    rl = RateLimiter()
    max_calls = 50
    results: list[bool] = []
    results_lock = threading.Lock()

    def _attempt():
        ok = rl.allow("s", "shared-key", max_calls=max_calls, window_seconds=60.0, now=0.0)
        with results_lock:
            results.append(ok)

    threads = [threading.Thread(target=_attempt) for _ in range(500)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == max_calls
    assert results.count(False) == 500 - max_calls


# ======================================================================
# 10 — malformed identifiers cannot create unsafe/unbounded keys
# ======================================================================
def test_client_ip_falls_back_safely_on_malformed_forwarded_header():
    assert resolve_client_ip(
        direct_peer="203.0.113.5",
        forwarded_for="not-an-ip, also not one, " + ("x" * 100_000),
        trusted_proxy_hops=1,
    ) == "203.0.113.5"


def test_client_ip_falls_back_to_unknown_when_nothing_parseable():
    assert resolve_client_ip(direct_peer=None, forwarded_for=None, trusted_proxy_hops=0) == UNKNOWN
    assert resolve_client_ip(direct_peer="testclient", forwarded_for=None, trusted_proxy_hops=0) == UNKNOWN


def test_client_ip_ignores_forwarded_header_when_no_hops_trusted():
    # Even a perfectly well-formed spoofed header must not be trusted at
    # the default trust level.
    assert resolve_client_ip(
        direct_peer="203.0.113.5", forwarded_for="1.2.3.4", trusted_proxy_hops=0,
    ) == "203.0.113.5"


def test_rate_limiter_accepts_arbitrarily_weird_key_text_safely():
    rl = RateLimiter()
    weird = "a" * 10_000 + "\x00\x1f\n,;" + "🚀"
    assert rl.allow("login_ip", weird, max_calls=1, window_seconds=60.0, now=0.0) is True
    assert rl.size() == 1


# ======================================================================
# 11 — IPv4 / IPv6 handling is deterministic
# ======================================================================
def test_ipv6_forwarded_header_parsed_and_normalized():
    assert resolve_client_ip(
        direct_peer="127.0.0.1", forwarded_for="2001:db8::1", trusted_proxy_hops=1,
    ) == "2001:db8::1"


def test_ipv6_bracketed_literal_is_accepted():
    assert resolve_client_ip(
        direct_peer="127.0.0.1", forwarded_for="[2001:db8::1]", trusted_proxy_hops=1,
    ) == "2001:db8::1"


def test_ipv4_and_ipv6_key_the_limiter_distinctly():
    rl = RateLimiter()
    assert rl.allow("login_ip", "203.0.113.5", max_calls=1, window_seconds=60.0, now=0.0)
    # A different address family/value must not collide with the first.
    assert rl.allow("login_ip", "2001:db8::1", max_calls=1, window_seconds=60.0, now=0.0)
    assert rl.size() == 2


# ======================================================================
# 12 — trusted-proxy / IP-extraction hop semantics
# ======================================================================
def test_trusted_proxy_hop_takes_rightmost_entry_not_client_supplied_prefix():
    """A client that forges its own X-Forwarded-For prefix only pushes the
    forgery further left; with exactly one trusted hop, the address that
    hop itself appended (rightmost) is what's trusted."""
    forged_then_real = "9.9.9.9, 203.0.113.5"  # attacker's fake, then the trusted proxy's real one
    assert resolve_client_ip(
        direct_peer="10.0.0.1", forwarded_for=forged_then_real, trusted_proxy_hops=1,
    ) == "203.0.113.5"


def test_trusted_proxy_hops_two_reads_second_from_right():
    chain = "9.9.9.9, 203.0.113.5, 198.51.100.9"
    assert resolve_client_ip(
        direct_peer="10.0.0.1", forwarded_for=chain, trusted_proxy_hops=2,
    ) == "203.0.113.5"


def test_trusted_proxy_hops_falls_back_when_chain_too_short():
    assert resolve_client_ip(
        direct_peer="10.0.0.1", forwarded_for="203.0.113.5", trusted_proxy_hops=3,
    ) == "10.0.0.1"


# ======================================================================
# register() follow-up fix (found by independent adversarial review of
# this same slice): must not be hard-lockable by email alone either.
# ======================================================================
def test_attacker_cannot_block_victim_registration_by_email_alone():
    d = _db()
    cfg = AuthConfig(register_max_attempts=3, register_window_seconds=60.0)

    for _ in range(3):
        with pytest.raises(AuthError):
            auth_service.register(d, email=VICTIM_EMAIL, password="x",  # fails password policy
                                   client_ip="203.0.113.9", now=NOW, cfg=cfg)
    with pytest.raises(RateLimitedError):
        auth_service.register(d, email=VICTIM_EMAIL, password="x",
                               client_ip="203.0.113.9", now=NOW, cfg=cfg)

    # The real registrant, from their own IP, is unaffected.
    user_id = auth_service.register(d, email=VICTIM_EMAIL, password="correct horse battery",
                                     client_ip="198.51.100.7", now=NOW, cfg=cfg)
    assert user_id


# ======================================================================
# Ops shared-token exchange: brute-force guard (Slice 9 follow-up finding -
# the same volume gap, on a different, higher-privilege login endpoint)
# ======================================================================
def test_ops_login_is_rate_limited_per_ip(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import detoura.persistence.db as _db
    from detoura.api.app import create_app

    monkeypatch.setenv("DETOURA_OPS_TOKEN", "s3cr3t-ops-token")
    monkeypatch.setenv("OPS_LOGIN_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("OPS_LOGIN_WINDOW_SECONDS", "60")
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "ops_rl.db"))
    monkeypatch.setattr(_db, "_DB", None)
    client = TestClient(create_app())

    for _ in range(3):
        r = client.post("/api/v1/ops/session", json={"token": "wrong-guess"})
        assert r.status_code == 401
    r = client.post("/api/v1/ops/session", json={"token": "wrong-guess"})
    assert r.status_code == 429
    # Even the CORRECT token is refused once the IP's budget is spent - the
    # cap is on volume from this source, not on wrong guesses specifically.
    r = client.post("/api/v1/ops/session", json={"token": "s3cr3t-ops-token"})
    assert r.status_code == 429


def test_ops_login_rate_limit_does_not_leak_across_reconfiguration_before_enabled_check(monkeypatch, tmp_path):
    """The rate-limit check must not itself become an oracle: an unconfigured
    deployment (no DETOURA_OPS_TOKEN) still answers 503 first, not 429 or
    401 - see ops.py ordering (ops_enabled() checked before the limiter)."""
    from fastapi.testclient import TestClient

    import detoura.persistence.db as _db
    from detoura.api.app import create_app

    monkeypatch.delenv("DETOURA_OPS_TOKEN", raising=False)
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "ops_rl2.db"))
    monkeypatch.setattr(_db, "_DB", None)
    client = TestClient(create_app())
    r = client.post("/api/v1/ops/session", json={"token": "anything"})
    assert r.status_code == 503


# ======================================================================
# API-level: the endpoint actually resolves and uses request IP
# ======================================================================
def test_login_endpoint_uses_forwarded_header_only_when_configured(monkeypatch):
    from detoura import auth_config as auth_config_mod
    from detoura.api.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_LOGIN_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("AUTH_LOGIN_WINDOW_SECONDS", "60")
    monkeypatch.setenv("AUTH_TRUSTED_PROXY_HOPS", "1")
    auth_config_mod.reset_auth_config()
    try:
        client = TestClient(create_app())
        client.post("/api/v1/auth/register", json={"email": VICTIM_EMAIL, "password": "correct horse battery"})

        headers_a = {"X-Forwarded-For": "203.0.113.1"}
        headers_b = {"X-Forwarded-For": "203.0.113.2"}
        for _ in range(2):
            r = client.post("/api/v1/auth/login",
                             json={"email": VICTIM_EMAIL, "password": "bad"}, headers=headers_a)
            assert r.status_code == 401
        r = client.post("/api/v1/auth/login",
                         json={"email": VICTIM_EMAIL, "password": "bad"}, headers=headers_a)
        assert r.status_code == 429

        # A different forwarded IP is a different (ip, email) pair - not
        # pre-throttled by IP A's failures.
        r = client.post("/api/v1/auth/login",
                         json={"email": VICTIM_EMAIL, "password": "correct horse battery"},
                         headers=headers_b)
        assert r.status_code == 200
    finally:
        auth_config_mod.reset_auth_config()
