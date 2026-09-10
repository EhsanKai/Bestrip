"""V8.5 C3 — post-booking ticket operations (cancellation, change, recovery).

Integration tests around the real ops endpoints and the ticket_operations
service, with a scripted fake Duffel for the provider steps.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from detoura.api.app import create_app
from detoura.providers.duffel import DuffelTransportProvider
from detoura.providers.http import HttpResponse


# --------------------------------------------------------------------------
class FakeDuffel:
    """Scripts the Duffel endpoints the ticket-ops service touches."""

    def __init__(self, *, refund="20.00", confirm=True, order_changeable=True,
                 cancel_unsupported=False):
        self.refund = refund
        self.confirm = confirm
        self.order_changeable = order_changeable
        self.cancel_unsupported = cancel_unsupported
        self.calls: list[str] = []

    def request(self, method, url, *, headers=None, params=None, body=None,
                timeout=10.0):
        self.calls.append(f"{method} {url}")
        if "/air/order_cancellations/" in url and url.endswith("/actions/confirm"):
            if not self.confirm:
                return HttpResponse(status=500, body='{"errors":[{"title":"boom"}]}')
            return HttpResponse(status=200, body=json.dumps({"data": {
                "id": "ore_1", "live_mode": False, "confirmed_at": "2026-01-01T00:00:00Z",
                "refund_amount": self.refund, "refund_currency": "EUR",
            }}))
        if url.endswith("/air/order_cancellations"):
            if self.cancel_unsupported:
                return HttpResponse(status=422, body='{"errors":[{"code":"not_cancellable","title":"cannot"}]}')
            return HttpResponse(status=201, body=json.dumps({"data": {
                "id": "ore_1", "live_mode": False, "order_id": "ord_abc",
                "refund_amount": self.refund, "refund_currency": "EUR",
                "refund_to": "original_form_of_payment",
            }}))
        if "/air/orders/" in url:
            return HttpResponse(status=200, body=json.dumps({"data": {
                "id": "ord_abc", "live_mode": False,
                "available_actions": ["change", "cancel"] if self.order_changeable else ["cancel"],
                "changeable": self.order_changeable,
            }}))
        return HttpResponse(status=404, body='{"errors":[{"title":"nope"}]}')


def _fake_factory(fake):
    return lambda: DuffelTransportProvider(
        access_token="duffel_test_x", http_client=fake, max_calls=50,
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "c3.db"))
    monkeypatch.setenv("DETOURA_OPS_TOKEN", "adm")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_x")
    import detoura.persistence.db as _db
    monkeypatch.setattr(_db, "_DB", None)
    return TestClient(create_app())


@pytest.fixture
def H(client):
    tok = client.post("/api/v1/ops/session", json={"token": "adm"}).json()["session_token"]
    return {"Authorization": f"Bearer {tok}"}


def _seed_sandbox_booking(client, monkeypatch, *, order_id="ord_abc"):
    """Create a DEMO booking then force it to look sandbox-booked with a
    provider order id, so the ticket-ops path is exercised."""
    from detoura.services.booking_flow import booking_store, create_run_demo
    from detoura.services.booking_orchestrator import BookingPhase
    from detoura.models.booking import BookingState
    from detoura.models.travel_pass import PassMode
    from detoura.persistence import get_db
    from detoura.services.booking_persistence import persist_run

    run = create_run_demo(
        trip_label="Test", currency="EUR",
        legs=[{"origin": "CGN", "destination": "CDG",
               "departure": "2026-11-01T09:00:00Z", "arrival": "2026-11-01T10:10:00Z",
               "carrier": "LH", "flight_number": "42", "price_per_person": 80.0}],
    )
    run.mode = PassMode.SANDBOX_BOOKED
    run.items[0].provider = "duffel"
    run.items[0].offer_id = "off_x"
    run.items[0].provider_order_id = order_id
    run.items[0].state = BookingState.CONFIRMED
    run.items[0].carrier = "LH"
    run.items[0].carrier_name = "Lufthansa"
    run.phase = BookingPhase.COMPLETE
    booking_store().put(run)
    persist_run(run, get_db())
    return run.booking_id


def _seed_demo_booking(client):
    body = {
        "demo_currency": "EUR", "demo_travelers": 1, "service_tier": "BASIC",
        "demo_legs": [{"origin": "CGN", "destination": "CDG",
                       "departure": "2026-11-01T09:00:00Z",
                       "arrival": "2026-11-01T10:10:00Z", "carrier": "LH",
                       "flight_number": "1", "price_per_person": 80.0}],
    }
    return client.post("/api/v1/booking-intents", json=body).json()["booking_id"]


# ======================================================================
# Cancellation
# ======================================================================
def test_cancel_refundable(client, H, monkeypatch):
    fake = FakeDuffel(refund="80.00")
    monkeypatch.setattr("detoura.services.ticket_operations.duffel_for_ops",
                        _fake_factory(fake))
    bid = _seed_sandbox_booking(client, monkeypatch)

    r = client.post(f"/api/v1/ops/bookings/{bid}/tickets/1/cancellation/eligibility",
                    headers=H)
    assert r.status_code == 200, r.text
    op = r.json()
    assert op["state"] == "ELIGIBLE"
    assert op["quote"]["refund_amount"] == 80.0
    op_id = op["operation_id"]

    # cannot execute before approve
    assert client.post(f"/api/v1/ops/operations/{op_id}/cancellation/execute",
                       headers=H, json={}).status_code == 409

    client.post(f"/api/v1/ops/operations/{op_id}/cancellation/approve",
                headers=H, json={"reason": "customer request"})
    r = client.post(f"/api/v1/ops/operations/{op_id}/cancellation/execute",
                    headers=H, json={})
    assert r.status_code == 200
    res = r.json()
    assert res["state"] == "REFUNDED"
    assert res["result"]["refund_status"] == "FULL"


def test_cancel_non_refundable(client, H, monkeypatch):
    fake = FakeDuffel(refund="0.00")
    monkeypatch.setattr("detoura.services.ticket_operations.duffel_for_ops",
                        _fake_factory(fake))
    bid = _seed_sandbox_booking(client, monkeypatch)
    op = client.post(f"/api/v1/ops/bookings/{bid}/tickets/1/cancellation/eligibility",
                     headers=H).json()
    op_id = op["operation_id"]
    client.post(f"/api/v1/ops/operations/{op_id}/cancellation/approve", headers=H, json={})
    res = client.post(f"/api/v1/ops/operations/{op_id}/cancellation/execute",
                      headers=H, json={}).json()
    assert res["state"] == "NON_REFUNDABLE"
    assert res["result"]["refund_status"] == "NONE"


def test_cancel_refund_pending_is_not_refunded(client, H, monkeypatch):
    # provider confirms the cancellation but states no refund amount yet
    fake = FakeDuffel(refund=None)
    monkeypatch.setattr("detoura.services.ticket_operations.duffel_for_ops",
                        _fake_factory(fake))
    bid = _seed_sandbox_booking(client, monkeypatch)
    op = client.post(f"/api/v1/ops/bookings/{bid}/tickets/1/cancellation/eligibility",
                     headers=H).json()
    op_id = op["operation_id"]
    client.post(f"/api/v1/ops/operations/{op_id}/cancellation/approve", headers=H, json={})
    res = client.post(f"/api/v1/ops/operations/{op_id}/cancellation/execute",
                      headers=H, json={}).json()
    assert res["state"] == "REFUND_PENDING"          # cancelled…
    assert res["result"]["refund_status"] == "UNKNOWN"  # …but NOT refunded


def test_cancel_provider_failure_sets_recovery(client, H, monkeypatch):
    fake = FakeDuffel(confirm=False)
    monkeypatch.setattr("detoura.services.ticket_operations.duffel_for_ops",
                        _fake_factory(fake))
    bid = _seed_sandbox_booking(client, monkeypatch)
    op = client.post(f"/api/v1/ops/bookings/{bid}/tickets/1/cancellation/eligibility",
                     headers=H).json()
    op_id = op["operation_id"]
    client.post(f"/api/v1/ops/operations/{op_id}/cancellation/approve", headers=H, json={})
    res = client.post(f"/api/v1/ops/operations/{op_id}/cancellation/execute",
                      headers=H, json={}).json()
    assert res["state"] == "CANCELLATION_FAILED"
    detail = client.get(f"/api/v1/ops/bookings/{bid}", headers=H).json()
    assert detail["recovery_state"] == "CANCELLATION_FAILED"


def test_cancel_duplicate_execute_is_idempotent(client, H, monkeypatch):
    fake = FakeDuffel(refund="80.00")
    monkeypatch.setattr("detoura.services.ticket_operations.duffel_for_ops",
                        _fake_factory(fake))
    bid = _seed_sandbox_booking(client, monkeypatch)
    op = client.post(f"/api/v1/ops/bookings/{bid}/tickets/1/cancellation/eligibility",
                     headers=H).json()
    op_id = op["operation_id"]
    client.post(f"/api/v1/ops/operations/{op_id}/cancellation/approve", headers=H, json={})
    a = client.post(f"/api/v1/ops/operations/{op_id}/cancellation/execute",
                    headers=H, json={"idempotency_key": "k1"}).json()
    calls_after_first = len([c for c in fake.calls if "confirm" in c])
    b = client.post(f"/api/v1/ops/operations/{op_id}/cancellation/execute",
                    headers=H, json={"idempotency_key": "k1"}).json()
    assert a["state"] == b["state"] == "REFUNDED"
    # the provider confirm endpoint was not called a second time
    assert len([c for c in fake.calls if "confirm" in c]) == calls_after_first


def test_cancel_demo_booking_is_not_supported(client, H):
    bid = _seed_demo_booking(client)
    r = client.post(f"/api/v1/ops/bookings/{bid}/tickets/1/cancellation/eligibility",
                    headers=H)
    assert r.status_code == 200
    assert r.json()["state"] == "NOT_SUPPORTED_IN_DEMO"


# ======================================================================
# Change
# ======================================================================
def test_change_supported(client, H, monkeypatch):
    fake = FakeDuffel(order_changeable=True)
    monkeypatch.setattr("detoura.services.ticket_operations.duffel_for_ops",
                        _fake_factory(fake))
    bid = _seed_sandbox_booking(client, monkeypatch)
    r = client.post(f"/api/v1/ops/bookings/{bid}/tickets/1/change/capability", headers=H)
    assert r.status_code == 200
    assert r.json()["quote"]["capability"] == "ORDER_CHANGE"


def test_change_not_supported(client, H, monkeypatch):
    fake = FakeDuffel(order_changeable=False)
    monkeypatch.setattr("detoura.services.ticket_operations.duffel_for_ops",
                        _fake_factory(fake))
    bid = _seed_sandbox_booking(client, monkeypatch)
    r = client.post(f"/api/v1/ops/bookings/{bid}/tickets/1/change/capability", headers=H)
    assert r.json()["state"] == "NOT_SUPPORTED"
    assert r.json()["quote"]["capability"] == "NOT_SUPPORTED"


def test_change_execute_without_offer_is_blocked_not_faked(client, H, monkeypatch):
    fake = FakeDuffel(order_changeable=True)
    monkeypatch.setattr("detoura.services.ticket_operations.duffel_for_ops",
                        _fake_factory(fake))
    bid = _seed_sandbox_booking(client, monkeypatch)
    op = client.post(f"/api/v1/ops/bookings/{bid}/tickets/1/change/capability",
                     headers=H).json()
    op_id = op["operation_id"]
    client.post(f"/api/v1/ops/operations/{op_id}/change/approve", headers=H, json={})
    res = client.post(f"/api/v1/ops/operations/{op_id}/change/execute", headers=H, json={}).json()
    assert res["state"] == "FAILED"
    assert "change offer" in res["result"]["detail"].lower()


# ======================================================================
# Recovery
# ======================================================================
def test_recovery_flow_no_silent_autobook(client, H, monkeypatch):
    bid = _seed_sandbox_booking(client, monkeypatch)
    op = client.post(f"/api/v1/ops/bookings/{bid}/tickets/1/recovery",
                     headers=H, json={"reason": "leg pulled by airline"}).json()
    op_id = op["operation_id"]
    assert op["state"] == "INSPECTED"

    # cannot approve before a candidate is compared
    assert client.post(f"/api/v1/ops/operations/{op_id}/recovery/approve",
                       headers=H, json={}).status_code == 409

    r = client.post(f"/api/v1/ops/operations/{op_id}/recovery/candidate", headers=H,
                    json={"summary": "LH 43 same day 3h later", "new_route": "CGN→CDG",
                          "carrier": "LH", "flight_number": "43", "supplier_fare": 92.0})
    assert r.json()["state"] == "COMPARED"

    client.post(f"/api/v1/ops/operations/{op_id}/recovery/approve", headers=H,
                json={"reason": "acceptable replacement"})
    res = client.post(f"/api/v1/ops/operations/{op_id}/recovery/execute", headers=H,
                      json={}).json()
    assert res["state"] == "EXECUTED"
    assert "does not auto-book" in res["result"]["note"]


# ======================================================================
# Ops safety
# ======================================================================
def test_ticket_ops_require_auth(client):
    bid = _seed_demo_booking(client)
    assert client.post(
        f"/api/v1/ops/bookings/{bid}/tickets/1/cancellation/eligibility"
    ).status_code in (401, 403)


def test_client_cannot_forge_refund_amount(client, H, monkeypatch):
    fake = FakeDuffel(refund="12.34")
    monkeypatch.setattr("detoura.services.ticket_operations.duffel_for_ops",
                        _fake_factory(fake))
    bid = _seed_sandbox_booking(client, monkeypatch)
    op = client.post(f"/api/v1/ops/bookings/{bid}/tickets/1/cancellation/eligibility",
                     headers=H).json()
    op_id = op["operation_id"]
    client.post(f"/api/v1/ops/operations/{op_id}/cancellation/approve", headers=H, json={})
    # try to inject a refund via the execute body — it is ignored
    res = client.post(f"/api/v1/ops/operations/{op_id}/cancellation/execute", headers=H,
                      json={"reason": "x", "idempotency_key": "k",
                            "refund_amount": 9999}).json()
    assert res["result"]["refund_amount"] == 12.34


def test_destructive_actions_are_audited(client, H, monkeypatch):
    fake = FakeDuffel(refund="80.00")
    monkeypatch.setattr("detoura.services.ticket_operations.duffel_for_ops",
                        _fake_factory(fake))
    bid = _seed_sandbox_booking(client, monkeypatch)
    op = client.post(f"/api/v1/ops/bookings/{bid}/tickets/1/cancellation/eligibility",
                     headers=H).json()
    op_id = op["operation_id"]
    client.post(f"/api/v1/ops/operations/{op_id}/cancellation/approve", headers=H, json={})
    client.post(f"/api/v1/ops/operations/{op_id}/cancellation/execute", headers=H, json={})
    audit = client.get("/api/v1/ops/audit", headers=H).json()
    actions = {e["action"] for e in audit}
    assert "CANCELLATION_ELIGIBILITY_CHECKED" in actions
    assert "CANCELLATION_APPROVED" in actions
    assert "CANCELLATION_EXECUTED" in actions


# ======================================================================
# Airline metadata + analytics
# ======================================================================
def test_airline_marketing_and_operating_preserved():
    from detoura.models.airline import carriers_from_duffel_segment
    seg = {
        "marketing_carrier": {"iata_code": "BA", "name": "British Airways"},
        "operating_carrier": {"iata_code": "AA", "name": "American Airlines"},
        "marketing_carrier_flight_number": "1499",
        "operating_carrier_flight_number": "6001",
    }
    c = carriers_from_duffel_segment(seg)
    assert c.marketing.iata_code == "BA"
    assert c.operating.iata_code == "AA"
    assert c.codeshare is True


def test_airline_logo_fallback_never_hides_ticket():
    from detoura.models.airline import airline_for
    known = airline_for("LH")
    assert known.logo_key == "lufthansa"
    unknown = airline_for("QQ")
    assert unknown.logo_key == ""          # no logo…
    assert unknown.display_name == "QQ"    # …but still renderable


def test_airline_report_aggregates(client, H, monkeypatch):
    _seed_sandbox_booking(client, monkeypatch)
    r = client.get("/api/v1/ops/airlines", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["dimension"] == "marketing"
    lh = next((a for a in body["airlines"] if a["iata_code"] == "LH"), None)
    assert lh is not None
    assert lh["name"] == "Lufthansa"
    assert lh["tickets_issued"] >= 1
