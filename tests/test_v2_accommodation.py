"""Accommodation: the model, the provider, and its effect on the search."""

from __future__ import annotations

from datetime import date

import pytest

from detoura.config import PlannerConfig
from detoura.models.accommodation import AccommodationOption, AccommodationTier
from detoura.models.debug import RejectionReason
from detoura.providers.accommodation import (
    AccommodationDataProvider,
    NoAccommodationProvider,
    RealAccommodationDataProvider,
    SyntheticAccommodationDataProvider,
)
from detoura.services.accommodation_estimator import CachedAccommodationEstimator

from .conftest import STAY_TRAP_RATES, leg, make_state, room, trip_request

CHECK_IN = date(2026, 9, 10)
CHECK_OUT = date(2026, 9, 13)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
def test_nights_and_total_price():
    stay = room("Prague", CHECK_IN, CHECK_OUT, 45.0)
    assert stay.nights == 3
    assert stay.rooms_required(1) == 1
    assert stay.rooms_required(2) == 1
    assert stay.total_price(2) == 135.0


def test_capacity_drives_the_number_of_rooms():
    """Four travelers in double rooms pay for two rooms."""
    stay = room("Prague", CHECK_IN, CHECK_OUT, 45.0, capacity=2)
    assert stay.rooms_required(3) == 2
    assert stay.rooms_required(4) == 2
    assert stay.rooms_required(5) == 3
    assert stay.total_price(4) == pytest.approx(45.0 * 3 * 2)


def test_a_stay_must_have_at_least_one_night():
    with pytest.raises(ValueError, match="check_out"):
        room("Prague", CHECK_IN, CHECK_IN, 45.0)


def test_zero_travelers_is_rejected():
    with pytest.raises(ValueError, match="travelers"):
        room("Prague", CHECK_IN, CHECK_OUT, 45.0).total_price(0)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
def test_provider_satisfies_the_protocol(accommodation):
    assert isinstance(accommodation, AccommodationDataProvider)


def test_every_city_offers_three_tiers(accommodation):
    options = accommodation.search("Prague", CHECK_IN, CHECK_OUT, 2)
    assert {option.tier for option in options} == set(AccommodationTier)
    # Cheapest first, deterministically.
    assert options == sorted(options, key=lambda o: (o.total_price(2), o.tier.value, o.id))
    assert options[0].tier is AccommodationTier.BUDGET


def test_cities_have_different_prices(accommodation):
    prague = accommodation.search("Prague", CHECK_IN, CHECK_OUT, 2)[0]
    copenhagen = accommodation.search("Copenhagen", CHECK_IN, CHECK_OUT, 2)[0]
    assert copenhagen.total_price(2) > prague.total_price(2)


def test_longer_stays_cost_more(accommodation):
    short = accommodation.search("Prague", CHECK_IN, date(2026, 9, 11), 2)[0]
    long = accommodation.search("Prague", CHECK_IN, date(2026, 9, 14), 2)[0]
    assert long.total_price(2) > short.total_price(2)
    assert long.nights == 4 and short.nights == 1


def test_search_is_cached_and_deterministic(accommodation):
    first = accommodation.search("Prague", CHECK_IN, CHECK_OUT, 2)
    second = accommodation.search("Prague", CHECK_IN, CHECK_OUT, 2)
    assert first == second


def test_no_nights_means_no_options(accommodation):
    assert accommodation.search("Prague", CHECK_IN, CHECK_IN, 2) == []


def test_injected_rates_are_authoritative():
    """A rate table pins the economics and hides every other city."""
    provider = SyntheticAccommodationDataProvider(
        STAY_TRAP_RATES, date_variation=False
    )
    assert provider.search("Madrid", CHECK_IN, CHECK_OUT, 1) == []
    london = provider.search("London", CHECK_IN, CHECK_OUT, 1)
    standard = next(o for o in london if o.tier is AccommodationTier.STANDARD)
    assert standard.price_per_night == 120.0


def test_null_provider_is_free():
    provider = NoAccommodationProvider()
    assert provider.search("Prague", CHECK_IN, CHECK_OUT, 2) == []
    assert provider.min_price_per_night("Prague", 2) == 0.0


def test_real_provider_is_an_unimplemented_placeholder():
    with pytest.raises(NotImplementedError, match="placeholder"):
        RealAccommodationDataProvider()


# ---------------------------------------------------------------------------
# Estimator (the pruning bound)
# ---------------------------------------------------------------------------
def test_estimator_bound_is_admissible(accommodation):
    """No bookable stay may be cheaper than the bound used for pruning."""
    estimator = CachedAccommodationEstimator(accommodation)
    for city in ("Prague", "London", "Copenhagen"):
        bound = estimator.min_stay_cost(city, 3, 2)
        cheapest = accommodation.search(city, CHECK_IN, CHECK_OUT, 2)[0]
        assert bound <= cheapest.total_price(2) + 1e-9


def test_estimator_scales_with_nights_and_party(accommodation):
    estimator = CachedAccommodationEstimator(accommodation)
    one_night = estimator.min_stay_cost("Prague", 1, 2)
    assert estimator.min_stay_cost("Prague", 3, 2) == pytest.approx(one_night * 3)
    assert estimator.min_stay_cost("Prague", 1, 4) > one_night
    assert estimator.min_stay_cost("Prague", 0, 2) == 0.0


# ---------------------------------------------------------------------------
# State integration
# ---------------------------------------------------------------------------
def _london_trip(travelers: int = 1, nightly: float | None = None):
    legs = [
        leg("DUS", "London", __import__("datetime").datetime(2026, 9, 10, 8, 0), 90, 35.0),
        leg("London", "DUS", __import__("datetime").datetime(2026, 9, 12, 8, 0), 90, 35.0),
    ]
    rooms = (
        {"London": room("London", date(2026, 9, 10), date(2026, 9, 12), nightly)}
        if nightly is not None
        else None
    )
    return make_state(legs, travelers=travelers, rooms=rooms)


def test_accommodation_is_part_of_the_total_cost():
    without = _london_trip()
    with_hotel = _london_trip(nightly=120.0)
    assert without.total_cost == 70.0
    assert with_hotel.transport_cost == 70.0
    assert with_hotel.accommodation_cost == 240.0
    assert with_hotel.total_cost == 310.0


def test_state_records_the_stay():
    state = _london_trip(nightly=120.0)
    assert len(state.stays) == 1
    stay = state.stays[0]
    assert stay.city == "London"
    assert stay.nights == 2
    assert stay.accommodation_cost == 240.0
    assert stay.accommodation is not None
    assert state.stay_days == (2,)


def test_travelers_scale_accommodation_when_rooms_are_needed():
    solo = _london_trip(travelers=1, nightly=120.0)
    pair = _london_trip(travelers=2, nightly=120.0)
    quad = _london_trip(travelers=4, nightly=120.0)
    # One double room covers one or two people; four people need two rooms.
    assert solo.accommodation_cost == pair.accommodation_cost == 240.0
    assert quad.accommodation_cost == 480.0
    # Transport, by contrast, scales with every head.
    assert pair.transport_cost == 140.0
    assert quad.transport_cost == 280.0


# ---------------------------------------------------------------------------
# THE critical accommodation scenario (spec section 37)
# ---------------------------------------------------------------------------
def test_transport_alone_would_choose_london(stay_trap):
    """Precondition: London really is the cheapest place to fly."""
    day = date(2026, 9, 10)
    london = stay_trap.transport.search("DUS", "London", day)[0]
    prague = stay_trap.transport.search("DUS", "Prague", day)[0]
    assert london.price_per_person == 35.0 < prague.price_per_person == 55.0

    # ... and the cheapest room there is the most expensive in the network.
    rooms = {
        city: stay_trap.accommodation.search(city, day, date(2026, 9, 12), 1)[0]
        for city in STAY_TRAP_RATES
    }
    assert rooms["London"].total_price(1) > rooms["Prague"].total_price(1)


def _two_night_room_cost(stay_trap, city: str) -> float:
    """What the search will actually pay for two nights - the cheapest tier."""
    options = stay_trap.accommodation.search(
        city, date(2026, 9, 10), date(2026, 9, 12), 1
    )
    return options[0].total_price(1)


def test_the_optimizer_prices_the_whole_trip_not_just_the_flights(stay_trap):
    """A cheap flight into an expensive city must lose.

    London: 70 of transport + two nights at 120/night (list) = the dearer trip.
    Prague: 110 of transport + two nights at 40/night = the cheaper one.
    """
    result = stay_trap.planner.plan(stay_trap.request)
    assert result.recommendations

    best = result.recommendations[0]
    assert best.cities == ["Prague"]
    assert best.cost_breakdown.transport == 110.0
    assert best.cost_breakdown.accommodation == _two_night_room_cost(stay_trap, "Prague")
    assert best.total_cost == pytest.approx(
        110.0 + _two_night_room_cost(stay_trap, "Prague")
    )

    by_city = {tuple(i.cities): i for i in result.recommendations}
    assert ("London",) in by_city, "the London trip must still be discovered"
    london = by_city[("London",)]
    assert london.cost_breakdown.transport == 70.0
    assert london.total_cost == pytest.approx(
        70.0 + _two_night_room_cost(stay_trap, "London")
    )

    # The point of the whole exercise: cheaper transport, dearer trip, lower score.
    assert london.cost_breakdown.transport < best.cost_breakdown.transport
    assert london.total_cost > best.total_cost
    assert london.score < best.score


def test_accommodation_can_be_switched_off(stay_trap):
    """With stays free, the V1 answer comes back - London wins on transport."""
    from detoura.providers.ground_transfer import FreeGroundTransferProvider
    from detoura.services.planner import TravelPlanner

    planner = TravelPlanner(
        stay_trap.transport,
        stay_trap.planner.destinations,
        config=stay_trap.config.model_copy(update={"enable_accommodation": False}),
        accommodation_provider=NoAccommodationProvider(),
        ground_transfer_provider=FreeGroundTransferProvider(),
    )
    result = planner.plan(stay_trap.request)
    assert result.recommendations[0].cities == ["London"]
    assert result.recommendations[0].total_cost == 70.0


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------
def test_budget_pruning_accounts_for_accommodation(stay_trap):
    """A budget that covers the flights but not a bed rejects the state."""
    # London costs 35 out; a night there is 78 at the cheapest tier. A budget
    # of 100 can buy the flight and then strand the traveler.
    result = stay_trap.planner.plan(
        stay_trap.request.model_copy(update={"budget": 100.0}), debug=True
    )
    assert result.recommendations == []
    reasons = {
        reason
        for iteration in result.debug.iterations
        for reason in iteration.rejection_counts
    }
    assert RejectionReason.UNAFFORDABLE_ACCOMMODATION in reasons


def test_a_city_with_no_rooms_is_reported_not_silently_skipped():
    """An unbookable required stay is a rejection with a reason."""
    from detoura.data.destinations import DESTINATIONS
    from detoura.providers.destinations import StaticDestinationProvider
    from detoura.providers.ground_transfer import FreeGroundTransferProvider
    from detoura.providers.transport import SyntheticTransportDataProvider
    from detoura.services.planner import TravelPlanner

    from .conftest import STAY_TRAP_CONNECTIONS

    planner = TravelPlanner(
        SyntheticTransportDataProvider(STAY_TRAP_CONNECTIONS, price_variation=False),
        StaticDestinationProvider(
            [d for d in DESTINATIONS if d.id in STAY_TRAP_RATES]
        ),
        config=PlannerConfig(
            max_cities=2,
            min_city_stay_days=2,
            max_city_stay_days=2,
            max_origin_distance_km=20.0,
            min_duration_utilization=0.0,
            enable_ground_transfer=False,
        ),
        # Every city is bookable except through this empty table.
        accommodation_provider=SyntheticAccommodationDataProvider({}),
        ground_transfer_provider=FreeGroundTransferProvider(),
    )
    request = trip_request(
        origin="Düsseldorf",
        travelers=1,
        budget=400.0,
        date_flexible=False,
        preferred_destinations=[],
        avoid_destinations=[],
    )
    result = planner.plan(request, debug=True)
    assert result.recommendations == []
    counts = {
        reason: count
        for iteration in result.debug.iterations
        for reason, count in iteration.rejection_counts.items()
    }
    assert counts.get(RejectionReason.NO_ACCOMMODATION_AVAILABLE, 0) > 0


def test_accommodation_reaches_the_api(planner, koln_request):
    result = planner.plan(koln_request)
    assert result.recommendations
    for itinerary in result.recommendations:
        assert itinerary.cost_breakdown.accommodation > 0
        assert itinerary.stays
        assert sum(s.accommodation_cost for s in itinerary.stays) == pytest.approx(
            itinerary.cost_breakdown.accommodation, abs=0.02
        )
        for stay in itinerary.stays:
            assert stay.accommodation_tier in {t.value for t in AccommodationTier}


def test_option_branching_is_configurable(stay_trap):
    """Raising the per-stay branching lets the search trade room quality."""
    from detoura.services.planner import TravelPlanner
    from detoura.providers.ground_transfer import FreeGroundTransferProvider

    tiers = TravelPlanner(
        stay_trap.transport,
        stay_trap.planner.destinations,
        config=stay_trap.config.model_copy(
            update={"accommodation_options_per_stay": 3}
        ),
        accommodation_provider=stay_trap.accommodation,
        ground_transfer_provider=FreeGroundTransferProvider(),
    )
    wide = tiers.plan(stay_trap.request)
    narrow = stay_trap.planner.plan(stay_trap.request)
    assert (
        wide.metadata.completed_itineraries > narrow.metadata.completed_itineraries
    )
    # The cheapest answer is unchanged; there are simply more alternatives.
    assert wide.recommendations[0].total_cost == narrow.recommendations[0].total_cost


def test_accommodation_option_is_immutable():
    stay = room("Prague", CHECK_IN, CHECK_OUT, 45.0)
    with pytest.raises(Exception):
        stay.price_per_night = 1.0
    assert isinstance(stay, AccommodationOption)
