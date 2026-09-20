"""V9 CSRF Hardening slice.

Closes the one deferred Medium finding from the Real Provider E2E audit:
CSRF enforcement was inconsistent across authenticated booking/payment
mutation routes. Two gaps, both now closed the same way ``confirm_payment``
and ``refund_payment`` already worked (§A6's double-submit check, only when
a session is actually present - the legacy anonymous flow stays untouched):

* ``POST /api/v1/payments`` (``payments.py::create_payment``) - created a
  payment, attributable to a signed-in caller's account, without ever
  calling ``require_csrf``.
* ``POST /api/v1/booking-intents`` (``v1.py::create_booking_intent``) -
  attributes the resulting trip's ownership to a signed-in caller's session,
  again without ``require_csrf``.

Every other booking-intent endpoint (travelers, commercial options,
confirm, ticket start/mark) is deliberately left alone: none of them read
the session cookie at all (see ``_booking_or_404``) - they are
capability-by-``booking_id``, exactly like the payment webhook, so CSRF
does not apply without redesigning that intentional authorization model
(out of scope for this slice - see ``docs/V9_CSRF_HARDENING_REPORT.md``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from detoura.api.app import create_app
from detoura.models.commercial import CommercialQuote, PriceBreakdown, PricingPolicyRef, ServiceTier
from detoura.models.travel_pass import PassMode
from detoura.persistence import accounts as account_store
from detoura.persistence import get_db
from detoura.persistence import payments as payment_store
from detoura.services.booking_flow import booking_store
from detoura.services.booking_orchestrator import BookingRun


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "csrf.db"))
    monkeypatch.delenv("DETOURA_OPS_TOKEN", raising=False)
    import detoura.persistence.db as _db
    monkeypatch.setattr(_db, "_DB", None)
    import detoura.payment_config as _pc
    _pc.reset_payment_config()
    return TestClient(create_app())


def _register_and_login(client, email, password="correct horse battery") -> str:
    client.post("/api/v1/auth/register", json={"email": email, "password": password})
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["user_id"]


def _csrf_headers(client) -> dict:
    csrf = client.cookies.get("detoura_csrf")
    return {"X-CSRF-Token": csrf} if csrf else {}


def _quote(total: float = 150.0) -> CommercialQuote:
    return CommercialQuote(
        service_tier=ServiceTier.BASIC,
        breakdown=PriceBreakdown(currency="EUR", supplier_transport=total),
        markup_policy=PricingPolicyRef(policy_id="default", version=1),
    )


def _seed_booking(*, booking_id: str, user_key: str = "anonymous", total: float = 150.0) -> None:
    from detoura.models.booking import PriceTolerance

    run = BookingRun(
        booking_id=booking_id, journey_reference=f"jr_{booking_id}", mode=PassMode.DEMO_ONLY,
        trip_label="t", route_cities=("BER", "LHR"), currency="EUR",
        discovered_total=total, tolerance=PriceTolerance(absolute=5.0, percentage=5.0),
        items=[], quote=_quote(total), user_key=user_key,
    )
    booking_store().put(run)


def _demo_booking_body(label="T"):
    dep = (datetime.now(timezone.utc) + timedelta(days=20)).replace(microsecond=0)
    return {
        "demo_trip_label": label, "demo_currency": "EUR", "demo_travelers": 1,
        "demo_legs": [{
            "origin": "CGN", "destination": "PRG", "departure": dep.isoformat(),
            "arrival": (dep + timedelta(hours=1)).isoformat(), "carrier": "OK",
            "flight_number": "1", "price_per_person": 95.8,
            "cabin": "included", "checked": "unknown",
        }],
        "service_tier": "ALL_IN_ONE",
    }


# ======================================================================
# POST /api/v1/payments - create_payment
# ======================================================================
def test_create_payment_signed_in_missing_csrf_is_rejected_before_any_payment_exists(client):
    uid = _register_and_login(client, "pay_missing@example.com")
    _seed_booking(booking_id="bk_pay_missing", user_key=uid)
    r = client.post(
        "/api/v1/payments", json={"booking_id": "bk_pay_missing", "idempotency_key": "idem_pay_missing1"},
    )
    assert r.status_code == 403
    # Fails closed: no payment side effect from the rejected request.
    assert payment_store.list_payments_for_booking(get_db(), "bk_pay_missing") == []


def test_create_payment_signed_in_invalid_csrf_is_rejected(client):
    uid = _register_and_login(client, "pay_invalid@example.com")
    _seed_booking(booking_id="bk_pay_invalid", user_key=uid)
    r = client.post(
        "/api/v1/payments", json={"booking_id": "bk_pay_invalid", "idempotency_key": "idem_pay_invalid1"},
        headers={"X-CSRF-Token": "not-the-real-token"},
    )
    assert r.status_code == 403
    assert payment_store.list_payments_for_booking(get_db(), "bk_pay_invalid") == []


def test_create_payment_signed_in_valid_csrf_succeeds(client):
    uid = _register_and_login(client, "pay_valid@example.com")
    _seed_booking(booking_id="bk_pay_valid", user_key=uid)
    r = client.post(
        "/api/v1/payments", json={"booking_id": "bk_pay_valid", "idempotency_key": "idem_pay_valid1"},
        headers=_csrf_headers(client),
    )
    assert r.status_code == 200, r.text
    assert len(payment_store.list_payments_for_booking(get_db(), "bk_pay_valid")) == 1


def test_create_payment_anonymous_needs_no_csrf(client):
    """The legacy anonymous flow (§S) must keep working untouched - no
    session means no CSRF cookie exists to check in the first place."""
    _seed_booking(booking_id="bk_pay_anon")
    r = client.post(
        "/api/v1/payments", json={"booking_id": "bk_pay_anon", "idempotency_key": "idem_pay_anon1"},
    )
    assert r.status_code == 200, r.text


# ======================================================================
# POST /api/v1/booking-intents - create_booking_intent
# ======================================================================
def test_create_booking_intent_signed_in_missing_csrf_is_rejected_before_any_ownership_exists(client):
    uid = _register_and_login(client, "bk_missing@example.com")
    r = client.post("/api/v1/booking-intents", json=_demo_booking_body())
    assert r.status_code == 403
    # Fails closed: no booking_id is even minted, so nothing to own.
    assert "booking_id" not in r.json()
    assert account_store.list_trip_ids_for_user(get_db(), uid) == []


def test_create_booking_intent_signed_in_invalid_csrf_is_rejected(client):
    uid = _register_and_login(client, "bk_invalid@example.com")
    r = client.post(
        "/api/v1/booking-intents", json=_demo_booking_body(), headers={"X-CSRF-Token": "forged"},
    )
    assert r.status_code == 403
    assert account_store.list_trip_ids_for_user(get_db(), uid) == []


def test_create_booking_intent_signed_in_valid_csrf_succeeds_and_is_owned(client):
    uid = _register_and_login(client, "bk_valid@example.com")
    r = client.post(
        "/api/v1/booking-intents", json=_demo_booking_body(), headers=_csrf_headers(client),
    )
    assert r.status_code == 201, r.text
    booking_id = r.json()["booking_id"]
    assert account_store.get_trip_owner(get_db(), booking_id) == uid


def test_create_booking_intent_anonymous_needs_no_csrf_and_stays_unowned(client):
    r = client.post("/api/v1/booking-intents", json=_demo_booking_body())
    assert r.status_code == 201, r.text
    booking_id = r.json()["booking_id"]
    assert account_store.get_trip_owner(get_db(), booking_id) is None


def test_create_booking_intent_cross_site_forgery_cannot_plant_a_trip_on_the_victims_account(client):
    """The concrete attack this closes: a malicious page auto-submits this
    request with the victim's ambient session cookie attached (as a browser
    would) but obviously can't also know/forge the victim's CSRF cookie
    value (a different, unguessable per-session token) - so it fails
    closed instead of quietly attaching a trip to the victim's account."""
    uid = _register_and_login(client, "victim@example.com")
    forged = client.post(
        "/api/v1/booking-intents", json=_demo_booking_body("forged"),
        headers={"X-CSRF-Token": "attacker-guessed-value"},
    )
    assert forged.status_code == 403
    assert account_store.list_trip_ids_for_user(get_db(), uid) == []
