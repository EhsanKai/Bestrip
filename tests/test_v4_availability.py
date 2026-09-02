"""Inventory and availability (V4).

V3's own limitations section said it plainly: "rooms never sell out and flights
never fill; there is no inventory model", and ``free_cancellation`` was carried
but unpriced. Both are now modelled.

Two decisions run through all of this. **Unknown is not unlimited** - a feed
that quotes a fare without a seat count must stay bookable, or the engine
refuses to work with most of the real world. And **scarcity is deterministic** -
the counts are a fixed function of route, date and tier, because the engine's
determinism guarantee is worth more than a plausible-looking simulation.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from travel_planner.algorithms.accommodation_value import (
    CANCELLATION_BONUS,
    SCARCITY_CANCELLATION_BONUS,
    AccommodationScorer,
)
from travel_planner.config import PlannerConfig
from travel_planner.data.synthetic_accommodation import (
    INVENTORY_PER_TIER,
    build_options,
    rooms_left,
)
from travel_planner.data.synthetic_transport import CONNECTIONS, seats_left
from travel_planner.models.accommodation import (
    SCARCITY_HORIZON_ROOMS,
    AccommodationOption,
)
from travel_planner.models.debug import RejectionReason
from travel_planner.models.itinerary import ExplanationFactor
from travel_planner.models.transport import TransportOption, TransportType
from travel_planner.models.trip import AccommodationPreference
from travel_planner.providers.accommodation import SyntheticAccommodationDataProvider
from travel_planner.providers.transport import SyntheticTransportDataProvider
from travel_planner.services.planner import TravelPlanner

from .conftest import trip_request

CHECK_IN, CHECK_OUT = date(2026, 9, 10), date(2026, 9, 12)


def fare(seats: int | None) -> TransportOption:
    return TransportOption(
        id=f"fare-{seats}",
        origin="DUS",
        destination="Prague",
        departure=datetime(2026, 9, 10, 8, 0),
        arrival=datetime(2026, 9, 10, 9, 15),
        price_per_person=55.0,
        transport_type=TransportType.FLIGHT,
        duration_minutes=75,
        seats_available=seats,
    )


def bed(rooms: int | None, *, capacity: int = 2, refundable: bool = True):
    return AccommodationOption(
        id=f"bed-{rooms}",
        city="Prague",
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        price_per_night=60.0,
        capacity=capacity,
        rooms_available=rooms,
        free_cancellation=refundable,
    )


# ---------------------------------------------------------------------------
# Unknown is not unlimited
# ---------------------------------------------------------------------------
def test_an_uncounted_fare_is_bookable():
    """A feed that quotes no inventory must not be treated as sold out."""
    assert fare(None).has_seats_for(4) is True


def test_an_uncounted_room_is_bookable():
    assert bed(None).has_capacity_for(4) is True


def test_zero_is_a_real_sell_out():
    assert fare(0).has_seats_for(1) is False
    assert bed(0).has_capacity_for(1) is False


def test_seats_are_counted_per_traveler():
    assert fare(2).has_seats_for(2) is True
    assert fare(2).has_seats_for(3) is False


def test_rooms_are_counted_per_room_not_per_traveler():
    """Four travelers in doubles need two rooms, not four."""
    two_doubles = bed(2, capacity=2)
    assert two_doubles.has_capacity_for(4) is True
    assert two_doubles.has_capacity_for(5) is False


# ---------------------------------------------------------------------------
# Scarcity
# ---------------------------------------------------------------------------
def test_unknown_availability_is_not_scarce():
    """Absent data must not read as pressure to book."""
    assert bed(None).scarcity == 0.0


def test_scarcity_rises_as_rooms_run_out():
    values = [bed(n).scarcity for n in range(SCARCITY_HORIZON_ROOMS, -1, -1)]
    assert values == sorted(values)
    assert values[0] == 0.0
    assert values[-1] == 1.0


def test_plentiful_rooms_are_not_scarce():
    assert bed(SCARCITY_HORIZON_ROOMS * 3).scarcity == 0.0


# ---------------------------------------------------------------------------
# Refundability is now priced
# ---------------------------------------------------------------------------
def test_refundability_is_worth_more_when_the_room_is_nearly_gone(scorer=None):
    """The V3 gap: a flat bonus said cancelling the last room in town was worth
    exactly as much as cancelling one of forty."""
    scorer = AccommodationScorer()
    plentiful = scorer.score_option(bed(SCARCITY_HORIZON_ROOMS), AccommodationPreference.BALANCED)
    last_one = scorer.score_option(bed(1), AccommodationPreference.BALANCED)
    assert last_one > plentiful


def test_an_unpriced_feed_keeps_the_v3_flat_bonus():
    """No inventory data must reproduce V3 exactly, to the last decimal."""
    scorer = AccommodationScorer()
    refundable = scorer.score_option(bed(None), AccommodationPreference.BALANCED)
    firm = scorer.score_option(
        bed(None, refundable=False), AccommodationPreference.BALANCED
    )
    assert refundable - firm == pytest.approx(CANCELLATION_BONUS)


def test_the_scarcity_premium_is_bounded():
    scorer = AccommodationScorer()
    firm = scorer.score_option(
        bed(0, refundable=False), AccommodationPreference.BALANCED
    )
    desperate = scorer.score_option(bed(0), AccommodationPreference.BALANCED)
    assert desperate - firm == pytest.approx(
        CANCELLATION_BONUS + SCARCITY_CANCELLATION_BONUS
    )


def test_a_non_refundable_room_gains_nothing_from_scarcity():
    """Scarcity makes the *option* valuable, not the room."""
    scorer = AccommodationScorer()
    assert scorer.score_option(
        bed(1, refundable=False), AccommodationPreference.BALANCED
    ) == scorer.score_option(
        bed(20, refundable=False), AccommodationPreference.BALANCED
    )


# ---------------------------------------------------------------------------
# The synthetic inventory model
# ---------------------------------------------------------------------------
def test_the_counts_are_deterministic():
    assert rooms_left("Prague", CHECK_IN, 0) == rooms_left("Prague", CHECK_IN, 0)
    connection = CONNECTIONS[0]
    assert seats_left(connection, CHECK_IN, 0) == seats_left(connection, CHECK_IN, 0)


def test_the_counts_vary_by_city_date_and_tier():
    """A uniform simulation would teach the optimizer nothing."""
    by_city = {rooms_left(c, CHECK_IN, 0) for c in ("Prague", "Vienna", "Madrid")}
    by_date = {rooms_left("Prague", date(2026, 9, d), 0) for d in range(10, 17)}
    by_tier = {rooms_left("Prague", CHECK_IN, t) for t in range(3)}
    assert len(by_city) > 1 and len(by_date) > 1 and len(by_tier) > 1


def test_the_cheap_rooms_go_first():
    """Which is what makes scarcity bite: it is what most trips want."""
    for city in ("Prague", "Vienna", "Budapest"):
        budget, standard, comfort = (rooms_left(city, CHECK_IN, t) for t in range(3))
        assert budget < standard < comfort


def test_counts_never_go_negative():
    for city in ("Prague", "Vienna", "Madrid", "Zurich"):
        for day in range(1, 29):
            for tier in range(3):
                left = rooms_left(city, date(2026, 9, day), tier)
                assert 0 <= left <= INVENTORY_PER_TIER


def test_scarcity_is_off_unless_asked_for():
    """Every published V3 number assumed no inventory data. It still holds."""
    plain = build_options("Prague", CHECK_IN, CHECK_OUT)
    assert all(option.rooms_available is None for option in plain)
    counted = build_options("Prague", CHECK_IN, CHECK_OUT, simulate_scarcity=True)
    assert all(option.rooms_available is not None for option in counted)


def test_the_providers_pass_the_flag_through():
    rooms = SyntheticAccommodationDataProvider(simulate_scarcity=True)
    assert all(
        o.rooms_available is not None
        for o in rooms.search("Prague", CHECK_IN, CHECK_OUT, 2)
    )
    legs = SyntheticTransportDataProvider(simulate_scarcity=True)
    assert all(o.seats_available is not None for o in legs.search("DUS", "Prague", CHECK_IN))


def test_pricing_is_untouched_by_the_inventory_flag():
    """Scarcity must change what is bookable, never what it costs."""
    plain = build_options("Prague", CHECK_IN, CHECK_OUT)
    counted = build_options("Prague", CHECK_IN, CHECK_OUT, simulate_scarcity=True)
    assert [o.price_per_night for o in plain] == [o.price_per_night for o in counted]


# ---------------------------------------------------------------------------
# The search
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def scarce_planner() -> TravelPlanner:
    return TravelPlanner(
        transport_provider=SyntheticTransportDataProvider(simulate_scarcity=True),
        accommodation_provider=SyntheticAccommodationDataProvider(
            simulate_scarcity=True
        ),
    )


@pytest.fixture(scope="module")
def scarce(scarce_planner) -> object:
    return scarce_planner.plan(trip_request(budget=450, travelers=2), debug=True)


def rejections(result) -> dict[str, int]:
    counts: dict[str, int] = {}
    for iteration in result.debug.iterations:
        for reason, count in iteration.rejection_counts.items():
            counts[reason.value] = counts.get(reason.value, 0) + count
    return counts


def test_sold_out_transport_is_reported_not_silently_dropped(scarce):
    counts = rejections(scarce)
    assert counts.get(RejectionReason.SOLD_OUT_TRANSPORT.value, 0) > 0


def test_sold_out_accommodation_is_reported(scarce):
    counts = rejections(scarce)
    assert counts.get(RejectionReason.SOLD_OUT_ACCOMMODATION.value, 0) > 0


def test_a_sell_out_is_distinguished_from_having_no_hotels_at_all():
    """"No rooms in Prague" and "the last double went" are different facts."""
    planner = TravelPlanner(
        accommodation_provider=SyntheticAccommodationDataProvider(
            rates={"Prague": 40.0}, simulate_scarcity=True
        )
    )
    result = planner.plan(trip_request(budget=450, travelers=2), debug=True)
    counts = rejections(result)
    # Every other city has no rate at all -> nothing on offer.
    assert counts.get(RejectionReason.NO_ACCOMMODATION_AVAILABLE.value, 0) > 0


def test_scarcity_changes_which_trips_are_reachable(scarce):
    """If inventory made no difference it would not be worth modelling."""
    plain = TravelPlanner().plan(trip_request(budget=450, travelers=2))
    assert scarce.metadata.completed_itineraries < plain.metadata.completed_itineraries
    assert scarce.recommendations


def test_no_recommendation_books_something_sold_out(scarce):
    for itinerary in scarce.recommendations:
        for stay in itinerary.stays:
            if stay.rooms_available is not None and stay.accommodation_cost > 0:
                assert stay.rooms_available >= 1


def test_a_bigger_party_finds_less(scarce_planner):
    """Six people need three rooms and six seats. Inventory should bite harder."""
    small = scarce_planner.plan(trip_request(budget=1400, travelers=2))
    large = scarce_planner.plan(trip_request(budget=1400, travelers=6))
    assert (
        large.metadata.completed_itineraries < small.metadata.completed_itineraries
    )


class _DusOnly:
    """One departure airport, so the fixture's economics stay exact."""

    def resolve(self, origin: str, config: PlannerConfig):
        from travel_planner.services.origin_resolver import OriginCandidate

        return [OriginCandidate(code="DUS", name="Düsseldorf", city="Düsseldorf", distance_km=0.0)]


class OneSoldOutFare:
    """Two fares per leg: the cheapest is sold out, the other is not.

    A deliberately minimal provider rather than a corner of the synthetic
    network, because the property under test is exact: with room to branch on
    only one fare, the search must pick the bookable one.
    """

    def __init__(self, seats_on_the_cheap_fare: int = 0) -> None:
        self.seats = seats_on_the_cheap_fare

    def search(self, origin: str, destination: str, departure_date: date):
        routes = {("DUS", "Prague"), ("Prague", "DUS")}
        if (origin, destination) not in routes:
            return []
        depart = datetime.combine(departure_date, datetime.min.time()).replace(hour=9)
        return [
            TransportOption(
                id=f"{origin}{destination}{departure_date}-cheap",
                origin=origin,
                destination=destination,
                departure=depart,
                arrival=depart.replace(hour=10),
                price_per_person=40.0,
                transport_type=TransportType.FLIGHT,
                duration_minutes=60,
                seats_available=self.seats,
            ),
            TransportOption(
                id=f"{origin}{destination}{departure_date}-dear",
                origin=origin,
                destination=destination,
                departure=depart.replace(hour=11),
                arrival=depart.replace(hour=12),
                price_per_person=90.0,
                transport_type=TransportType.FLIGHT,
                duration_minutes=60,
                seats_available=20,
            ),
        ]


def one_fare_config(**overrides) -> PlannerConfig:
    return PlannerConfig(
        max_transport_options_per_leg=1,
        max_origin_distance_km=20.0,  # DUS only
        min_duration_utilization=0.0,
        enable_accommodation=False,
        enable_ground_transfer=False,
        **overrides,
    )


def test_a_sold_out_fare_does_not_consume_a_branching_slot():
    """Otherwise a sold-out cheap fare pushes a bookable one out of the search.

    Both fares exist and only one can be branched on. Excluding the sold-out
    one *after* the cap would leave the search with nothing at all.
    """
    planner = TravelPlanner(
        config=one_fare_config(),
        transport_provider=OneSoldOutFare(seats_on_the_cheap_fare=0),
        origin_resolver=_DusOnly(),
    )
    result = planner.plan(trip_request(budget=400, travelers=2))
    assert result.recommendations, "the bookable fare must survive the cap"
    assert result.recommendations[0].cost_breakdown.transport == 360.0  # 2 x 90 x 2


def test_the_cheap_fare_wins_when_it_is_actually_available():
    """The control: the same network with seats on the cheap fare."""
    planner = TravelPlanner(
        config=one_fare_config(),
        transport_provider=OneSoldOutFare(seats_on_the_cheap_fare=20),
        origin_resolver=_DusOnly(),
    )
    result = planner.plan(trip_request(budget=400, travelers=2))
    assert result.recommendations
    assert result.recommendations[0].cost_breakdown.transport == 160.0  # 2 x 40 x 2


def test_availability_can_be_switched_off(scarce_planner):
    """For a caller who intends to re-check at booking time."""
    off = TravelPlanner(
        config=PlannerConfig(require_availability=False),
        transport_provider=SyntheticTransportDataProvider(simulate_scarcity=True),
        accommodation_provider=SyntheticAccommodationDataProvider(
            simulate_scarcity=True
        ),
    )
    request = trip_request(budget=450, travelers=2)
    ignored = off.plan(request, debug=True)
    enforced = scarce_planner.plan(request)
    assert ignored.metadata.completed_itineraries > enforced.metadata.completed_itineraries
    assert not rejections(ignored).get(RejectionReason.SOLD_OUT_TRANSPORT.value)


def test_it_stays_deterministic(scarce_planner):
    request = trip_request(budget=450, travelers=2)
    first = scarce_planner.plan(request)
    second = scarce_planner.plan(request)
    assert [i.route_label() for i in first.recommendations] == [
        i.route_label() for i in second.recommendations
    ]


# ---------------------------------------------------------------------------
# What the caller is told
# ---------------------------------------------------------------------------
def test_the_stay_reports_what_is_left(scarce):
    booked = [
        stay
        for itinerary in scarce.recommendations
        for stay in itinerary.stays
        if stay.accommodation_cost > 0
    ]
    assert booked
    assert all(stay.rooms_available is not None for stay in booked)


def test_scarcity_is_explained_when_it_is_real(scarce):
    flagged = [
        itinerary
        for itinerary in scarce.recommendations
        if ExplanationFactor.LIMITED_AVAILABILITY in itinerary.explanation_factors
    ]
    for itinerary in flagged:
        counted = [s for s in itinerary.stays if s.rooms_available is not None]
        assert min(stay.rooms_available for stay in counted) <= 3


def test_availability_is_never_claimed_without_data():
    """An uncounted feed must say nothing rather than reassure."""
    result = TravelPlanner().plan(trip_request(budget=450, travelers=2))
    for itinerary in result.recommendations:
        assert ExplanationFactor.LIMITED_AVAILABILITY not in itinerary.explanation_factors
        assert all(stay.rooms_available is None for stay in itinerary.stays)


def test_refundability_is_reported(scarce):
    for itinerary in scarce.recommendations:
        booked = [s for s in itinerary.stays if s.accommodation_cost > 0]
        if booked and all(stay.free_cancellation for stay in booked):
            assert ExplanationFactor.FULLY_REFUNDABLE in itinerary.explanation_factors


PAYLOAD = {
    "origin": "Köln",
    "budget": 450,
    "travelers": 2,
    "duration_days": 5,
    "date_from": "2026-09-10",
    "date_to": "2026-09-15",
    "transport_preferences": ["flight", "train"],
}


def api_client(planner: TravelPlanner):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from travel_planner.api.app import create_app
    from travel_planner.api.routes import get_planner

    app = create_app()
    app.dependency_overrides[get_planner] = lambda: planner
    return TestClient(app)


def test_availability_reaches_the_api(scarce_planner):
    body = api_client(scarce_planner).post("/plan-trip", json=PAYLOAD).json()
    assert body["recommendations"]
    booked = [
        stay
        for itinerary in body["recommendations"]
        for stay in itinerary["stays"]
        if stay["accommodation_cost"] > 0
    ]
    assert booked
    assert all(stay["rooms_available"] >= 1 for stay in booked)


def test_the_api_omits_availability_it_does_not_have():
    """``response_model_exclude_none`` is the right behaviour here: an absent
    field is honest about unknown inventory, a null would invite a guess."""
    body = api_client(TravelPlanner()).post("/plan-trip", json=PAYLOAD).json()
    assert body["recommendations"]
    for itinerary in body["recommendations"]:
        for stay in itinerary["stays"]:
            assert "rooms_available" not in stay
