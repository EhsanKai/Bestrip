"""Re-checking a saved trip (V6.1).

The endpoint's whole job is to answer "is this trip still there, at that
price?" honestly, and the interesting cases are the ones where the honest
answer is "we don't know". Those are asserted hardest here, because they are
the ones a careless implementation turns into a confident wrong answer.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.models.accommodation import AccommodationOption  # noqa: E402
from detoura.models.freshness import PriceFreshness  # noqa: E402
from detoura.models.transport import TransportOption, TransportType  # noqa: E402
from detoura.services.planner import TravelPlanner  # noqa: E402
from detoura.services.recheck import (  # noqa: E402
    ComponentState,
    RecheckStatus,
    recheck_trip,
)

DEPART = datetime(2026, 9, 10, 9, 0)
RETURN = datetime(2026, 9, 15, 18, 0)


# ---------------------------------------------------------------------------
# Stubs. Minimal on purpose: each one exists to make exactly one thing true.
# ---------------------------------------------------------------------------
class StubTransport:
    """One outbound and one return leg, with the fare under the test's control."""

    def __init__(self, price=60.0, *, seats=None, offer=True, explode=False):
        self.price = price
        self.seats = seats
        self.offer = offer
        self.explode = explode
        self.calls = 0

    def search(self, origin: str, destination: str, departure_date: date):
        self.calls += 1
        if self.explode:
            raise ConnectionError("provider unreachable")
        if not self.offer:
            return []
        depart = DEPART if origin == "DUS" else RETURN
        if depart.date() != departure_date:
            return []
        return [
            TransportOption(
                id=f"{origin}{destination}{departure_date}",
                origin=origin,
                destination=destination,
                departure=depart,
                arrival=depart + timedelta(hours=2),
                price_per_person=self.price,
                transport_type=TransportType.FLIGHT,
                duration_minutes=120,
                operator="TestAir",
                seats_available=self.seats,
            )
        ]


class StubRooms:
    """One named stay, with the rate under the test's control."""

    def __init__(self, rate=50.0, *, rooms=None, offer=True, explode=False):
        self.rate = rate
        self.rooms = rooms
        self.offer = offer
        self.explode = explode
        self.calls = 0

    def search(self, city: str, check_in: date, check_out: date, travelers: int):
        self.calls += 1
        if self.explode:
            raise ConnectionError("provider unreachable")
        if not self.offer:
            return []
        return [
            AccommodationOption(
                id=f"{city}-{check_in}",
                city=city,
                name="Hotel Test",
                check_in=check_in,
                check_out=check_out,
                price_per_night=self.rate,
                capacity=2,
                rooms_available=self.rooms,
            )
        ]

    def min_price_per_night(self, city: str, travelers: int):
        return self.rate


LEGS = [
    {
        "origin": "DUS",
        "destination": "Prague",
        "departure": DEPART,
        "operator": "TestAir",
        "price_per_person": 60.0,
    },
    {
        "origin": "Prague",
        "destination": "DUS",
        "departure": RETURN,
        "operator": "TestAir",
        "price_per_person": 60.0,
    },
]

STAYS = [
    {
        "city": "Prague",
        "arrival": DEPART,
        "departure": RETURN,
        "name": "Hotel Test",
        "cost": 250.0,
    }
]

#: Two travelers: legs are 60 pp -> 120 each -> 240; stay is 50/night x 5 = 250.
SAVED_TOTAL = 490.0


def build(transport=None, rooms=None) -> TravelPlanner:
    return TravelPlanner(
        transport_provider=transport or StubTransport(),
        accommodation_provider=rooms or StubRooms(),
    )


def check(planner, *, saved=SAVED_TOTAL, legs=None, stays=None):
    return recheck_trip(
        planner,
        legs=legs if legs is not None else LEGS,
        stays=stays if stays is not None else STAYS,
        travelers=2,
        saved_price=saved,
    )


# ---------------------------------------------------------------------------
# The ordinary answers
# ---------------------------------------------------------------------------
def test_a_trip_that_has_not_moved_is_unchanged():
    result = check(build())

    assert result.status is RecheckStatus.UNCHANGED
    assert result.current_price == SAVED_TOTAL
    assert result.change == 0.0


def test_a_dearer_fare_is_reported_with_the_difference():
    result = check(build(StubTransport(price=80.0)))

    assert result.status is RecheckStatus.PRICE_CHANGED
    # 80 pp x 2 x 2 legs = 320, plus the 250 stay.
    assert result.current_price == 570.0
    assert result.change == 80.0
    assert result.change_pct == pytest.approx(16.3, abs=0.1)


def test_a_cheaper_trip_is_a_change_too():
    """Good news is still news: the traveler saved it at the higher price."""
    result = check(build(StubTransport(price=40.0)))

    assert result.status is RecheckStatus.PRICE_CHANGED
    assert result.change == -80.0


def test_rounding_noise_is_not_worth_interrupting_anyone_about():
    """A fare that moved by cents has not meaningfully changed."""
    result = check(build(StubTransport(price=60.1)))

    assert result.status is RecheckStatus.UNCHANGED


# ---------------------------------------------------------------------------
# Things genuinely gone
# ---------------------------------------------------------------------------
def test_a_withdrawn_departure_is_gone_not_re_matched():
    """The next flight out is a different trip, and must not be substituted."""
    planner = build()
    moved = [{**LEGS[0], "departure": DEPART + timedelta(hours=3)}, LEGS[1]]

    result = check(planner, legs=moved)

    assert result.legs[0].state is ComponentState.GONE
    assert result.status is RecheckStatus.PARTIALLY_UNAVAILABLE


def test_a_trip_whose_every_part_is_gone_is_unavailable():
    result = check(build(StubTransport(offer=False), StubRooms(offer=False)))

    assert result.status is RecheckStatus.UNAVAILABLE


def test_a_leg_without_seats_for_the_party_is_sold_out():
    result = check(build(StubTransport(seats=1)))

    assert result.legs[0].state is ComponentState.SOLD_OUT
    assert result.status is RecheckStatus.PARTIALLY_UNAVAILABLE


def test_a_stay_without_rooms_for_the_party_is_sold_out():
    result = check(build(rooms=StubRooms(rooms=0)))

    assert result.stays[0].state is ComponentState.SOLD_OUT


# ---------------------------------------------------------------------------
# The rule that matters most
# ---------------------------------------------------------------------------
def test_a_provider_outage_is_never_reported_as_a_missing_trip():
    """The whole point of the module.

    If a timeout could read as UNAVAILABLE, a traveler would abandon a trip
    that is still perfectly bookable because our network had a bad second.
    """
    result = check(build(StubTransport(explode=True)))

    assert result.status is RecheckStatus.UNVERIFIABLE
    assert result.status is not RecheckStatus.UNAVAILABLE
    assert result.legs[0].state is ComponentState.UNVERIFIABLE


def test_unverifiable_wins_over_parts_that_did_check_out():
    """One unchecked part means the trip's total is not a fact."""
    result = check(build(StubTransport(explode=True)))

    assert result.stays[0].state is ComponentState.FOUND
    assert result.status is RecheckStatus.UNVERIFIABLE


def test_unverifiable_wins_even_over_something_genuinely_gone():
    """We cannot describe a trip we could not finish checking."""
    result = check(build(StubTransport(explode=True), StubRooms(offer=False)))

    assert result.stays[0].state is ComponentState.GONE
    assert result.status is RecheckStatus.UNVERIFIABLE


def test_no_total_is_reported_when_a_part_could_not_be_priced():
    """Summing the parts we could reach would read as a bargain."""
    result = check(build(StubTransport(explode=True)))

    assert result.current_price is None
    assert result.change is None
    assert result.change_pct is None


def test_a_stay_saved_without_a_name_cannot_be_matched_or_condemned():
    """Missing identity is our gap, not evidence the hotel stopped existing."""
    nameless = [{**STAYS[0], "name": ""}]

    result = check(build(), stays=nameless)

    assert result.stays[0].state is ComponentState.UNVERIFIABLE
    assert result.status is RecheckStatus.UNVERIFIABLE


# ---------------------------------------------------------------------------
# It has to actually re-check
# ---------------------------------------------------------------------------
def test_the_recheck_bypasses_the_cache_that_the_search_warmed():
    """A re-check served from the memo table would return the saved number.

    This is the failure that would make every other assertion here vacuous:
    the status would say UNCHANGED, and it would say it without asking anyone.
    """
    transport = StubTransport()
    planner = build(transport)

    # Warm the cache the way a search does, through the decorator.
    planner.transport.search("DUS", "Prague", DEPART.date())
    planner.transport.search("DUS", "Prague", DEPART.date())
    assert transport.calls == 1, "the caching decorator should have absorbed the second"

    before = transport.calls
    check(planner)

    assert transport.calls > before


def test_a_price_move_is_seen_even_after_the_search_cached_the_old_one():
    """The end-to-end version of the above, in the terms the user cares about."""
    transport = StubTransport(price=60.0)
    planner = build(transport)
    planner.transport.search("DUS", "Prague", DEPART.date())  # cache the old fare

    transport.price = 90.0
    result = check(planner)

    assert result.status is RecheckStatus.PRICE_CHANGED
    assert result.change == 120.0


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------
def test_synthetic_prices_re_check_as_unknown_not_fresh():
    """Fabricated data has no provenance, and must not be dressed up as live."""
    result = check(build())

    assert result.freshness is PriceFreshness.UNKNOWN


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402

from detoura.api.app import create_app  # noqa: E402
from detoura.api.v1 import get_planner  # noqa: E402

SEARCH = {
    "origin": "Köln",
    "budget": 900,
    "travelers": 2,
    "duration_days": 5,
    "date_from": "2026-09-10",
    "date_to": "2026-09-15",
    "search_mode": "QUICK",
}


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


def recheck_body_from(trip: dict) -> dict:
    """Turn a recommendation the API just returned into a re-check request.

    Deliberately built from the response the client actually receives - if the
    two shapes drift apart, this stops compiling before anyone's browser does.
    """
    return {
        "trip_id": trip["id"],
        "travelers": 2,
        "saved_price": trip["total_price"],
        "legs": [
            {
                "from": leg["from"],
                "to": leg["to"],
                "departure": leg["departure"],
                "operator": leg["operator"],
                "price_per_person": leg["price_per_person"],
            }
            for leg in trip["legs"]
        ],
        "stays": [
            {
                "city": stay["city"],
                "arrival": stay["arrival"],
                "departure": stay["departure"],
                "name": stay["name"],
                "cost": stay["cost"],
            }
            for stay in trip["stays"]
        ],
        "transfers": (
            [{"cost": trip["costs"]["ground_transfer"]}]
            if trip["costs"]["ground_transfer"]
            else []
        ),
    }


def test_a_trip_from_a_real_search_can_be_re_checked(client):
    """End to end, on the synthetic network, in the client's own shapes."""
    found = client.post("/api/v1/search", json=SEARCH).json()
    assert found["recommendations"], "need a trip to re-check"
    trip = found["recommendations"][0]

    response = client.post("/api/v1/trips/recheck", json=recheck_body_from(trip))

    assert response.status_code == 200
    body = response.json()
    assert body["trip_id"] == trip["id"]
    # The synthetic providers are deterministic by contract, so a re-check of
    # an unmoved world has to come back unchanged. If this ever fails, either
    # determinism broke or the re-check stopped matching what it re-quoted.
    assert body["status"] == "UNCHANGED"
    assert body["current_price"] == trip["total_price"]
    assert body["price_change"] == 0.0


def test_the_response_carries_copy_for_the_status(client):
    found = client.post("/api/v1/search", json=SEARCH).json()
    body = client.post(
        "/api/v1/trips/recheck", json=recheck_body_from(found["recommendations"][0])
    ).json()

    assert body["message"]
    assert body["legs"] and body["stays"]


def test_an_outage_surfaces_as_unverifiable_over_http(client):
    """The rule, asserted where the frontend will actually meet it."""
    app = create_app()
    app.dependency_overrides[get_planner] = lambda: build(StubTransport(explode=True))
    local = TestClient(app)

    body = local.post(
        "/api/v1/trips/recheck",
        json={
            "trip_id": "t1",
            "travelers": 2,
            "saved_price": SAVED_TOTAL,
            "legs": [
                {
                    "from": leg["origin"],
                    "to": leg["destination"],
                    "departure": leg["departure"].isoformat(),
                    "operator": leg["operator"],
                    "price_per_person": leg["price_per_person"],
                }
                for leg in LEGS
            ],
            "stays": [
                {
                    "city": s["city"],
                    "arrival": s["arrival"].isoformat(),
                    "departure": s["departure"].isoformat(),
                    "name": s["name"],
                    "cost": s["cost"],
                }
                for s in STAYS
            ],
        },
    ).json()

    assert body["status"] == "UNVERIFIABLE"
    assert body["current_price"] is None
    # And the outage is reported as an issue, not swallowed.
    assert body["issues"], "an infrastructure failure must reach the client"
    assert body["issues"][0]["retryable"] is True


def test_a_trip_with_no_legs_is_rejected(client):
    """There is nothing to re-check, and pretending otherwise would answer."""
    response = client.post(
        "/api/v1/trips/recheck",
        json={"trip_id": "t", "travelers": 2, "saved_price": 100.0, "legs": []},
    )

    assert response.status_code == 422


def test_a_request_that_leaves_out_a_priced_part_is_refused(client):
    """The regression test for a phantom price drop.

    A trip's ground transfer is part of its total. A re-check that re-quotes
    only the legs and stays reassembles a smaller number, and the difference
    would be reported as a saving on a trip where nothing whatsoever had
    changed. The parts must reconcile with the saved total or no comparison is
    offered at all.
    """
    found = client.post("/api/v1/search", json=SEARCH).json()
    trip = next(
        t for t in found["recommendations"] if t["costs"]["ground_transfer"] > 0
    )

    complete = recheck_body_from(trip)
    incomplete = {**complete, "transfers": []}

    assert client.post("/api/v1/trips/recheck", json=complete).json()["status"] == (
        "UNCHANGED"
    )

    body = client.post("/api/v1/trips/recheck", json=incomplete).json()
    assert body["status"] == "UNVERIFIABLE"
    assert body["current_price"] is None
    assert body["price_change"] is None


def test_transfers_are_reported_as_carried_not_as_checked(client):
    """We include them in the total; we must not imply we re-quoted them."""
    found = client.post("/api/v1/search", json=SEARCH).json()
    trip = next(
        t for t in found["recommendations"] if t["costs"]["ground_transfer"] > 0
    )

    body = client.post("/api/v1/trips/recheck", json=recheck_body_from(trip)).json()

    assert body["transfers"], "the transfer should appear as its own component"
    carried = body["transfers"][0]
    assert carried["state"] == "CARRIED"
    assert "not re-checked" in carried["detail"]


def test_a_trip_with_no_transfer_needs_none_declared(client):
    """Reconciliation must not demand a component the trip does not have."""
    found = client.post("/api/v1/search", json=SEARCH).json()
    trip = next(
        (t for t in found["recommendations"] if t["costs"]["ground_transfer"] == 0),
        None,
    )
    if trip is None:
        pytest.skip("this search returned no transfer-free trip")

    body = client.post("/api/v1/trips/recheck", json=recheck_body_from(trip)).json()

    assert body["status"] == "UNCHANGED"


def test_a_carried_transfer_does_not_keep_a_dead_trip_alive(client):
    """Every flight and room gone is UNAVAILABLE, transfer or no transfer.

    The transfer is carried forward at its saved price and never re-quoted, so
    it is not evidence that anything survived. Counting it as a live component
    would downgrade a trip with nothing left to book into "partially"
    unavailable, on the strength of a bus fare nobody checked.
    """
    from detoura.services.recheck import recheck_trip

    planner = build(StubTransport(offer=False), StubRooms(offer=False))
    result = recheck_trip(
        planner,
        legs=LEGS,
        stays=STAYS,
        transfers=[{"cost": 20.0}],
        travelers=2,
        saved_price=SAVED_TOTAL + 20.0,
    )

    assert result.status is RecheckStatus.UNAVAILABLE
