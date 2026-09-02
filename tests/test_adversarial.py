"""The V2 adversarial suite.

One test per numbered scenario in the spec's section 36, plus the date-handling
cases. These are deliberately end-to-end: they assert on what the planner
actually returns, not on internals.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from detoura.config import PlannerConfig
from detoura.models.trip import TravelPreferences
from detoura.profiles import PROFILES, ProfileName
from detoura.services.planner import TravelPlanner

from .conftest import (
    TRAP_ROUTE_COSTS,
    leg,
    make_state,
    trip_request,
)


@pytest.fixture(scope="module")
def default_result():
    """The house scenario, planned once under BEST_VALUE."""
    planner = TravelPlanner()
    request = trip_request()
    return planner, request, planner.plan(request, debug=True)


# ---------------------------------------------------------------------------
# 1. Cheapest first leg is not the best complete route
# ---------------------------------------------------------------------------
def test_01_cheapest_first_leg_is_not_the_best_route(trap):
    """The V1 guarantee, still holding after the V2 rewrite."""
    result = trap.planner.plan(trap.request)
    best = result.recommendations[0]
    assert best.cities == ["Prague", "Vienna"]
    assert best.total_cost == TRAP_ROUTE_COSTS[("Prague", "Vienna")]
    # It starts with the *expensive* first leg.
    assert best.legs[0].destination == "Prague"
    assert best.legs[0].price_per_person == 55.0
    cheapest_first_leg = min(
        trap.transport.search("DUS", city, date(2026, 9, 10))[0].price_per_person
        for city in ("London", "Prague")
    )
    assert cheapest_first_leg == 35.0  # London


# ---------------------------------------------------------------------------
# 2. Cheap flight + expensive hotel loses
# ---------------------------------------------------------------------------
def test_02_cheap_flight_expensive_hotel_loses(stay_trap):
    result = stay_trap.planner.plan(stay_trap.request)
    best = result.recommendations[0]
    london = next(i for i in result.recommendations if i.cities == ["London"])
    assert best.cities == ["Prague"]
    assert london.cost_breakdown.transport < best.cost_breakdown.transport
    assert london.total_cost > best.total_cost


# ---------------------------------------------------------------------------
# 3. Ground transfer flips the departure airport
# ---------------------------------------------------------------------------
def test_03_ground_transfer_flips_the_airport(transfer_trap):
    result = transfer_trap.planner.plan(transfer_trap.request)
    assert result.recommendations[0].origin_airport == "CGN"
    assert result.recommendations[0].total_cost == 100.0


# ---------------------------------------------------------------------------
# 4 & 5 & 6. Trip length and appetite for cities
# ---------------------------------------------------------------------------
def test_04_a_two_day_trip_loses_to_a_meaningful_five_day_trip(default_result):
    """Nobody asking for five days should be handed 40 hours away."""
    _, request, result = default_result
    for itinerary in result.recommendations:
        assert itinerary.duration_days >= 0.6 * request.duration_days
        assert itinerary.value_breakdown.usable_ratio > 0.2


def test_05_a_multi_city_lover_gets_more_cities():
    planner = TravelPlanner()
    keen = planner.plan(
        trip_request(budget=650, preferences=TravelPreferences(multiple_cities=1.0))
    )
    assert any(len(i.cities) >= 2 for i in keen.recommendations)


def test_06_a_one_city_lover_is_not_forced_to_move():
    planner = TravelPlanner()
    homebody = planner.plan(
        trip_request(budget=650, preferences=TravelPreferences(multiple_cities=0.0))
    )
    keen = planner.plan(
        trip_request(budget=650, preferences=TravelPreferences(multiple_cities=1.0))
    )
    assert homebody.recommendations
    assert len(homebody.recommendations[0].cities) == 1
    homebody_cities = sum(len(i.cities) for i in homebody.recommendations)
    keen_cities = sum(len(i.cities) for i in keen.recommendations)
    assert homebody_cities < keen_cities


# ---------------------------------------------------------------------------
# 7 & 8. Mandatory and forbidden destinations
# ---------------------------------------------------------------------------
def test_07_mandatory_london_is_always_visited():
    planner = TravelPlanner()
    result = planner.plan(trip_request(budget=700, must_visit=["London"]))
    assert result.recommendations
    for itinerary in result.recommendations:
        assert "London" in itinerary.cities


def test_08_paris_is_excluded_everywhere():
    planner = TravelPlanner()
    result = planner.plan(trip_request(budget=700, avoid_destinations=["Paris"]))
    assert result.recommendations
    for itinerary in result.recommendations:
        assert "Paris" not in itinerary.cities
        assert all("Paris" not in (l.origin, l.destination) for l in itinerary.legs)


# ---------------------------------------------------------------------------
# 9. Party size drives both cost lines
# ---------------------------------------------------------------------------
def test_09_four_travelers_cost_more_in_transport_and_rooms():
    """Party size drives every cost line.

    The room choice is pinned to the cheapest tier here on purpose: with the
    V3 default the optimizer may buy a *better* room for one person than for
    four on the same budget, which is correct behaviour but would make this
    test about tier selection rather than about party pricing.
    """
    planner = TravelPlanner(config=PlannerConfig(accommodation_options_per_stay=1))
    solo = planner.plan(trip_request(travelers=1, budget=900))
    four = planner.plan(trip_request(travelers=4, budget=900))
    assert solo.recommendations and four.recommendations

    # Transport is strictly per head.
    assert four.recommendations[0].cost_breakdown.transport > (
        solo.recommendations[0].cost_breakdown.transport
    )
    # Accommodation rises too, because four people need more than one room.
    assert four.recommendations[0].cost_breakdown.accommodation > (
        solo.recommendations[0].cost_breakdown.accommodation
    )
    # ... and so does the ride to the airport, which is priced per ticket.
    assert four.recommendations[0].cost_breakdown.ground_transfer > (
        solo.recommendations[0].cost_breakdown.ground_transfer
    )


def test_09b_rooms_scale_in_steps_not_per_head(accommodation):
    """Two people share a double; the third person needs a second room."""
    options = accommodation.search("Prague", date(2026, 9, 10), date(2026, 9, 12), 2)
    double = next(o for o in options if o.capacity == 2)
    assert double.total_price(1) == double.total_price(2)
    assert double.total_price(3) == 2 * double.total_price(2)


# ---------------------------------------------------------------------------
# 10 & 11. Date flexibility
# ---------------------------------------------------------------------------
def test_10_flexible_dates_explore_more_departures():
    planner = TravelPlanner()
    fixed = planner.plan(trip_request(date_flexible=False))
    flexible = planner.plan(trip_request(date_flexible=True))
    assert fixed.metadata.start_dates == ["2026-09-10"]
    assert len(flexible.metadata.start_dates) > 1
    # More departure dates cannot make the answer worse.
    assert flexible.recommendations[0].score >= fixed.recommendations[0].score


def test_10b_flexible_dates_can_find_a_cheaper_departure():
    """A wider window must never cost more than a narrower one."""
    planner = TravelPlanner()
    narrow = planner.plan(
        trip_request(date_flexible=False, profile=ProfileName.CHEAPEST)
    )
    wide = planner.plan(
        trip_request(
            date_from=date(2026, 9, 8),
            date_to=date(2026, 9, 20),
            date_flexible=True,
            profile=ProfileName.CHEAPEST,
        )
    )
    assert len(wide.metadata.start_dates) > len(narrow.metadata.start_dates)
    assert wide.recommendations[0].total_cost <= narrow.recommendations[0].total_cost


def test_11_fixed_dates_do_not_move():
    planner = TravelPlanner()
    result = planner.plan(trip_request(date_flexible=False))
    assert result.metadata.start_dates == ["2026-09-10"]
    for itinerary in result.recommendations:
        assert itinerary.departure.date() == date(2026, 9, 10)


def test_11b_the_window_is_not_the_trip_length():
    """A 5-day trip inside a 10-day window is 5 days, not 10."""
    planner = TravelPlanner()
    result = planner.plan(
        trip_request(date_from=date(2026, 9, 8), date_to=date(2026, 9, 18), duration_days=5)
    )
    assert result.recommendations
    for itinerary in result.recommendations:
        assert itinerary.duration_days <= 5
        for option in itinerary.legs:
            assert date(2026, 9, 8) <= option.departure.date() <= date(2026, 9, 18)


# ---------------------------------------------------------------------------
# 12 & 13. Arrival and departure times
# ---------------------------------------------------------------------------
def test_12_a_late_arrival_reduces_usable_time():
    early = make_state(
        [
            leg("DUS", "London", datetime(2026, 9, 10, 9, 0), 90, 35.0),
            leg("London", "DUS", datetime(2026, 9, 13, 18, 0), 90, 35.0),
        ]
    )
    late = make_state(
        [
            leg("DUS", "London", datetime(2026, 9, 10, 21, 30), 90, 35.0),
            leg("London", "DUS", datetime(2026, 9, 13, 18, 0), 90, 35.0),
        ]
    )
    assert late.usable_destination_minutes < early.usable_destination_minutes


def test_13_an_early_departure_reduces_usable_time():
    late_out = make_state(
        [
            leg("DUS", "London", datetime(2026, 9, 10, 9, 0), 90, 35.0),
            leg("London", "DUS", datetime(2026, 9, 13, 18, 0), 90, 35.0),
        ]
    )
    dawn_out = make_state(
        [
            leg("DUS", "London", datetime(2026, 9, 10, 9, 0), 90, 35.0),
            leg("London", "DUS", datetime(2026, 9, 13, 6, 0), 90, 35.0),
        ]
    )
    assert dawn_out.usable_destination_minutes < late_out.usable_destination_minutes


# ---------------------------------------------------------------------------
# 14 & 15. Cheap is not the same as good
# ---------------------------------------------------------------------------
def test_14_a_cheap_three_city_slog_ranks_below_a_sane_trip(destinations, config):
    from detoura.algorithms.travel_value import TravelValueScorer

    scorer = TravelValueScorer(config, destinations)
    request = trip_request(travelers=1, budget=400, preferred_destinations=[])
    profile = PROFILES[ProfileName.BEST_VALUE]

    slog = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 6, 0), 600, 10.0),
            leg("Prague", "Rome", datetime(2026, 9, 11, 6, 0), 700, 10.0),
            leg("Rome", "Madrid", datetime(2026, 9, 12, 6, 0), 800, 10.0),
            leg("Madrid", "DUS", datetime(2026, 9, 13, 6, 0), 700, 10.0),
        ],
        travelers=1,
    )
    sane = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 75, 40.0),
            leg("Prague", "Vienna", datetime(2026, 9, 12, 10, 0), 240, 25.0),
            leg("Vienna", "DUS", datetime(2026, 9, 14, 18, 0), 95, 40.0),
        ],
        travelers=1,
    )
    assert slog.total_cost < sane.total_cost
    assert scorer.total(slog, request, profile) < scorer.total(sane, request, profile)


def test_15_a_single_city_trip_can_be_the_recommendation():
    """The optimizer must not force multi-city travel."""
    planner = TravelPlanner()
    result = planner.plan(
        trip_request(budget=650, preferences=TravelPreferences(multiple_cities=0.0))
    )
    assert result.recommendations[0].cities and len(result.recommendations[0].cities) == 1


# ---------------------------------------------------------------------------
# 16 & 17. Profiles and diversity
# ---------------------------------------------------------------------------
def test_16_profiles_produce_different_rankings():
    planner = TravelPlanner()
    request = trip_request()
    rankings = {
        name: [i.route_label() for i in planner.plan(request, profile=name).recommendations]
        for name in ProfileName
    }
    assert len({tuple(r) for r in rankings.values()}) > 1
    assert rankings[ProfileName.CHEAPEST] != rankings[ProfileName.ADVENTURE]


def test_17_top_recommendations_are_genuinely_diverse(default_result):
    _, _, result = default_result
    signatures = [frozenset(i.cities) for i in result.recommendations]
    assert len(signatures) == len(set(signatures))
    labels = [i.route_label() for i in result.recommendations]
    assert len(labels) == len(set(labels))


# ---------------------------------------------------------------------------
# 18 - 20. The hard constraints, under every profile
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("profile", list(ProfileName))
def test_18_no_itinerary_exceeds_the_budget(profile):
    planner = TravelPlanner()
    request = trip_request()
    result = planner.plan(request, profile=profile)
    assert result.recommendations
    for itinerary in result.recommendations:
        assert itinerary.total_cost <= request.budget
        assert itinerary.cost_breakdown.total == pytest.approx(
            itinerary.total_cost, abs=0.02
        )


@pytest.mark.parametrize("profile", list(ProfileName))
def test_19_no_itinerary_exceeds_the_duration(profile):
    planner = TravelPlanner()
    request = trip_request()
    result = planner.plan(request, profile=profile)
    for itinerary in result.recommendations:
        assert itinerary.duration_days <= request.duration_days
        elapsed = (itinerary.arrival - itinerary.departure).total_seconds() / 60
        assert elapsed <= request.max_trip_minutes


@pytest.mark.parametrize("profile", list(ProfileName))
def test_20_every_itinerary_returns_to_an_origin_airport(profile):
    planner = TravelPlanner()
    result = planner.plan(trip_request(), profile=profile)
    airports = set(result.metadata.origin_airports)
    for itinerary in result.recommendations:
        assert itinerary.route_nodes[0] in airports
        assert itinerary.route_nodes[-1] in airports


# ---------------------------------------------------------------------------
# Regression guards
# ---------------------------------------------------------------------------
def test_the_v1_economics_are_still_reproducible(v1_planner):
    """Turning both V2 cost lines off gives transport-only itineraries again."""
    result = v1_planner.plan(trip_request(budget=250))
    assert result.recommendations
    for itinerary in result.recommendations:
        assert itinerary.cost_breakdown.accommodation == 0.0
        assert itinerary.cost_breakdown.ground_transfer == 0.0
        assert itinerary.total_cost == itinerary.cost_breakdown.transport


def test_determinism_across_profiles_and_repeats():
    planner = TravelPlanner()
    request = trip_request()
    for profile in ProfileName:
        runs = [planner.plan(request, profile=profile) for _ in range(3)]
        assert runs[0].recommendations == runs[1].recommendations == runs[2].recommendations


def test_planning_stays_interactive_with_the_v2_cost_model():
    planner = TravelPlanner()
    result = planner.plan(trip_request())
    assert result.metadata.elapsed_seconds < 5.0


def test_pareto_and_diversity_still_filter(default_result):
    """Both post-processing stages still do real work under the V3 model.

    V3 splits similarity filtering into an exact route-duplicate pass and the
    Jaccard pass; either one firing is evidence the pipeline de-duplicates.
    """
    _, _, result = default_result
    assert result.debug.pareto_input > result.debug.pareto_kept > 0
    stages = {record.stage.value for record in result.debug.filtered}
    assert "PARETO" in stages
    assert stages & {"DIVERSITY", "DUPLICATE_ROUTE"}
