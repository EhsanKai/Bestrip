"""V9 Phase 2.6 Part A — the auth + My Trips HTTP API: cookies, CSRF,
session lifecycle, cross-user object authorization, anonymous compatibility
(§A4-A9, §A11)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from detoura.api.app import create_app
from detoura.persistence import accounts as store
from detoura.persistence import bookings as booking_store
from detoura.persistence import get_db
from detoura.persistence.bookings import BookingRecord
from detoura.services.rate_limit import rate_limiter

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    rate_limiter().clear()
    yield
    rate_limiter().clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "auth.db"))
    import detoura.persistence.db as _db
    monkeypatch.setattr(_db, "_DB", None)
    return TestClient(create_app())


def _register_and_login(client, email="user@example.com", password="correct horse battery"):
    client.post("/api/v1/auth/register", json={"email": email, "password": password})
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200
    return r.json()["user_id"]


# ======================================================================
# Registration / login / logout / me
# ======================================================================
def test_register_then_login_then_me(client):
    uid = _register_and_login(client)
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 200 and r.json()["user_id"] == uid


def test_duplicate_registration_rejected(client):
    client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "correct horse battery"})
    r = client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "another password 1"})
    assert r.status_code == 400


def test_wrong_password_login_rejected_generically(client):
    client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "correct horse battery"})
    r = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "wrong-one-here-999"})
    assert r.status_code == 401
    r2 = client.post("/api/v1/auth/login", json={"email": "nope@example.com", "password": "wrong-one-here-999"})
    assert r2.status_code == 401
    assert r.json() == r2.json()  # identical body - no enumeration signal


def test_anonymous_me_is_200_with_null_not_401(client):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json() is None


def test_session_cookie_is_httponly_csrf_cookie_is_not(client):
    _register_and_login(client)
    # httpx/starlette TestClient exposes raw Set-Cookie headers via .cookies
    # for value access; assert on the jar's httponly flag via the cookiejar.
    jar = client.cookies.jar
    session_cookie = next(c for c in jar if c.name == "detoura_session")
    csrf_cookie = next(c for c in jar if c.name == "detoura_csrf")
    assert session_cookie.has_nonstandard_attr("HttpOnly") or session_cookie._rest.get("HttpOnly") is not None
    assert not (csrf_cookie.has_nonstandard_attr("HttpOnly") or csrf_cookie._rest.get("HttpOnly") is not None)


def test_logout_without_csrf_header_is_rejected(client):
    _register_and_login(client)
    r = client.post("/api/v1/auth/logout")
    assert r.status_code == 403


def test_logout_with_wrong_csrf_value_is_rejected(client):
    _register_and_login(client)
    r = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "not-the-real-token"})
    assert r.status_code == 403
    # session must still be alive - a failed CSRF check must not itself log out
    r2 = client.get("/api/v1/auth/me")
    assert r2.status_code == 200 and r2.json() is not None


def test_logout_with_correct_csrf_succeeds_and_session_dies(client):
    _register_and_login(client)
    csrf = client.cookies.get("detoura_csrf")
    r = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    r2 = client.get("/api/v1/auth/me")
    assert r2.json() is None


def test_session_token_never_appears_in_any_json_response_body(client):
    r = client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "correct horse battery"})
    assert "password" not in r.text.lower() or "password_hash" not in r.text
    r2 = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "correct horse battery"})
    raw_session = client.cookies.get("detoura_session")
    assert raw_session not in r2.text


# ======================================================================
# §A7 — abuse controls via the API
# ======================================================================
def test_login_rate_limited_after_repeated_failures(client, monkeypatch):
    from detoura import auth_config as auth_config_mod
    auth_config_mod.reset_auth_config()
    monkeypatch.setenv("AUTH_LOGIN_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("AUTH_LOGIN_WINDOW_SECONDS", "60")
    auth_config_mod.reset_auth_config()
    client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "correct horse battery"})
    for _ in range(3):
        r = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "bad-pw-attempt-1"})
        assert r.status_code == 401
    r = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "bad-pw-attempt-1"})
    assert r.status_code == 429
    auth_config_mod.reset_auth_config()


# ======================================================================
# §A9 — My Trips ownership
# ======================================================================
def _seed_booking(booking_id: str, label: str) -> None:
    db = get_db()
    booking_store.upsert(db, BookingRecord(
        booking_id=booking_id, journey_reference=f"J-{booking_id}", created_at=NOW, updated_at=NOW,
        mode="SMART", phase="CONFIRMED", trip_label=label,
    ))


def test_anonymous_cannot_list_or_read_trips(client):
    assert client.get("/api/v1/me/trips").status_code == 401
    assert client.get("/api/v1/me/trips/anything").status_code == 401


def test_user_a_reads_own_trip_not_user_b_trip(client):
    uid_a = _register_and_login(client, email="alice@example.com")
    db = get_db()
    _seed_booking("bk_a", "Alice trip")
    store.claim_trip(db, user_id=uid_a, booking_id="bk_a", now=NOW)

    r = client.get("/api/v1/me/trips")
    assert r.status_code == 200
    assert [t["booking_id"] for t in r.json()["trips"]] == ["bk_a"]

    r2 = client.get("/api/v1/me/trips/bk_a")
    assert r2.status_code == 200 and r2.json()["booking_id"] == "bk_a"

    csrf = client.cookies.get("detoura_csrf")
    client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

    uid_b = _register_and_login(client, email="bob@example.com")
    _seed_booking("bk_b", "Bob trip")
    store.claim_trip(db, user_id=uid_b, booking_id="bk_b", now=NOW)

    r3 = client.get("/api/v1/me/trips")
    assert [t["booking_id"] for t in r3.json()["trips"]] == ["bk_b"]

    r4 = client.get("/api/v1/me/trips/bk_a")  # bob reading alice's trip
    assert r4.status_code == 404

    r5 = client.get("/api/v1/me/trips/does-not-exist-at-all")
    assert r5.status_code == 404
    assert r5.json() == r4.json()  # identical shape - no ownership leak via response difference


def test_disabled_account_cannot_use_a_live_session_for_my_trips(client):
    uid = _register_and_login(client)
    db = get_db()
    store.set_status(db, uid, "DISABLED", now=NOW)
    r = client.get("/api/v1/me/trips")
    assert r.status_code == 401


# ======================================================================
# Anonymous compatibility (§A: "anonymous search must remain fully functional")
# ======================================================================
def test_health_and_core_api_reachable_without_any_session(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200


def test_anonymous_search_endpoint_still_works(client):
    from detoura.data.destinations import CORE_DESTINATIONS
    body = {
        "origin": "Köln", "budget": 3000.0, "travelers": 1, "duration_days": 4,
        "date_from": "2026-10-01", "date_to": "2026-10-20",
        "preferences": {"history": 0.7, "culture": 0.7},
    }
    r = client.post("/api/v1/search", json=body)
    assert r.status_code == 200


# ======================================================================
# UserAccount is not Traveler (V9 Phase 2.6 §A1, §A11)
# ======================================================================
def test_user_account_model_has_no_traveler_fields():
    from detoura.models.account import UserAccount
    fields = set(UserAccount.model_fields)
    for bad in ("given_name", "family_name", "born_on", "phone", "passport_number",
                "nationality", "passport_issuing_country", "document_type"):
        assert bad not in fields


def test_traveler_model_has_no_account_fields():
    from detoura.models.traveler import Traveler
    fields = set(Traveler.model_fields)
    for bad in ("user_id", "password_hash", "email_normalized"):
        assert bad not in fields


# ======================================================================
# V9 account/auth security audit: a 422 validation error must never echo a
# submitted password back in its response body. FastAPI's default handler
# for RequestValidationError puts the raw invalid value in each error's
# "input" - harmless for most fields, but a password that merely exceeds the
# max-length check came back verbatim in the response otherwise.
# ======================================================================
def test_register_oversized_password_422_does_not_echo_password(client):
    secret_marker = "AuditRegressionSecretMarker123!"
    oversized = secret_marker + ("a" * 1200)  # exceeds RegisterRequest.password max_length=1000
    r = client.post("/api/v1/auth/register", json={"email": "audit-422@example.com", "password": oversized})
    assert r.status_code == 422
    assert secret_marker not in r.text
    detail = r.json()["detail"]
    assert any(err.get("loc") == ["body", "password"] and "input" not in err for err in detail)


def test_login_oversized_password_422_does_not_echo_password(client):
    secret_marker = "AuditRegressionLoginMarker456!"
    oversized = secret_marker + ("a" * 1200)
    r = client.post("/api/v1/auth/login", json={"email": "audit-422@example.com", "password": oversized})
    assert r.status_code == 422
    assert secret_marker not in r.text


def test_non_sensitive_field_validation_error_still_echoes_input(client):
    """The redaction is scoped to sensitive field names - an ordinary field
    (email) still reports its invalid value, since there is nothing to
    protect and the client needs to see what it sent."""
    r = client.post("/api/v1/auth/register", json={"email": "x", "password": "a-fine-password"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any(err.get("loc") == ["body", "email"] and err.get("input") == "x" for err in detail)
