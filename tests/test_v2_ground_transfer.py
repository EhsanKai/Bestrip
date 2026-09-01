"""Origin ground transfers: the journey starts at the user's front door."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from travel_planner.models.transfer import GroundTransferMode, GroundTransferOption
from travel_planner.providers.ground_transfer import (
    FreeGroundTransferProvider,
    GroundTransferProvider,
    RealGroundTransferProvider,
    SyntheticGroundTransferProvider,
)

from .conftest import completed_states, leg, make_state, transfer


def _completed_states(trap):
    """Every complete itinerary the search found, before any filtering."""
    return completed_states(trap.planner, trap.request)


# ---------------------------------------------------------------------------
# Model and provider
# ---------------------------------------------------------------------------
def test_transfer_price_is_per_person():
    option = transfer("Köln", "CGN", 5.0, 20)
    assert option.total_price(1) == 5.0
    assert option.total_price(4) == 20.0
    with pytest.raises(ValueError, match="travelers"):
        option.total_price(0)


def test_provider_satisfies_the_protocol(ground_transfer):
    assert isinstance(ground_transfer, GroundTransferProvider)


def test_koln_transfers_match_the_documented_table(ground_transfer):
    prices = {
        airport: ground_transfer.search("Köln", airport)[0]
        for airport in ("CGN", "DUS", "FRA")
    }
    assert prices["CGN"].price_per_person == 5.0
    assert prices["CGN"].duration_minutes == 20
    assert prices["DUS"].price_per_person == 15.0
    assert prices["DUS"].duration_minutes == 55
    assert prices["FRA"].price_per_person == 30.0
    assert prices["FRA"].duration_minutes == 90


def test_nearer_airports_are_cheaper_and_quicker(ground_transfer):
    near = ground_transfer.search("Köln", "CGN")[0]
    far = ground_transfer.search("Köln", "FRA")[0]
    assert near.price_per_person < far.price_per_person
    assert near.duration_minutes < far.duration_minutes


def test_origin_spellings_resolve(ground_transfer):
    for spelling in ("Köln", "koeln", "COLOGNE"):
        assert ground_transfer.search(spelling, "CGN")[0].price_per_person == 5.0


def test_unknown_pairs_fall_back_to_distance(ground_transfer):
    """An origin with a known distance still gets a priced transfer."""
    options = ground_transfer.search("Aachen", "AMS")
    assert options and options[0].price_per_person > 0


def test_unknown_origin_has_no_transfer(ground_transfer):
    assert ground_transfer.search("Atlantis", "CGN") == []


def test_injected_table_is_authoritative():
    provider = SyntheticGroundTransferProvider(
        {"koln": {"CGN": (GroundTransferMode.TRAIN, 5.0, 20)}}
    )
    assert provider.search("Köln", "CGN")[0].price_per_person == 5.0
    # No distance fallback when a table is supplied.
    assert provider.search("Köln", "FRA") == []


def test_free_provider_is_the_v1_behaviour():
    assert FreeGroundTransferProvider().search("Köln", "CGN") == []


def test_real_provider_is_an_unimplemented_placeholder():
    with pytest.raises(NotImplementedError, match="placeholder"):
        RealGroundTransferProvider()


def test_search_is_deterministic(ground_transfer):
    assert ground_transfer.search("Köln", "DUS") == ground_transfer.search("Köln", "DUS")


# ---------------------------------------------------------------------------
# State integration
# ---------------------------------------------------------------------------
def _london_trip(travelers: int, out=None, back=None):
    legs = [
        leg("DUS", "London", datetime(2026, 9, 10, 8, 0), 90, 35.0),
        leg("London", "DUS", datetime(2026, 9, 12, 8, 0), 90, 35.0),
    ]
    return make_state(
        legs, travelers=travelers, outbound_transfer=out, return_transfer=back
    )


def test_transfers_are_added_to_cost_and_time():
    out = transfer("Köln", "DUS", 15.0, 55)
    state = _london_trip(2, out=out, back=out)
    assert state.transport_cost == 140.0
    assert state.ground_transfer_cost == 60.0  # 15 x 2 travelers, both ways
    assert state.total_cost == 200.0
    assert state.ground_transfer_minutes == 110
    # Intercity travel time stays separate from the ride to the airport.
    assert state.total_travel_minutes == 180
    assert state.total_transport_minutes == 290


def test_a_missing_transfer_costs_nothing():
    state = _london_trip(2)
    assert state.ground_transfer_cost == 0.0
    assert state.ground_transfer_minutes == 0
    assert state.total_cost == 140.0


# ---------------------------------------------------------------------------
# THE critical ground-transfer scenario (spec section 38)
# ---------------------------------------------------------------------------
def test_the_cheaper_flight_leaves_from_the_dearer_airport(transfer_trap):
    """Precondition: DUS has the cheaper flight, CGN the cheaper transfer."""
    day = date(2026, 9, 10)
    dus_flight = transfer_trap.transport.search("DUS", "London", day)[0]
    cgn_flight = transfer_trap.transport.search("CGN", "London", day)[0]
    assert dus_flight.price_per_person == 35.0 < cgn_flight.price_per_person == 45.0

    dus_ride = transfer_trap.transfers.search("Köln", "DUS")[0]
    cgn_ride = transfer_trap.transfers.search("Köln", "CGN")[0]
    assert cgn_ride.price_per_person == 5.0 < dus_ride.price_per_person == 30.0

    # One way, door to destination: DUS 65, CGN 50 - exactly the spec's figures.
    assert dus_ride.price_per_person + dus_flight.price_per_person == 65.0
    assert cgn_ride.price_per_person + cgn_flight.price_per_person == 50.0


def test_the_optimizer_picks_the_airport_that_wins_door_to_door(transfer_trap):
    """CGN's dearer flight beats DUS once the ride to the airport is paid for."""
    result = transfer_trap.planner.plan(transfer_trap.request)
    assert result.recommendations

    best = result.recommendations[0]
    assert best.origin_airport == "CGN"
    assert best.return_airport == "CGN"
    assert best.cost_breakdown.transport == 90.0  # 45 out + 45 back
    assert best.cost_breakdown.ground_transfer == 10.0  # 5 each way
    assert best.total_cost == 100.0

    # The DUS alternatives were explored - and then removed, because once the
    # ride to the airport is counted they are dearer *and* slower, which is
    # exactly what Pareto domination is for.
    completed = _completed_states(transfer_trap)
    dus_states = [s for s in completed if s.origin_airport == "DUS"]
    assert dus_states, "the DUS alternative must still be discovered by the search"
    for state in dus_states:
        assert state.total_cost > best.total_cost

    # The heart of it: the itinerary with the cheapest *flights* is not the
    # itinerary with the cheapest *journey*.
    cheapest_transport = min(completed, key=lambda s: s.transport_cost)
    assert cheapest_transport.transport_cost < best.cost_breakdown.transport
    assert cheapest_transport.total_cost > best.total_cost


def test_a_one_way_mix_of_airports_is_allowed(transfer_trap):
    """Out of one airport, home into another, is a move the search considers."""
    mixed = [
        s
        for s in _completed_states(transfer_trap)
        if s.origin_airport != s.current_location
    ]
    assert mixed, "the search should consider asymmetric airport pairs"
    for state in mixed:
        # 35 out of DUS + 45 home into CGN, or the mirror image.
        assert state.transport_cost == 80.0


def test_without_transfers_the_naive_airport_wins(transfer_trap):
    """Turn the feature off and the cheaper flight wins again - the V1 answer."""
    from travel_planner.providers.accommodation import NoAccommodationProvider
    from travel_planner.services.planner import TravelPlanner

    planner = TravelPlanner(
        transfer_trap.transport,
        transfer_trap.planner.destinations,
        config=transfer_trap.config.model_copy(
            update={"enable_ground_transfer": False}
        ),
        accommodation_provider=NoAccommodationProvider(),
        ground_transfer_provider=FreeGroundTransferProvider(),
    )
    result = planner.plan(transfer_trap.request)
    assert result.recommendations[0].origin_airport == "DUS"
    assert result.recommendations[0].total_cost == 70.0


def test_transfers_reach_the_itinerary(planner, koln_request):
    result = planner.plan(koln_request)
    for itinerary in result.recommendations:
        assert itinerary.cost_breakdown.ground_transfer > 0
        assert itinerary.ground_transfer_minutes > 0
        assert itinerary.total_transport_minutes > itinerary.total_travel_minutes


def test_transfer_option_is_immutable():
    option = transfer("Köln", "CGN", 5.0, 20)
    with pytest.raises(Exception):
        option.price_per_person = 99.0
    assert isinstance(option, GroundTransferOption)
