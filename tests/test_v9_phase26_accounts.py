"""V9 Phase 2.6 Part A — account domain, password hashing, email
normalization, persistence, and the auth service layer (§A1-A5, §A7, §A11)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from detoura.auth_config import AuthConfig
from detoura.persistence import accounts as store
from detoura.persistence.db import Database
from detoura.services import auth_service
from detoura.services.auth_service import AuthError, RateLimitedError
from detoura.services.email_normalization import InvalidEmail, normalize_email
from detoura.services.password_hashing import (
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    verify_password,
)
from detoura.services.rate_limit import RateLimiter

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _db() -> Database:
    return Database(":memory:")


# ======================================================================
# §A3 — email normalization
# ======================================================================
def test_normalize_trims_and_lowercases_domain():
    assert normalize_email("  Ada@Example.COM  ") == "ada@example.com"


def test_normalize_lowercases_local_part_too():
    assert normalize_email("Ada.Lovelace@EXAMPLE.com") == "ada.lovelace@example.com"


def test_normalize_does_not_strip_plus_alias():
    assert normalize_email("ada+travel@example.com") == "ada+travel@example.com"


def test_normalize_does_not_remove_gmail_dots():
    assert normalize_email("a.d.a@gmail.com") == "a.d.a@gmail.com"


def test_normalize_rejects_garbage():
    for bad in ("not-an-email", "", "   ", "@example.com", "ada@", "ada"):
        with pytest.raises(InvalidEmail):
            normalize_email(bad)


# ======================================================================
# §A2 — password hashing
# ======================================================================
def test_hash_is_not_plaintext_and_is_argon2id():
    h = hash_password("correct horse battery staple")
    assert "correct horse" not in h
    assert h.startswith("$argon2id$")


def test_verify_correct_and_wrong_password():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong password entirely", h) is False


def test_verify_never_raises_on_malformed_hash():
    assert verify_password("anything", "not-a-real-hash") is False
    assert verify_password("anything", "") is False


def test_password_length_policy():
    with pytest.raises(PasswordPolicyError):
        hash_password("short")
    with pytest.raises(PasswordPolicyError):
        hash_password("x" * 1000)  # pathological input, rejected before hashing


def test_needs_rehash_is_false_for_a_freshly_hashed_password():
    h = hash_password("correct horse battery staple")
    assert needs_rehash(h) is False


def test_two_hashes_of_the_same_password_differ_salt_is_random():
    h1 = hash_password("correct horse battery staple")
    h2 = hash_password("correct horse battery staple")
    assert h1 != h2  # per-hash random salt, not a lookup-table-friendly digest


# ======================================================================
# §A1 — account persistence
# ======================================================================
def test_create_and_fetch_user():
    d = _db()
    uid = store.create_user(d, email_normalized="a@example.com", password_hash="h", now=NOW)
    user = store.get_user(d, uid)
    assert user["email_normalized"] == "a@example.com"
    assert user["status"] == "ACTIVE"
    assert user["password_hash"] == "h"


def test_duplicate_email_rejected():
    d = _db()
    store.create_user(d, email_normalized="a@example.com", password_hash="h1", now=NOW)
    with pytest.raises(store.DuplicateEmail):
        store.create_user(d, email_normalized="a@example.com", password_hash="h2", now=NOW)


def test_user_id_is_not_derived_from_email():
    d = _db()
    uid1 = store.create_user(d, email_normalized="a@example.com", password_hash="h", now=NOW)
    d2 = _db()
    uid2 = store.create_user(d2, email_normalized="a@example.com", password_hash="h", now=NOW)
    assert uid1 != uid2  # two independent accounts never collide on id


def test_disable_account():
    d = _db()
    uid = store.create_user(d, email_normalized="a@example.com", password_hash="h", now=NOW)
    store.set_status(d, uid, "DISABLED", now=NOW)
    assert store.get_user(d, uid)["status"] == "DISABLED"


# ======================================================================
# §A5 — sessions
# ======================================================================
def test_session_created_stores_only_a_hash_of_the_token():
    d = _db()
    uid = store.create_user(d, email_normalized="a@example.com", password_hash="h", now=NOW)
    raw = store.new_session_token()
    session_id = store.create_session(d, user_id=uid, token_hash=store.hash_token(raw),
                                      csrf_token_hash="x", ttl_seconds=3600, now=NOW)
    row = d.query_one("SELECT * FROM auth_sessions WHERE session_id=?", (session_id,))
    assert raw not in row["token_hash"]
    assert row["token_hash"] == store.hash_token(raw)


def test_expired_session_is_not_found_as_valid_by_the_service():
    d = _db()
    uid = store.create_user(d, email_normalized="a@example.com",
                            password_hash=hash_password("correct horse battery"), now=NOW)
    raw = store.new_session_token()
    store.create_session(d, user_id=uid, token_hash=store.hash_token(raw),
                         csrf_token_hash="x", ttl_seconds=1, now=NOW)
    later = NOW + timedelta(seconds=5)
    assert auth_service.validate_session(d, raw_token=raw, now=later) is None


def test_revoked_session_is_invalid():
    d = _db()
    uid = store.create_user(d, email_normalized="a@example.com", password_hash="h", now=NOW)
    raw = store.new_session_token()
    session_id = store.create_session(d, user_id=uid, token_hash=store.hash_token(raw),
                                      csrf_token_hash="x", ttl_seconds=3600, now=NOW)
    assert auth_service.validate_session(d, raw_token=raw, now=NOW) is not None
    store.revoke_session(d, session_id, now=NOW)
    assert auth_service.validate_session(d, raw_token=raw, now=NOW) is None


def test_disabled_account_invalidates_an_otherwise_live_session():
    d = _db()
    uid = store.create_user(d, email_normalized="a@example.com", password_hash="h", now=NOW)
    raw = store.new_session_token()
    store.create_session(d, user_id=uid, token_hash=store.hash_token(raw),
                         csrf_token_hash="x", ttl_seconds=3600, now=NOW)
    store.set_status(d, uid, "DISABLED", now=NOW)
    assert auth_service.validate_session(d, raw_token=raw, now=NOW) is None


def test_unknown_token_is_invalid():
    d = _db()
    assert auth_service.validate_session(d, raw_token="not-a-real-token", now=NOW) is None


# ======================================================================
# §A4 — the service layer: generic failures, enumeration resistance
# ======================================================================
def test_login_failure_message_identical_for_unknown_account_and_wrong_password():
    d = _db()
    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    msg_unknown = msg_wrong = None
    try:
        auth_service.login(d, email="nope@example.com", password="whatever12", now=NOW)
    except AuthError as e:
        msg_unknown = str(e)
    try:
        auth_service.login(d, email="a@example.com", password="totally wrong", now=NOW)
    except AuthError as e:
        msg_wrong = str(e)
    assert msg_unknown == msg_wrong == auth_service.GENERIC_LOGIN_FAILURE


def test_login_rejects_disabled_account_with_the_same_generic_message():
    d = _db()
    uid = auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    store.set_status(d, uid, "DISABLED", now=NOW)
    with pytest.raises(AuthError, match=auth_service.GENERIC_LOGIN_FAILURE):
        auth_service.login(d, email="a@example.com", password="correct horse battery", now=NOW)


def test_successful_login_issues_a_usable_session():
    d = _db()
    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    result = auth_service.login(d, email="a@example.com", password="correct horse battery", now=NOW)
    ctx = auth_service.validate_session(d, raw_token=result.raw_token, now=NOW)
    assert ctx is not None and ctx.user_id == result.user_id


def test_logout_revokes_the_session():
    d = _db()
    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    result = auth_service.login(d, email="a@example.com", password="correct horse battery", now=NOW)
    auth_service.logout(d, session_id=result.session_id, user_id=result.user_id)
    assert auth_service.validate_session(d, raw_token=result.raw_token, now=NOW) is None


# ======================================================================
# §A7 — abuse / rate limiting
# ======================================================================
def test_repeated_invalid_logins_are_rate_limited():
    d = _db()
    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    cfg = AuthConfig(login_max_attempts=3, login_window_seconds=60.0)
    from detoura.services.rate_limit import rate_limiter
    rate_limiter().clear()
    for _ in range(3):
        with pytest.raises(AuthError):
            auth_service.login(d, email="a@example.com", password="wrong", now=NOW, cfg=cfg)
    with pytest.raises(RateLimitedError):
        auth_service.login(d, email="a@example.com", password="wrong", now=NOW, cfg=cfg)
    rate_limiter().clear()


def test_legitimate_user_still_usable_after_a_few_failed_attempts():
    d = _db()
    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    cfg = AuthConfig(login_max_attempts=5, login_window_seconds=60.0)
    from detoura.services.rate_limit import rate_limiter
    rate_limiter().clear()
    for _ in range(2):
        with pytest.raises(AuthError):
            auth_service.login(d, email="a@example.com", password="wrong", now=NOW, cfg=cfg)
    result = auth_service.login(d, email="a@example.com", password="correct horse battery", now=NOW, cfg=cfg)
    assert result.user_id
    rate_limiter().clear()


def test_repeated_registration_attempts_are_rate_limited():
    d = _db()
    cfg = AuthConfig(register_max_attempts=2, register_window_seconds=60.0)
    from detoura.services.rate_limit import rate_limiter
    rate_limiter().clear()
    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW, cfg=cfg)
    try:
        auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW, cfg=cfg)
    except AuthError:
        pass  # duplicate - also fine, still counts as an attempt
    with pytest.raises(RateLimitedError):
        auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW, cfg=cfg)
    rate_limiter().clear()


def test_rate_limiter_primitive_windows_reset():
    rl = RateLimiter()
    assert rl.allow("s", "k", max_calls=2, window_seconds=10, now=0.0)
    assert rl.allow("s", "k", max_calls=2, window_seconds=10, now=1.0)
    assert not rl.allow("s", "k", max_calls=2, window_seconds=10, now=2.0)
    assert rl.allow("s", "k", max_calls=2, window_seconds=10, now=11.0)  # new window


# ======================================================================
# §A10 — audit, no PII/secrets leaked
# ======================================================================
def test_audit_events_recorded_without_password_or_token():
    d = _db()
    uid = auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    result = auth_service.login(d, email="a@example.com", password="correct horse battery", now=NOW)
    auth_service.logout(d, session_id=result.session_id, user_id=uid)
    attempted_password = "definitely-not-the-real-password-987"
    try:
        auth_service.login(d, email="a@example.com", password=attempted_password, now=NOW)
    except AuthError:
        pass

    rows = d.query("SELECT action, before_json, after_json, note FROM audit_events")
    actions = {r["action"] for r in rows}
    assert {"account_created", "login_success", "logout", "login_failure"} <= actions
    blob = " ".join(f"{r['action']} {r['before_json']} {r['after_json']} {r['note']}" for r in rows).lower()
    for bad in ("correct horse battery", attempted_password, result.raw_token, result.raw_csrf_token):
        assert bad.lower() not in blob


def test_no_pii_or_secret_columns_in_account_tables():
    d = _db()
    for tbl in ("user_accounts", "auth_sessions", "trip_ownership"):
        cols = {r["name"] for r in d.query(f"PRAGMA table_info({tbl})")}
        for bad in ("phone", "passport", "dob", "date_of_birth", "address",
                    "nationality", "given_name", "family_name"):
            assert not any(bad in c for c in cols), (tbl, bad)
    # the raw token/password never has its OWN column named plainly "token"/"password"
    session_cols = {r["name"] for r in d.query("PRAGMA table_info(auth_sessions)")}
    assert "token" not in session_cols and "raw_token" not in session_cols
    assert "token_hash" in session_cols


# ======================================================================
# §A8 — trip ownership
# ======================================================================
def test_claim_trip_is_idempotent_for_the_same_user():
    d = _db()
    assert store.claim_trip(d, user_id="u1", booking_id="bk1", now=NOW) is True
    assert store.claim_trip(d, user_id="u1", booking_id="bk1", now=NOW) is True
    assert store.get_trip_owner(d, "bk1") == "u1"


def test_claim_trip_never_overwrites_a_different_owner():
    d = _db()
    store.claim_trip(d, user_id="u1", booking_id="bk1", now=NOW)
    assert store.claim_trip(d, user_id="u2", booking_id="bk1", now=NOW) is False
    assert store.get_trip_owner(d, "bk1") == "u1"


def test_unclaimed_booking_has_no_owner_anonymous_journeys_stay_anonymous():
    d = _db()
    assert store.get_trip_owner(d, "some-anonymous-booking") is None


def test_list_trip_ids_for_user_only_returns_that_users_trips():
    d = _db()
    store.claim_trip(d, user_id="alice", booking_id="bk_a1", now=NOW)
    store.claim_trip(d, user_id="alice", booking_id="bk_a2", now=NOW)
    store.claim_trip(d, user_id="bob", booking_id="bk_b1", now=NOW)
    assert set(store.list_trip_ids_for_user(d, "alice")) == {"bk_a1", "bk_a2"}
    assert set(store.list_trip_ids_for_user(d, "bob")) == {"bk_b1"}
