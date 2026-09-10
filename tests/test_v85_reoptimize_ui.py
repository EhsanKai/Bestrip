"""V8.5 C3 add-on: interactive journey re-optimization, exercised the way the
new Trip Detail editor drives it.

The editor sends KEEP -> lock_city, REMOVE -> remove_city, REPLACE ->
replace_city through POST /api/v1/trips/reoptimize, shows Original vs New, and
on Accept swaps the selected journey. These tests defend the guarantees the UI
relies on: a locked city always survives, a removed city never returns, the
original stays available, and an accepted edit forces a fresh price check
before booking.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from detoura.api.app import create_app

SEARCH = {
    "origin": "Köln",
    "date_from": "2026-09-14",
    "date_to": "2026-09-28",
    "duration_days": 7,
    "travelers": 2,
    "budget": 1300,
    "interests": ["history", "food", "culture"],
    "search_mode": "SMART",
}


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


@pytest.fixture(scope="module")
def base(client):
    r = client.post("/api/v1/search", json=SEARCH)
    assert r.status_code == 200
    recs = r.json()["recommendations"]
    assert recs, "fixture needs results"
    trip = next((t for t in recs if len(t["cities"]) >= 3), recs[0])
    assert len(trip["cities"]) >= 3, "need a multi-city trip to edit"
    return trip


def _payload(trip: dict) -> dict:
    d2d = round(trip["travel_hours"] * 60)
    return {
        "cities": trip["cities"],
        "origin_airport": trip["origin_airport"],
        "return_airport": trip["return_airport"],
        "departure": trip["departure"],
        "arrival": trip["arrival"],
        "duration_days": trip["duration_days"],
        "total_price": trip["total_price"],
        "intercity_minutes": max(0, d2d - trip["transfer_minutes"]),
        "transfer_minutes": trip["transfer_minutes"],
        "usable_minutes": round(trip["usable_hours"] * 60),
        "experience_score": trip["experience_score"],
        "preference_match": trip["preference_match"],
        "accommodation_score": trip["accommodation_score"],
        "legs": [
            {"from": l["from"], "to": l["to"], "departure": l["departure"],
             "operator": l["operator"], "price_per_person": l["price_per_person"]}
            for l in trip["legs"]
        ],
        "stays": [
            {"city": s["city"], "arrival": s["arrival"],
             "departure": s["departure"], "cost": s["cost"], "name": s.get("name")}
            for s in trip["stays"]
        ],
    }


def _reopt(client, trip, ops):
    return client.post("/api/v1/trips/reoptimize", json={
        "trip_id": trip["id"], "search": SEARCH, "trip": _payload(trip),
        "patch": {"operations": ops},
    })


# 1
def test_lock_one_city(client, base):
    keep = base["cities"][0]
    r = _reopt(client, base, [
        {"op": "lock_city", "city": keep},
        {"op": "remove_city", "city": base["cities"][-1]},
    ])
    assert r.status_code == 200
    j = r.json()
    assert keep in j["locked"]
    if j["trip"]:
        assert keep in j["trip"]["cities"]


# 2
def test_lock_multiple_cities(client, base):
    a, b = base["cities"][0], base["cities"][1]
    r = _reopt(client, base, [
        {"op": "lock_city", "city": a},
        {"op": "lock_city", "city": b},
        {"op": "remove_city", "city": base["cities"][-1]},
    ])
    assert r.status_code == 200
    j = r.json()
    assert set(j["locked"]) >= {a, b}
    if j["trip"]:
        assert a in j["trip"]["cities"] and b in j["trip"]["cities"]


# 3
def test_remove_one_city(client, base):
    gone = base["cities"][-1]
    r = _reopt(client, base, [{"op": "remove_city", "city": gone}])
    assert r.status_code == 200
    j = r.json()
    assert gone in j["excluded"]
    if j["trip"]:
        assert gone not in j["trip"]["cities"]
        assert len(j["trip"]["cities"]) == len(base["cities"]) - 1


# 4
def test_replace_one_city(client, base):
    swap = base["cities"][-1]
    # name a concrete replacement so the count is a hard lock, not a soft target
    r = _reopt(client, base, [
        {"op": "replace_city", "city": swap, "replacement": "Vienna"},
    ])
    assert r.status_code in (200, 422)
    if r.status_code == 200 and r.json()["trip"]:
        cities = r.json()["trip"]["cities"]
        assert swap not in cities
        assert "Vienna" in cities
        # a named replacement holds the count
        assert len(cities) == len(base["cities"])
    # and a bare replace at least removes the named city and never grows the trip
    j2 = _reopt(client, base, [{"op": "replace_city", "city": swap}]).json()
    if j2["trip"]:
        assert swap not in j2["trip"]["cities"]
        assert len(j2["trip"]["cities"]) <= len(base["cities"])


# 5
def test_lock_and_remove_in_the_same_edit(client, base):
    keep, gone = base["cities"][0], base["cities"][-1]
    r = _reopt(client, base, [
        {"op": "lock_city", "city": keep},
        {"op": "remove_city", "city": gone},
    ])
    assert r.status_code == 200
    j = r.json()
    assert keep in j["locked"] and gone in j["excluded"]
    if j["trip"]:
        assert keep in j["trip"]["cities"] and gone not in j["trip"]["cities"]


# 6
def test_a_removed_city_never_comes_back(client, base):
    gone = base["cities"][1]
    j = _reopt(client, base, [{"op": "remove_city", "city": gone}]).json()
    if j["trip"]:
        assert gone not in j["trip"]["cities"]
    for alt in j["alternatives"]:
        assert gone not in alt["cities"]


# 7
def test_locked_cities_remain_in_every_candidate(client, base):
    keep = base["cities"][0]
    j = _reopt(client, base, [
        {"op": "lock_city", "city": keep},
        {"op": "replace_city", "city": base["cities"][-1]},
    ]).json()
    if j["trip"]:
        assert keep in j["trip"]["cities"]
    for alt in j["alternatives"]:
        assert keep in alt["cities"]


# 8
def test_alias_safe_city_handling(client, base):
    # a lowercase / trailing-space form of a real city still locks it
    keep = base["cities"][0]
    r = _reopt(client, base, [
        {"op": "lock_city", "city": f"  {keep.lower()} "},
        {"op": "remove_city", "city": base["cities"][-1]},
    ])
    assert r.status_code in (200, 422)
    if r.status_code == 200 and r.json()["trip"]:
        assert any(c.lower() == keep.lower() for c in r.json()["trip"]["cities"])


# 9
def test_original_trip_is_untouched_by_reoptimize(client, base):
    before = dict(base)
    _reopt(client, base, [{"op": "remove_city", "city": base["cities"][-1]}])
    assert base["cities"] == before["cities"]
    assert base["total_price"] == before["total_price"]
    # and the search still returns it
    again = client.post("/api/v1/search", json=SEARCH).json()["recommendations"]
    assert any(t["id"] == base["id"] for t in again)


# 10 / 11 handled in the UI (Accept swaps `selected`, Keep original does not);
# here we assert the response carries what the UI needs to render both options.
def test_response_carries_original_and_new_for_comparison(client, base):
    j = _reopt(client, base, [{"op": "remove_city", "city": base["cities"][-1]}]).json()
    if j["trip"] is None:
        pytest.skip("no alternative for this trip; comparison N/A")
    assert j["diff"] is not None
    d = j["diff"]
    assert set(d) >= {"cities_kept", "cities_removed", "cities_added",
                      "improvements", "costs", "price_delta"}
    assert j["trip"]["total_price"] >= 0


# 12
_MANAGED_PHASES = {
    "awaiting_travelers", "awaiting_confirmation", "revalidating",
    "reconfirm_required", "issuing", "complete", "partial_failure", "failed",
    "price_inconsistent",
}


def test_reoptimized_journey_needs_a_fresh_revalidation_before_booking(client, base):
    """After accepting a re-optimized journey the NEW journey must be
    revalidated before it is booked, and no pre-edit intent/revalidation may
    survive.

    This is asserted on *durable observable state*, not on catching the
    transient ``revalidating`` phase mid-flight:

      * the booking intent is a brand-new id built from the NEW legs;
      * once the managed run reaches a terminal state, every leg carries a
        ``current_price`` — and the ONLY code that sets ``current_price`` is
        ``_revalidate_item`` inside ``run_booking``. A run that "went straight
        to a pass" without revalidating would leave every ``current_price``
        ``None`` (``create_run_demo`` never sets it).
    """
    j = _reopt(client, base, [
        {"op": "lock_city", "city": base["cities"][0]},
        {"op": "remove_city", "city": base["cities"][-1]},
    ]).json()
    if j["trip"] is None:
        pytest.skip("no alternative")
    new = j["trip"]

    legs = [{
        "origin": l["from"], "destination": l["to"], "departure": l["departure"],
        "arrival": l["arrival"], "carrier": (l["operator"] or "XX").split()[0],
        "flight_number": "1", "price_per_person": l["price_per_person"],
    } for l in new["legs"]]
    b = client.post("/api/v1/booking-intents", json={
        "demo_currency": new["currency"], "demo_total": new["total_price"],
        "demo_legs": legs, "service_tier": "ALL_IN_ONE",
    }).json()
    bid = b["booking_id"]
    # a fresh intent, keyed by its own id, carrying the NEW journey — not the
    # base one and not a resurrected pre-edit intent.
    assert bid.startswith("bk_")
    removed_city = base["cities"][-1]
    kept_city = base["cities"][0]
    booked_cities = b["route_cities"][1:-1]
    assert booked_cities == new["cities"], (booked_cities, new["cities"])
    assert kept_city in booked_cities, "the locked city must survive into the booking"
    if removed_city not in new["cities"]:
        assert removed_city not in booked_cities, "a removed city must not be booked"
    assert len(b["items"]) == len(new["legs"])
    assert all(it["current_price"] is None for it in b["items"]), (
        "a just-created intent must not carry any revalidated price yet"
    )
    assert b["current_total"] is None

    client.post(f"/api/v1/booking-intents/{bid}/travelers", json={"travelers": [{
        "given_name": "Ed", "family_name": "I", "born_on": "1990-01-01",
        "email": "ed@example.com", "phone": "+15551234567"}]})
    r = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    assert r.status_code == 200

    # Wait for the async managed run to finish. This is a bounded wait on a
    # terminal condition (pass_available), not a race for a transient phase.
    seen: set[str] = set()
    final = None
    for _ in range(120):
        s = client.get(f"/api/v1/booking-intents/{bid}").json()
        seen.add(s["phase"])
        if s["pass_available"]:
            final = s
            break
        time.sleep(0.1)
    assert final is not None, f"managed run never reached a terminal state; saw {seen}"

    # every phase we observed belongs to the managed flow — the run did not
    # jump to a state outside the revalidate/issue sequence.
    assert seen <= _MANAGED_PHASES, f"unexpected phase(s): {seen - _MANAGED_PHASES}"

    # Durable proof that a fresh revalidation ran on the NEW journey:
    assert final["current_total"] is not None, (
        "the booked journey has no revalidated total — run_booking did not "
        "revalidate before issuing"
    )
    assert final["items"], "the run has no legs"
    assert all(it["current_price"] is not None for it in final["items"]), (
        "at least one NEW leg was issued without being revalidated first"
    )

    # the travel pass reflects the NEW, revalidated journey.
    pass_r = client.get(f"/api/v1/booking-intents/{bid}/travel-pass")
    assert pass_r.status_code == 200
    tp = pass_r.json()
    assert list(tp["route_cities"][1:-1]) == new["cities"]


# 13
def test_no_stale_booking_intent_is_reused_across_an_edit(client, base):
    """There is no server-side trip->intent mapping: every booking attempt
    creates its own intent keyed by a fresh id. An edit cannot resurrect an
    intent from the pre-edit journey."""
    # intent for the original
    orig_legs = [{
        "origin": l["from"], "destination": l["to"], "departure": l["departure"],
        "arrival": l["arrival"], "price_per_person": l["price_per_person"],
    } for l in base["legs"]]
    a = client.post("/api/v1/booking-intents", json={
        "demo_currency": base["currency"], "demo_total": base["total_price"],
        "demo_legs": orig_legs,
    }).json()

    j = _reopt(client, base, [{"op": "remove_city", "city": base["cities"][-1]}]).json()
    if j["trip"] is None:
        pytest.skip("no alternative")
    new = j["trip"]
    new_legs = [{
        "origin": l["from"], "destination": l["to"], "departure": l["departure"],
        "arrival": l["arrival"], "price_per_person": l["price_per_person"],
    } for l in new["legs"]]
    b = client.post("/api/v1/booking-intents", json={
        "demo_currency": new["currency"], "demo_total": new["total_price"],
        "demo_legs": new_legs,
    }).json()

    assert a["booking_id"] != b["booking_id"]
    assert b["route_cities"] != a["route_cities"]
    # the new intent reflects the new journey, not the old one
    assert base["cities"][-1] not in b["route_cities"]
