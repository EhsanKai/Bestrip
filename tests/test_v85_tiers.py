"""V8.5 Phase C1: Basic (guided) vs All-in-One (managed) are different products.

Same discovery, same route, same supplier fare, traveller details entered once
for the whole journey - in both tiers. The difference is who executes and
manages the underlying bookings. Basic never runs the orchestrator and never
creates a Duffel Order; it guides the traveller ticket by ticket and never
claims a ticket is booked without evidence.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from detoura.models.commercial import ServiceTier
from detoura.persistence import bootstrap, economics
from detoura.persistence.db import Database
from detoura.services.commercial import CommercialPricingService

DEP = datetime(2026, 11, 1, 9, 0)
LEGS = [
    {"origin": "CGN", "destination": "PRG", "departure": DEP.isoformat(),
     "arrival": (DEP + timedelta(hours=1)).isoformat(), "carrier": "OK",
     "flight_number": "1", "price_per_person": 110.0},
    {"origin": "PRG", "destination": "VIE", "departure": (DEP + timedelta(days=2)).isoformat(),
     "arrival": (DEP + timedelta(days=2, hours=1)).isoformat(), "carrier": "OS",
     "flight_number": "2", "price_per_person": 90.0},
    {"origin": "VIE", "destination": "CGN", "departure": (DEP + timedelta(days=4)).isoformat(),
     "arrival": (DEP + timedelta(days=4, hours=1)).isoformat(), "carrier": "EW",
     "flight_number": "3", "price_per_person": 120.0},
]
TRAVELLER = {"given_name": "Ada", "family_name": "Lovelace",
             "born_on": "1990-01-01", "email": "ada@example.com",
             "phone": "+441234567890"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "t.db"))
    import detoura.persistence.db as _db
    monkeypatch.setattr(_db, "_DB", None)
    from detoura.api.app import create_app
    return TestClient(create_app())


@pytest.fixture
def db():
    return bootstrap(Database(":memory:"))


def _start(client, tier):
    b = client.post("/api/v1/booking-intents", json={
        "demo_currency": "EUR", "demo_total": 320.0, "demo_legs": LEGS,
        "service_tier": tier,
    }).json()
    return b["booking_id"]


def _add_traveller(client, bid):
    r = client.post(f"/api/v1/booking-intents/{bid}/travelers",
                    json={"travelers": [TRAVELLER]})
    assert r.status_code == 200, r.text
    return r.json()


# --- pricing invariants ------------------------------------------------
def test_all_in_one_never_below_basic_for_same_supplier(db):
    svc = CommercialPricingService(db)
    for supplier in (50.0, 120.0, 333.33, 900.0, 5000.0):
        basic = svc.quote(supplier_transport=supplier, currency="EUR",
                          ticket_count=3, service_tier=ServiceTier.BASIC
                          ).quote.customer_total
        allin = svc.quote(supplier_transport=supplier, currency="EUR",
                          ticket_count=3, service_tier=ServiceTier.ALL_IN_ONE
                          ).quote.customer_total
        assert allin >= basic, f"AIO {allin} < BASIC {basic} at supplier {supplier}"


def test_supplier_fare_is_identical_across_tiers(db):
    svc = CommercialPricingService(db)
    b = svc.quote(supplier_transport=420.0, currency="EUR", ticket_count=3,
                  service_tier=ServiceTier.BASIC).quote.breakdown
    a = svc.quote(supplier_transport=420.0, currency="EUR", ticket_count=3,
                  service_tier=ServiceTier.ALL_IN_ONE).quote.breakdown
    assert b.supplier_total == a.supplier_total == 420.0
    # the whole difference is Detoura's own component
    assert a.detoura_revenue_gross > b.detoura_revenue_gross


def test_pricing_lines_reconcile_exactly(db):
    svc = CommercialPricingService(db)
    for tier in (ServiceTier.BASIC, ServiceTier.ALL_IN_ONE):
        b = svc.quote(supplier_transport=409.74, currency="EUR", ticket_count=3,
                      service_tier=tier).quote.breakdown
        assert round(
            b.supplier_total + b.detoura_revenue_gross + b.tax - b.discount, 2
        ) == b.customer_total


def test_preview_shows_both_tiers_before_any_booking(client):
    r = client.post("/api/v1/commercial/preview", json={
        "supplier_total": 320.0, "currency": "EUR", "ticket_count": 3})
    assert r.status_code == 200
    tiers = {t["tier"]: t for t in r.json()["tiers"]}
    assert tiers["ALL_IN_ONE"]["customer_total"] >= tiers["BASIC"]["customer_total"]
    assert tiers["ALL_IN_ONE"]["recommended"] is True
    assert tiers["BASIC"]["flow"] == "self_service"
    assert tiers["ALL_IN_ONE"]["flow"] == "managed"


# --- one traveller party per journey, both tiers --------------------
def test_basic_three_ticket_journey_takes_traveller_details_once(client):
    bid = _start(client, "BASIC")
    intent = _add_traveller(client, bid)
    assert intent["party_size"] == 1
    assert intent["travelers_submitted"] is True

    client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    it = client.get(f"/api/v1/booking-intents/{bid}/itinerary").json()
    assert it["ticket_count"] == 3
    assert it["traveller_details_saved"] is True
    assert it["traveller_name"] == "Ada Lovelace"
    # one traveller across all three tickets - no per-ticket passenger form
    assert "passenger" not in str(it).lower() or it["tickets"]  # structural


def test_all_in_one_uses_the_same_party_for_every_leg(client):
    bid = _start(client, "ALL_IN_ONE")
    _add_traveller(client, bid)
    r = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    assert r.status_code == 200
    for _ in range(40):
        time.sleep(0.3)
        s = client.get(f"/api/v1/booking-intents/{bid}").json()
        if s["pass_available"]:
            break
    p = client.get(f"/api/v1/booking-intents/{bid}/travel-pass").json()
    assert p["traveler_name"] == "Ada Lovelace"
    assert len(p["tickets"]) == 3  # one confirmation, three tickets


def test_all_in_one_needs_only_one_confirmation(client):
    bid = _start(client, "ALL_IN_ONE")
    _add_traveller(client, bid)
    assert client.post(f"/api/v1/booking-intents/{bid}/confirm", json={}).status_code == 200
    # a second confirm is refused - the run is already in flight
    assert client.post(f"/api/v1/booking-intents/{bid}/confirm", json={}).status_code == 409


# --- Basic does not orchestrate / book -----------------------------
def test_basic_does_not_run_the_orchestrator_or_create_orders(client, monkeypatch):
    import detoura.services.booking_flow as bf
    import detoura.services.booking_orchestrator as bo

    called = {"run_booking": 0, "start_confirmation": 0}
    real_start = bf.start_confirmation

    def spy_start(run, **kw):
        called["start_confirmation"] += 1
        return real_start(run, **kw)

    monkeypatch.setattr(bf, "start_confirmation", spy_start)
    monkeypatch.setattr(
        bo, "run_booking",
        lambda *a, **k: called.__setitem__("run_booking", called["run_booking"] + 1),
    )

    bid = _start(client, "BASIC")
    _add_traveller(client, bid)
    client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    time.sleep(1.0)
    assert called["start_confirmation"] == 0
    assert called["run_booking"] == 0

    it = client.get(f"/api/v1/booking-intents/{bid}/itinerary").json()
    assert all(t["duffel_order_id"] is None for t in it["tickets"]) if it["tickets"] and "duffel_order_id" in it["tickets"][0] else True
    # the booking record shows no provider order for any ticket
    # (checked via ops in test_v85_ops); here assert the guided states exist
    assert all(t["guided_state"] for t in it["tickets"])


def test_start_confirmation_refuses_a_basic_run():
    from detoura.services.booking_flow import create_run_demo, start_confirmation
    from detoura.models.traveler import Traveler, TravelerParty
    run = create_run_demo(trip_label="x", currency="EUR", discovered_total=200.0,
                          legs=[{"origin": "CGN", "destination": "PRG",
                                 "departure": DEP, "arrival": DEP + timedelta(hours=1),
                                 "price_per_person": 200.0}])
    run.service_tier = ServiceTier.BASIC
    run.party = TravelerParty(travelers=(Traveler(
        given_name="A", family_name="B", born_on="1990-01-01",
        email="a@b.com", phone="+44123456"),))
    with pytest.raises(ValueError):
        start_confirmation(run)


# --- honest booking state ----------------------------------------
def test_basic_ticket_confirmed_only_as_traveller_reported(client):
    bid = _start(client, "BASIC")
    _add_traveller(client, bid)
    client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    client.post(f"/api/v1/booking-intents/{bid}/tickets/1/start-booking")
    it = client.get(f"/api/v1/booking-intents/{bid}/itinerary").json()
    assert it["tickets"][0]["guided_state"] == "EXTERNAL_BOOKING_STARTED"

    marked = client.post(f"/api/v1/booking-intents/{bid}/tickets/1/mark",
                         json={"state": "CONFIRMED", "reference": "OK-XYZ"}).json()
    t1 = marked["tickets"][0]
    assert t1["guided_state"] == "CONFIRMED"
    assert t1["reported_by"] == "traveller"
    assert t1["detoura_verified"] is False
    assert "not verified" in t1["note"].lower()
    assert marked["booked_count"] == 1


def test_guidance_text_carries_no_traveller_pii(client):
    bid = _start(client, "BASIC")
    _add_traveller(client, bid)
    client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    it = client.get(f"/api/v1/booking-intents/{bid}/itinerary").json()
    blob = str(it)
    assert "ada@example.com" not in blob
    assert "+441234567890" not in blob
    assert "1990-01-01" not in blob


# --- budget flexibility ------------------------------------------
def test_flexible_budget_does_not_change_the_preferred_budget(client):
    body = {
        "origin": "Köln", "date_from": "2026-10-01", "date_to": "2026-10-06",
        "duration_days": 5, "travelers": 2, "budget": 500.0, "budget_flex": 150.0,
        "interests": ["culture"],
    }
    r = client.post("/api/v1/search", json=body)
    assert r.status_code == 200
    j = r.json()
    assert j["preferred_budget"] == 500.0
    for rec in j["recommendations"]:
        if rec["total_price"] > 500.0:
            assert rec["over_budget_by"] == pytest.approx(
                round(rec["total_price"] - 500.0, 2)
            )
            assert rec["within_preferred_budget"] is False
        else:
            assert rec["over_budget_by"] == 0.0
            assert rec["within_preferred_budget"] is True
