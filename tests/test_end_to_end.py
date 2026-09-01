"""The end-to-end scenario required by the spec.

Origin Köln, 2 travelers, 5 days, prefers Madrid, avoids Paris,
multiple_cities = 0.9. The budget is the V2 fixture default: once
accommodation and ground transfers are priced in, the V1 figure of EUR 250
buys nothing at all.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from travel_planner.config import PlannerConfig
from travel_planner.models.trip import TravelPreferences
from travel_planner.profiles import ProfileName
from travel_planner.services.planner import TravelPlanner

from .conftest import WINDOW_FROM, WINDOW_TO, trip_request


@pytest.fixture(scope="module")
def scenario():
    """The Köln scenario, planned once and inspected by many assertions."""
    planner = TravelPlanner()
    request = trip_request()
    return planner, request, planner.plan(request, debug=True)


# ---------------------------------------------------------------------------
# Required behaviours, one assertion block per bullet of the spec
# ---------------------------------------------------------------------------
def test_a_madrid_baseline_is_generated(scenario):
    _, _, result = scenario
    assert result.baseline is not None
    assert result.baseline.destination == "Madrid"
    assert result.baseline.total_cost <= 450
    assert len(result.baseline.legs) == 2
    # V2: the baseline is a trip the user could actually take, hotel and
    # airport transfer included - not a one-night flying visit.
    assert result.baseline.nights >= 2
    assert result.baseline.cost_breakdown.accommodation > 0
    assert result.baseline.cost_breakdown.ground_transfer > 0


def test_multiple_valid_itineraries_are_returned(scenario):
    _, _, result = scenario
    assert 2 <= len(result.recommendations) <= 5
    assert [i.rank for i in result.recommendations] == list(
        range(1, len(result.recommendations) + 1)
    )
    scores = [i.score for i in result.recommendations]
    assert scores == sorted(scores, reverse=True)


def test_alternative_destinations_are_searched(scenario):
    """The answer is not just "Madrid": other destinations are explored."""
    _, _, result = scenario
    visited = {city for i in result.recommendations for city in i.cities}
    assert len(visited) >= 3
    assert visited - {"Madrid"}


def test_multi_city_combinations_are_searched(scenario):
    _, _, result = scenario
    assert any(len(i.cities) >= 2 for i in result.recommendations), [
        i.cities for i in result.recommendations
    ]


def test_both_cgn_and_dus_are_considered(scenario):
    _, _, result = scenario
    assert {"CGN", "DUS"} <= set(result.metadata.origin_airports)
    # And both actually show up as departure or return nodes in the search.
    airports = set()
    for iteration in result.debug.iterations:
        for route in iteration.kept_routes:
            airports.add(route.split(" -> ")[0])
    assert {"CGN", "DUS"} <= airports


def test_beam_search_was_actually_used(scenario):
    _, _, result = scenario
    assert result.metadata.beam_width == 20
    assert result.debug.iterations
    assert result.metadata.states_generated > 100
    assert any(it.pruned_by_beam > 0 for it in result.debug.iterations)
    assert all(it.kept <= 20 for it in result.debug.iterations)


def test_paris_is_avoided_everywhere(scenario):
    _, _, result = scenario
    for itinerary in result.recommendations:
        assert "Paris" not in itinerary.cities
        assert all("Paris" not in (leg.origin, leg.destination) for leg in itinerary.legs)
    # It was generated and explicitly rejected, not merely absent.
    reasons = {
        reason
        for iteration in result.debug.iterations
        for reason in iteration.rejection_counts
    }
    assert any(reason.value == "AVOIDED_DESTINATION" for reason in reasons)


def test_the_budget_is_respected(scenario):
    _, request, result = scenario
    for itinerary in result.recommendations:
        assert itinerary.total_cost <= request.budget
        # The transport share really is the per-person price times two ...
        transport = sum(leg.price_per_person * request.travelers for leg in itinerary.legs)
        assert itinerary.cost_breakdown.transport == pytest.approx(transport, abs=0.02)
        # ... and the total is transport plus the two V2 cost lines.
        assert itinerary.total_cost == pytest.approx(
            itinerary.cost_breakdown.total, abs=0.02
        )


def test_the_duration_is_respected(scenario):
    _, request, result = scenario
    for itinerary in result.recommendations:
        assert itinerary.duration_days <= request.duration_days
        elapsed = (itinerary.arrival - itinerary.legs[0].departure).total_seconds() / 60
        assert elapsed <= request.max_trip_minutes


def test_every_leg_stays_inside_the_requested_dates(scenario):
    _, request, result = scenario
    for itinerary in result.recommendations:
        for option in itinerary.legs:
            assert WINDOW_FROM <= option.departure.date() <= WINDOW_TO
            assert WINDOW_FROM <= option.arrival.date() <= WINDOW_TO
        assert itinerary.departure.date() in {
            date.fromisoformat(d) for d in result.metadata.start_dates
        }


def test_every_itinerary_returns_to_an_origin_airport(scenario):
    _, _, result = scenario
    airports = set(result.metadata.origin_airports)
    for itinerary in result.recommendations:
        assert itinerary.route_nodes[0] in airports
        assert itinerary.route_nodes[-1] in airports
        assert itinerary.return_airport in airports


def test_recommendations_are_compared_against_the_baseline(scenario):
    _, _, result = scenario
    for itinerary in result.recommendations:
        comparison = itinerary.baseline_comparison
        assert comparison is not None
        assert comparison.baseline_destination == "Madrid"
        assert comparison.money_saved == round(
            result.baseline.total_cost - itinerary.total_cost, 2
        )


def test_at_least_one_recommendation_beats_the_baseline_on_price(scenario):
    _, _, result = scenario
    assert any(i.baseline_comparison.money_saved > 0 for i in result.recommendations)


def test_no_city_is_visited_twice_within_an_itinerary(scenario):
    _, _, result = scenario
    for itinerary in result.recommendations:
        assert len(itinerary.cities) == len(set(itinerary.cities))


def test_transport_preferences_are_honoured(scenario):
    _, request, result = scenario
    allowed = set(request.transport_preferences)
    for itinerary in result.recommendations:
        for option in itinerary.legs:
            assert option.transport_type in allowed


def test_the_run_is_deterministic():
    """Same request, same dataset, byte-identical answer."""
    request = trip_request()
    first = TravelPlanner().plan(request)
    second = TravelPlanner().plan(request)
    assert first.recommendations == second.recommendations
    assert first.baseline == second.baseline


def test_the_search_trace_is_inspectable(scenario):
    _, _, result = scenario
    rendered = result.debug.render()
    assert "Iteration 1" in rendered
    assert "Beam pruning" in rendered
    assert "Pareto" in rendered
    assert result.debug.total_generated > 0
    assert result.debug.total_rejected > 0
    first = result.debug.iterations[0]
    assert first.rejected_examples[0].reason
    assert first.rejected_examples[0].detail


# ---------------------------------------------------------------------------
# Variations on the scenario
# ---------------------------------------------------------------------------
def test_must_visit_forces_the_destination():
    planner = TravelPlanner()
    result = planner.plan(trip_request(budget=400, must_visit=["London"]))
    assert result.recommendations
    for itinerary in result.recommendations:
        assert "London" in itinerary.cities


def test_must_visit_an_unreachable_city_yields_nothing_rather_than_a_wrong_trip():
    planner = TravelPlanner()
    result = planner.plan(trip_request(budget=60, must_visit=["Madrid"]))
    assert result.recommendations == []


def test_a_tiny_budget_yields_no_recommendations():
    planner = TravelPlanner()
    result = planner.plan(trip_request(budget=15))
    assert result.recommendations == []
    assert result.baseline is None
    assert result.metadata.warnings


def test_more_travelers_shrink_the_option_space():
    """The same budget buys less for four people than for one."""
    planner = TravelPlanner()
    solo = planner.plan(trip_request(travelers=1))
    four = planner.plan(trip_request(travelers=4))
    assert solo.metadata.completed_itineraries > four.metadata.completed_itineraries


def test_a_multi_city_lover_gets_more_cities_than_someone_who_wants_one_place():
    planner = TravelPlanner()
    keen = planner.plan(
        trip_request(
            budget=400, preferences=TravelPreferences(multiple_cities=1.0)
        )
    )
    homebody = planner.plan(
        trip_request(
            budget=400, preferences=TravelPreferences(multiple_cities=0.0)
        )
    )
    keen_cities = sum(len(i.cities) for i in keen.recommendations)
    homebody_cities = sum(len(i.cities) for i in homebody.recommendations)
    assert keen_cities > homebody_cities


def test_inflexible_dates_use_only_the_first_start_date():
    planner = TravelPlanner()
    result = planner.plan(trip_request(date_flexible=False))
    assert result.metadata.start_dates == ["2026-09-10"]
    for itinerary in result.recommendations:
        assert itinerary.legs[0].departure.date() == date(2026, 9, 10)


def test_planning_completes_quickly(scenario):
    """The optimizer is a search, not a brute force: it must stay interactive."""
    _, _, result = scenario
    assert result.metadata.elapsed_seconds < 5.0


def test_configuration_flows_through_to_the_result():
    planner = TravelPlanner(config=PlannerConfig(beam_width=8, max_results=2, max_cities=2))
    result = planner.plan(trip_request())
    assert result.metadata.beam_width == 8
    assert result.metadata.max_cities == 2
    assert len(result.recommendations) <= 2
    for itinerary in result.recommendations:
        assert len(itinerary.cities) <= 2


def test_score_breakdown_is_attached_to_every_recommendation(scenario):
    """The V2 Travel Value drives the rank; the V1 breakdown rides along."""
    _, _, result = scenario
    for itinerary in result.recommendations:
        value = itinerary.value_breakdown
        assert value is not None
        assert value.total == pytest.approx(itinerary.score, abs=1e-6)
        assert value.profile is ProfileName.BEST_VALUE
        assert value.weights["experience"] == pytest.approx(0.30)

        legacy = itinerary.score_breakdown
        assert legacy is not None
        assert legacy.weights["budget"] == pytest.approx(0.40)


def test_the_result_serializes_to_json(scenario):
    _, _, result = scenario
    payload = result.model_dump(mode="json")
    assert set(payload) >= {"baseline", "recommendations", "metadata"}
    assert isinstance(payload["recommendations"][0]["legs"][0]["departure"], str)
    assert datetime.fromisoformat(payload["recommendations"][0]["departure"])
