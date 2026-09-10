"""V8.5 Phase C2: Ops commercial + promo management, finance, analytics.

Every admin route is auth-gated. The server owns commercial truth: a client
cannot submit a computed fee or margin. Markup changes and promo changes are
audited and never rewrite a historical booking. UNKNOWN costs stay UNKNOWN.
Analytics is anonymous - PII-shaped props are dropped at ingest.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

LEGS = [
    {"origin": "CGN", "destination": "BER", "departure": "2026-10-01T09:00:00Z",
     "arrival": "2026-10-01T10:10:00Z", "carrier": "LH", "flight_number": "1",
     "price_per_person": 130.0},
    {"origin": "BER", "destination": "CGN", "departure": "2026-10-04T09:00:00Z",
     "arrival": "2026-10-04T10:10:00Z", "carrier": "LH", "flight_number": "2",
     "price_per_person": 130.0},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DETOURA_OPS_TOKEN", "adm")
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "c2.db"))
    import detoura.persistence.db as _db
    monkeypatch.setattr(_db, "_DB", None)
    from detoura.api.app import create_app
    return TestClient(create_app())


@pytest.fixture
def H(client):
    tok = client.post("/api/v1/ops/session", json={"token": "adm"}).json()["session_token"]
    return {"Authorization": f"Bearer {tok}"}


def _managed_booking(client, tier="ALL_IN_ONE", wait=6.0):
    b = client.post("/api/v1/booking-intents", json={
        "demo_currency": "EUR", "demo_total": 260.0, "demo_legs": LEGS,
        "service_tier": tier,
    }).json()
    bid = b["booking_id"]
    client.post(f"/api/v1/booking-intents/{bid}/travelers", json={"travelers": [{
        "given_name": "Ida", "family_name": "N", "born_on": "1980-01-01",
        "email": "ida@example.com", "phone": "+15551234567"}]})
    client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    if wait:
        time.sleep(wait)
    client.get(f"/api/v1/booking-intents/{bid}")
    return bid


# --- auth ---------------------------------------------------------
def test_every_c2_route_needs_a_session(client):
    for path in ("/commercial/policies", "/promos", "/finance", "/analytics",
                 "/analytics/events"):
        assert client.get(f"/api/v1/ops{path}").status_code == 401
    assert client.post("/api/v1/ops/commercial/policies", json={}).status_code == 401
    assert client.post("/api/v1/ops/promos", json={}).status_code == 401


# --- markup policy management -----------------------------------
def test_create_activate_and_history(client, H):
    policies = client.get("/api/v1/ops/commercial/policies", headers=H).json()
    active = next(p for p in policies if p["active"])
    assert active["config"]["basic_percentage"] == pytest.approx(0.03)

    # price a booking under the current (v2) policy
    bid = _managed_booking(client)
    d = client.get(f"/api/v1/ops/bookings/{bid}", headers=H).json()
    priced_version = d["economics"]["markup_policy"]

    # operator creates + activates a new version
    r = client.post("/api/v1/ops/commercial/policies", headers=H, json={
        "label": "steeper", "basic_percentage": 0.06, "basic_fixed_fee": 0,
        "all_in_one_percentage": 0.09, "all_in_one_fixed_fee": 12,
        "activate": True,
    })
    assert r.status_code == 200
    new_v = r.json()["version"]
    assert new_v > int(priced_version.split("v")[-1])

    # the historical booking still points at the version it was priced with
    d2 = client.get(f"/api/v1/ops/bookings/{bid}", headers=H).json()
    assert d2["economics"]["markup_policy"] == priced_version
    assert d2["economics"]["detoura_gross_revenue"] == d["economics"]["detoura_gross_revenue"]

    # a NEW booking uses the new policy
    bid2 = _managed_booking(client)
    d3 = client.get(f"/api/v1/ops/bookings/{bid2}", headers=H).json()
    assert d3["economics"]["markup_policy"].endswith(f"v{new_v}")

    # audit recorded the change
    audit = client.get("/api/v1/ops/audit", headers=H).json()
    assert any(e["action"] == "MARKUP_POLICY_SAVED" for e in audit)


def test_client_cannot_submit_a_computed_fee(client, H):
    # the request model has no field for a fee/margin; extras are ignored and
    # the server computes from the tunable numbers
    r = client.post("/api/v1/ops/commercial/policies", headers=H, json={
        "basic_percentage": 0.03, "basic_fixed_fee": 0,
        "all_in_one_percentage": 0.05, "all_in_one_fixed_fee": 6,
        "detoura_fee": 0.0, "customer_total": 1.0, "markup_amount": 999.0,
    })
    assert r.status_code == 200
    pv = client.post("/api/v1/ops/commercial/preview", headers=H, json={
        "supplier_total": 400.0, "ticket_count": 3,
        "policy_id": "detoura.markup", "version": r.json()["version"],
    }).json()
    lines = {l["tier"]: l for l in pv["lines"]}
    assert lines["BASIC"]["detoura_fee_total"] == pytest.approx(12.0)
    assert lines["ALL_IN_ONE"]["detoura_fee_total"] == pytest.approx(26.0)


def test_preview_enforces_the_tier_invariant_and_bounds(client, H):
    pv = client.post("/api/v1/ops/commercial/preview", headers=H, json={
        "supplier_total": 500.0, "ticket_count": 2,
        "draft": {
            "basic_percentage": 0.9, "basic_fixed_fee": 500,
            "all_in_one_percentage": 0.9, "all_in_one_fixed_fee": 900,
            "max_percentage": 0.15, "max_fixed_fee": 25,
            "min_total_fee": 0, "max_total_fee": 120,
        },
    }).json()
    lines = {l["tier"]: l for l in pv["lines"]}
    assert lines["BASIC"]["detoura_fee_total"] <= 120 + 1e-6
    assert lines["ALL_IN_ONE"]["detoura_fee_total"] <= 120 + 1e-6
    assert pv["invariant_ok"] is True
    assert lines["ALL_IN_ONE"]["customer_total"] >= lines["BASIC"]["customer_total"]


# --- promo management ------------------------------------------
def test_promo_crud_and_stats(client, H):
    r = client.post("/api/v1/ops/promos", headers=H, json={
        "code": "OPS20", "kind": "PERCENTAGE", "value": 20,
        "eligible_tiers": ["ALL_IN_ONE"], "per_user_limit": 1,
    })
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert r.json()["eligible_tiers"] == ["ALL_IN_ONE"]

    # disable / enable are audited
    assert client.post("/api/v1/ops/promos/OPS20/disable", headers=H).json()["enabled"] is False
    assert client.post("/api/v1/ops/promos/OPS20/enable", headers=H).json()["enabled"] is True

    # use it on a booking, check redemption + revenue impact
    b = client.post("/api/v1/booking-intents", json={
        "demo_currency": "EUR", "demo_total": 260.0, "demo_legs": LEGS,
        "service_tier": "ALL_IN_ONE", "promo_code": "OPS20",
    }).json()
    bid = b["booking_id"]
    assert b["commercial"]["promo_accepted"] is True
    client.post(f"/api/v1/booking-intents/{bid}/travelers", json={"travelers": [{
        "given_name": "Ann", "family_name": "R", "born_on": "1990-01-01",
        "email": "ann@example.com", "phone": "+15550000000"}]})
    client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    time.sleep(6)
    client.get(f"/api/v1/booking-intents/{bid}")

    detail = client.get("/api/v1/ops/promos/OPS20", headers=H).json()
    assert detail["redemptions"] == 1
    assert detail["discount_total"] > 0
    assert detail["revenue_impact"] == pytest.approx(-detail["discount_total"])
    assert detail["redemption_log"][0]["booking_id"] == bid


def test_promo_never_touches_the_supplier_fare(client, H):
    client.post("/api/v1/ops/promos", headers=H, json={
        "code": "BIG", "kind": "PERCENTAGE", "value": 100,
        "target": "ORDER_TOTAL",
    })
    b = client.post("/api/v1/booking-intents", json={
        "demo_currency": "EUR", "demo_total": 260.0, "demo_legs": LEGS,
        "service_tier": "ALL_IN_ONE", "promo_code": "BIG",
    }).json()
    bd = b["commercial"]["breakdown"]
    assert bd["supplier_total"] == 260.0
    assert bd["customer_total"] >= bd["supplier_total"]
    assert bd["discount"] <= bd["detoura_revenue_gross"] + 1e-6


# --- finance -------------------------------------------------
def test_finance_aggregates_and_keeps_unknown_unknown(client, H):
    _managed_booking(client, "ALL_IN_ONE")
    _managed_booking(client, "BASIC", wait=6.0)
    fin = client.get("/api/v1/ops/finance", headers=H).json()
    assert fin["test_data"] is True
    assert fin["bookings"] >= 2
    assert fin["gross_booking_value"] > 0
    assert fin["supplier_cost"] > 0
    # costs Detoura does not know are counted as unknown, not zero
    assert fin["provider_cost_estimate_unknown_bookings"] == fin["bookings"]
    assert fin["payment_cost_unknown_bookings"] == fin["bookings"]
    assert fin["bookings_with_unknown_costs"] == fin["bookings"]
    assert fin["avg_margin_known"] is None  # nothing is fully costed
    assert set(fin["by_tier"]) == {"BASIC", "ALL_IN_ONE"}


# --- analytics ----------------------------------------------
def test_event_ingest_is_anonymous_and_whitelisted(client, H):
    r = client.post("/api/v1/events", json={
        "session_key": "sess-abc123", "visitor_key": "vis-def456",
        "events": [
            {"event": "SEARCH", "props": {"search_mode": "SMART",
                                          "email": "leak@example.com",
                                          "name": "Real Person"}},
            {"event": "RESULT_VIEW", "props": {"result_count": 4}},
            {"event": "TRIP_OPEN"},
            {"event": "NONSENSE"},
        ],
    })
    assert r.status_code == 200 and r.json()["kept"] == 3

    ev = client.get("/api/v1/ops/analytics/events", headers=H).json()
    blob = str(ev)
    assert "leak@example.com" not in blob
    assert "Real Person" not in blob
    assert "sess-abc123" not in blob  # keys are not echoed in the events list
    search = next(e for e in ev if e["event"] == "SEARCH")
    assert search["props"] == {"search_mode": "SMART"}


def test_funnel_and_tier_selection(client, H):
    for _ in range(3):
        client.post("/api/v1/events", json={
            "session_key": f"s{_}aaaaaa", "visitor_key": "vvvvvvvv",
            "events": [
                {"event": "SEARCH"}, {"event": "RESULT_VIEW"},
                {"event": "TRIP_OPEN"},
                {"event": "TIER_SELECTED", "tier": "ALL_IN_ONE"},
            ],
        })
    client.post("/api/v1/events", json={
        "session_key": "s0aaaaaa", "visitor_key": "vvvvvvvv",
        "events": [{"event": "BOOKED", "tier": "ALL_IN_ONE"}],
    })
    a = client.get("/api/v1/ops/analytics", headers=H).json()
    stages = {s["stage"]: s["sessions"] for s in a["funnel"]}
    assert stages["SEARCH"] == 3
    assert stages["TIER_SELECTED"] == 3
    assert stages["BOOKED"] == 1
    assert a["tier_selection"]["ALL_IN_ONE"]["selected"] == 3
    assert a["tier_selection"]["ALL_IN_ONE"]["conversion"] == pytest.approx(1 / 3, abs=0.01)
    assert a["repeat_search"]["repeat_searchers"] == 1  # one visitor, many searches
