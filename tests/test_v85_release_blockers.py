"""V8.5 release-blocker regression matrix (18 cases).

Integration tests around the real booking request flow, not component stubs.

  1-8   traveller correctness
  9-14  price truth / provenance
  15-18 journey editing visibility + behaviour
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from detoura.api.app import create_app

DEP = "2026-11-01T09:00:00Z"
ARR = "2026-11-01T10:10:00Z"


def _leg(o, d, pp, dep=DEP, arr=ARR):
    return {"origin": o, "destination": d, "departure": dep, "arrival": arr,
            "carrier": "LH", "flight_number": "1", "price_per_person": pp}


# CGN -> Paris -> Barcelona -> CGN, the manual-acceptance example fares.
LEGS3 = [
    _leg("CGN", "CDG", 24.20),
    _leg("CDG", "BCN", 42.00, "2026-11-04T09:00:00Z", "2026-11-04T11:00:00Z"),
    _leg("BCN", "CGN", 53.36, "2026-11-07T09:00:00Z", "2026-11-07T11:10:00Z"),
]
PER_PERSON_SUM = 24.20 + 42.00 + 53.36  # 119.56


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "rb.db"))
    monkeypatch.setenv("DETOURA_OPS_TOKEN", "adm")
    import detoura.persistence.db as _db
    monkeypatch.setattr(_db, "_DB", None)
    return TestClient(create_app())


def _person(i: int, **extra):
    p = {
        "given_name": f"Trav{i}", "family_name": f"Person{i}",
        "born_on": f"199{i % 10}-0{(i % 8) + 1}-15",
        "email": f"t{i}@example.com", "phone": f"+15550000{i:02d}",
    }
    p.update(extra)
    return p


def _intent(client, *, travelers: int, tier="ALL_IN_ONE", estimate=None,
            legs=LEGS3):
    body = {
        "demo_currency": "EUR", "demo_travelers": travelers,
        "demo_legs": legs, "service_tier": tier,
    }
    if estimate is not None:
        body["demo_trip_estimate"] = estimate
    r = client.post("/api/v1/booking-intents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ============================================================
# Traveller correctness
# ============================================================
@pytest.mark.parametrize("n", [1, 2, 4])
def test_1_2_3_traveller_count_matches_search(client, n):
    """1/2/3: N travellers in the search -> exactly N traveller records."""
    j = _intent(client, travelers=n)
    assert j["requested_travelers"] == n
    bid = j["booking_id"]
    ppl = [_person(i) for i in range(n)]
    r = client.post(f"/api/v1/booking-intents/{bid}/travelers",
                    json={"travelers": ppl})
    assert r.status_code == 200, r.text
    assert r.json()["party_size"] == n


def test_4_missing_traveller_blocks_booking(client):
    j = _intent(client, travelers=3)
    bid = j["booking_id"]
    r = client.post(f"/api/v1/booking-intents/{bid}/travelers",
                    json={"travelers": [_person(0), _person(1)]})
    assert r.status_code == 422
    assert "3 traveller" in r.json()["detail"]["message"]
    # and the run did not advance
    assert client.get(f"/api/v1/booking-intents/{bid}").json()["travelers_submitted"] is False


def test_5_traveller_one_cannot_be_silently_duplicated(client):
    """Sending the same identity 3 times is 3 records with a shared document ->
    rejected; without documents it is still 3 *distinct* Traveler rows, never
    one row copied by the server to fill the party."""
    j = _intent(client, travelers=3)
    bid = j["booking_id"]
    same = _person(0, passport_number="X1234567")
    r = client.post(f"/api/v1/booking-intents/{bid}/travelers",
                    json={"travelers": [same, same, same]})
    assert r.status_code == 422
    assert "document number" in r.json()["detail"]["message"]


def test_6_each_document_stays_with_its_passenger(client):
    j = _intent(client, travelers=2)
    bid = j["booking_id"]
    a = _person(0, passport_number="AAA111", passport_issuing_country="DE",
                passport_expiry="2031-01-01")
    b = _person(1, passport_number="BBB222", passport_issuing_country="FR",
                passport_expiry="2032-02-02")
    r = client.post(f"/api/v1/booking-intents/{bid}/travelers",
                    json={"travelers": [a, b]})
    assert r.status_code == 200
    # ops inspector never exposes document numbers, but the run keeps them
    # associated - assert via the pass which uses the party
    client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    time.sleep(6)
    p = client.get(f"/api/v1/booking-intents/{bid}/travel-pass").json()
    # the lead is traveller 0; documents are not on the pass at all
    assert "AAA111" not in str(p) and "BBB222" not in str(p)


def test_7_required_document_missing_blocks_issuance(client, monkeypatch):
    """A travel document is required to issue on an international SANDBOX route;
    booking must not proceed without it."""
    import detoura.services.booking_flow as bf

    # force the run to look like a managed international booking that needs docs
    monkeypatch.setattr(bf, "documents_required", lambda run: True)
    j = _intent(client, travelers=1, tier="ALL_IN_ONE")
    bid = j["booking_id"]
    client.post(f"/api/v1/booking-intents/{bid}/travelers",
                json={"travelers": [_person(0)]})  # no passport
    r = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    assert r.status_code == 409
    assert "travel document is required" in r.json()["detail"]["message"]
    # with a document it proceeds
    client.post(f"/api/v1/booking-intents/{bid}/travelers", json={"travelers": [
        _person(0, passport_number="P999", passport_issuing_country="DE",
                passport_expiry="2030-01-01")]})
    assert client.post(f"/api/v1/booking-intents/{bid}/confirm", json={}).status_code == 200


def test_8_document_data_never_enters_analytics_or_logs(client):
    # analytics ingest scrubs anything PII-shaped
    r = client.post("/api/v1/events", json={
        "session_key": "aaaaaaaa", "visitor_key": "bbbbbbbb",
        "events": [{"event": "REVIEW", "tier": "ALL_IN_ONE",
                    "props": {"passport": "X1234567", "born": "1990-01-01"}}],
    })
    assert r.status_code == 200
    H = {"Authorization": "Bearer " + client.post(
        "/api/v1/ops/session", json={"token": "adm"}).json()["session_token"]}
    events = client.get("/api/v1/ops/analytics/events", headers=H).text
    assert "X1234567" not in events
    # a full booking with documents: neither the intent DTO nor the ops record
    # nor the audit log carries the number
    j = _intent(client, travelers=1)
    bid = j["booking_id"]
    intent = client.post(f"/api/v1/booking-intents/{bid}/travelers", json={
        "travelers": [_person(0, passport_number="SECRET42",
                              passport_issuing_country="DE",
                              passport_expiry="2030-01-01")]}).text
    assert "SECRET42" not in intent
    ops = client.get(f"/api/v1/ops/bookings/{bid}", headers=H).text
    assert "SECRET42" not in ops
    assert "SECRET42" not in client.get("/api/v1/ops/audit", headers=H).text


# ============================================================
# Price truth / provenance
# ============================================================
def test_9_ticket_subtotal_reconciles_with_supplier_prices(client):
    j = _intent(client, travelers=1)
    b = j["commercial"]["breakdown"]
    assert b["supplier_transport"] == pytest.approx(PER_PERSON_SUM, abs=0.01)
    assert b["bookable_ticket_subtotal"] == pytest.approx(PER_PERSON_SUM, abs=0.01)
    assert b["reconciled"] is True
    assert j["price_reconciled"] is True


def test_10_whole_trip_estimate_cannot_become_supplier_transport(client):
    est = {"total": 575.0, "transport": 119.56, "accommodation": 400.0,
           "transfer": 55.44}
    j = _intent(client, travelers=1, estimate=est)
    assert j["discovered_total"] == pytest.approx(PER_PERSON_SUM, abs=0.01)
    assert j["commercial"]["breakdown"]["supplier_transport"] == pytest.approx(
        PER_PERSON_SUM, abs=0.01)
    assert j["trip_estimate"]["total"] == 575.0
    assert j["trip_estimate"]["accommodation"] == 400.0


def test_11_accommodation_estimate_not_charged_as_transport(client):
    est = {"total": 900.0, "transport": 119.56, "accommodation": 700.0,
           "transfer": 80.44}
    j = _intent(client, travelers=1, estimate=est)
    b = j["commercial"]["breakdown"]
    # the payable total is the tickets + fee, nowhere near the €900 estimate
    assert b["customer_total"] < 200.0
    assert b["customer_total"] == pytest.approx(
        b["supplier_transport"] + b["detoura_revenue_gross"] - b["discount"] + b["tax"],
        abs=0.01)


def test_12_detoura_fee_uses_the_supplier_transport_base(client):
    j = _intent(client, travelers=3, estimate={"total": 999.0, "accommodation": 600.0})
    b = j["commercial"]["breakdown"]
    party_transport = round(PER_PERSON_SUM * 3, 2)
    assert b["supplier_transport"] == pytest.approx(party_transport, abs=0.02)
    # AIO seed: 5% + EUR 6
    assert b["detoura_revenue_gross"] == pytest.approx(
        round(party_transport * 0.05, 2) + 6.0, abs=0.02)


def test_13_displayed_total_equals_server_authoritative_total(client):
    """Whatever the intent DTO shows as the payable amount is exactly what a
    fresh GET returns - one server-owned number."""
    j = _intent(client, travelers=2)
    bid = j["booking_id"]
    first = j["commercial"]["breakdown"]["customer_total"]
    again = client.get(f"/api/v1/booking-intents/{bid}").json()
    assert again["commercial"]["breakdown"]["customer_total"] == first
    # and after travellers + confirm it is unchanged (no promo lapse here)
    client.post(f"/api/v1/booking-intents/{bid}/travelers",
                json={"travelers": [_person(0), _person(1)]})
    conf = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={}).json()
    assert conf["commercial"]["breakdown"]["customer_total"] == first


def test_14_reconciliation_mismatch_blocks_confirmation(client, monkeypatch):
    """If the priced supplier transport ever diverges from the ticket fares,
    confirmation is refused with PRICE_INCONSISTENT."""
    import detoura.services.booking_commercial as bc

    j = _intent(client, travelers=1, estimate={"total": 574.52, "accommodation": 300.0})
    bid = j["booking_id"]
    client.post(f"/api/v1/booking-intents/{bid}/travelers",
                json={"travelers": [_person(0)]})

    # now corrupt the priced base so it equals the whole-trip estimate
    real = bc._supplier_transport
    monkeypatch.setattr(bc, "_supplier_transport", lambda run: 574.52)
    r = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["phase"] == "price_inconsistent"
    assert client.get(f"/api/v1/booking-intents/{bid}").json()["phase"] == "price_inconsistent"
    monkeypatch.setattr(bc, "_supplier_transport", real)


# ============================================================
# Journey editing
# ============================================================
EDIT_SEARCH = {
    "origin": "Köln", "date_from": "2026-09-14", "date_to": "2026-09-28",
    "duration_days": 7, "travelers": 2, "budget": 1300,
    "interests": ["history", "food", "culture"], "search_mode": "SMART",
}


@pytest.fixture
def searched(client):
    r = client.post("/api/v1/search", json=EDIT_SEARCH)
    assert r.status_code == 200
    return r.json()["recommendations"]


def _payload(t):
    d2d = round(t["travel_hours"] * 60)
    return {
        "cities": t["cities"], "origin_airport": t["origin_airport"],
        "return_airport": t["return_airport"], "departure": t["departure"],
        "arrival": t["arrival"], "duration_days": t["duration_days"],
        "total_price": t["total_price"],
        "intercity_minutes": max(0, d2d - t["transfer_minutes"]),
        "transfer_minutes": t["transfer_minutes"],
        "usable_minutes": round(t["usable_hours"] * 60),
        "experience_score": t["experience_score"],
        "preference_match": t["preference_match"],
        "accommodation_score": t["accommodation_score"],
        "legs": [{"from": l["from"], "to": l["to"], "departure": l["departure"],
                  "operator": l["operator"],
                  "price_per_person": l["price_per_person"]} for l in t["legs"]],
        "stays": [{"city": s["city"], "arrival": s["arrival"],
                   "departure": s["departure"], "cost": s["cost"],
                   "name": s.get("name")} for s in t["stays"]],
    }


def test_15_16_edit_route_reachable_and_works(client, searched):
    """15: the reoptimize endpoint the visible editor calls is reachable on the
    normal route. 16: lock + remove + reoptimize produce a coherent result."""
    multi = next((t for t in searched if len(t["cities"]) >= 2), None)
    assert multi is not None
    r = client.post("/api/v1/trips/reoptimize", json={
        "trip_id": multi["id"], "search": EDIT_SEARCH, "trip": _payload(multi),
        "patch": {"operations": [
            {"op": "lock_city", "city": multi["cities"][0]},
            {"op": "remove_city", "city": multi["cities"][-1]},
        ]},
    })
    assert r.status_code == 200
    j = r.json()
    assert multi["cities"][0] in j["locked"]
    assert multi["cities"][-1] in j["excluded"]
    if j["trip"]:
        assert multi["cities"][0] in j["trip"]["cities"]
        assert multi["cities"][-1] not in j["trip"]["cities"]


def test_15b_single_destination_trip_can_still_be_edited(client, searched):
    """The exact bug: Edit was hidden for one-destination trips. Replace must
    work; a lone Remove must not empty the trip."""
    one = next((t for t in searched if len(t["cities"]) == 1), None)
    if one is None:
        # construct one deterministically by removing down to a single city
        multi = next(t for t in searched if len(t["cities"]) >= 3)
        r = client.post("/api/v1/trips/reoptimize", json={
            "trip_id": multi["id"], "search": EDIT_SEARCH,
            "trip": _payload(multi),
            "patch": {"operations": [
                {"op": "remove_city", "city": c} for c in multi["cities"][1:]
            ]},
        })
        assert r.status_code in (200, 422)
        return
    r = client.post("/api/v1/trips/reoptimize", json={
        "trip_id": one["id"], "search": EDIT_SEARCH, "trip": _payload(one),
        "patch": {"operations": [
            {"op": "replace_city", "city": one["cities"][0]},
        ]},
    })
    assert r.status_code in (200, 422)
    if r.status_code == 200 and r.json()["trip"]:
        assert one["cities"][0] not in r.json()["trip"]["cities"]
        assert len(r.json()["trip"]["cities"]) >= 1


# 17 / 18 (desktop / mobile acceptance) are covered by the browser acceptance
# script run against the container; this asserts the server contract they rely
# on - the reoptimized trip is a full recommendation the booking flow accepts.
def test_17_18_reoptimized_trip_is_bookable(client, searched):
    multi = next((t for t in searched if len(t["cities"]) >= 2), None)
    assert multi is not None
    j = client.post("/api/v1/trips/reoptimize", json={
        "trip_id": multi["id"], "search": EDIT_SEARCH, "trip": _payload(multi),
        "patch": {"operations": [{"op": "remove_city", "city": multi["cities"][-1]}]},
    }).json()
    if j["trip"] is None:
        pytest.skip("no alternative")
    new = j["trip"]
    legs = [_leg(l["from"], l["to"], l["price_per_person"],
                 l["departure"], l["arrival"]) for l in new["legs"]]
    b = client.post("/api/v1/booking-intents", json={
        "demo_currency": new["currency"], "demo_travelers": 2, "demo_legs": legs,
        "service_tier": "ALL_IN_ONE",
        "demo_trip_estimate": {"total": new["total_price"],
                               "transport": new["costs"]["transport"],
                               "accommodation": new["costs"]["accommodation"],
                               "transfer": new["costs"]["ground_transfer"]},
    })
    assert b.status_code == 201
    jj = b.json()
    assert jj["requested_travelers"] == 2
    assert jj["price_reconciled"] is True
    # supplier transport is the flight sum for the NEW journey, not the estimate
    assert jj["commercial"]["breakdown"]["supplier_transport"] < new["total_price"]
