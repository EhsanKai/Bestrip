"""Beam search behaviour - including the mandatory non-greedy proof."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from travel_planner.algorithms.beam_search import BeamSearchOptimizer
from travel_planner.algorithms.scoring import ScoringEngine
from travel_planner.config import PlannerConfig
from travel_planner.constraints.validator import ConstraintValidator
from travel_planner.models.debug import SearchDebug
from travel_planner.services.return_estimator import CachedReturnEstimator

from .conftest import TRAP_ROUTE_COSTS, WINDOW_FROM, WINDOW_TO


# ---------------------------------------------------------------------------
# A deliberately greedy reference implementation
# ---------------------------------------------------------------------------
def greedy_cheapest_next_hop(trap, start_date: date = WINDOW_FROM) -> tuple[list[str], float]:
    """Always take the cheapest available next leg; go home when forced.

    This is the algorithm the optimizer must *not* be. It is implemented here
    rather than asserted from memory so the comparison is real.
    """
    request = trap.request
    provider = trap.transport
    cities = [d.id for d in trap.destinations.all()]
    location = "DUS"
    day = start_date
    visited: list[str] = []
    total = 0.0

    allowed = set(request.transport_preferences)

    while True:
        pool = []
        if len(visited) < trap.config.max_cities:
            for destination in cities:
                if destination in visited or destination == location:
                    continue
                pool.extend(provider.search(location, destination, day))
        # Only consider going home once at least one city has been seen.
        if visited:
            pool.extend(provider.search(location, "DUS", day))
        pool = [option for option in pool if option.transport_type in allowed]
        if not pool:
            break
        best = min(pool, key=lambda option: (option.price_per_person, option.id))
        total += best.total_price(request.travelers)
        location = best.destination
        day = best.arrival.date() + timedelta(days=trap.config.min_city_stay_days)
        if location == "DUS":
            break
        visited.append(location)
    return visited, round(total, 2)


# ---------------------------------------------------------------------------
# The mandatory test
# ---------------------------------------------------------------------------
def test_cheapest_first_leg_is_london(trap):
    """Precondition: the trap really does make London the cheapest first move."""
    first_legs = []
    for destination in ("London", "Prague"):
        first_legs.extend(trap.transport.search("DUS", destination, WINDOW_FROM))
    cheapest = min(first_legs, key=lambda o: o.price_per_person)
    assert cheapest.destination == "London"
    assert cheapest.price_per_person == 35.0
    prague = next(o for o in first_legs if o.destination == "Prague")
    assert prague.price_per_person == 55.0


def test_greedy_reference_picks_the_worse_trip(trap):
    """A cheapest-next-hop search commits to London and ends up paying more."""
    cities, total = greedy_cheapest_next_hop(trap)
    assert cities == ["London", "Brussels"]
    assert total == TRAP_ROUTE_COSTS[("London", "Brussels")] == 120.0
    assert total > TRAP_ROUTE_COSTS[("Prague", "Vienna")]


def test_optimizer_beats_the_cheapest_first_leg(trap):
    """THE required behaviour: the best complete itinerary wins, not the best leg.

    ``DUS -> Prague -> Vienna -> DUS`` costs 92 but starts with the *expensive*
    first leg (55 vs London's 35). It must come out on top.
    """
    result = trap.planner.plan(trap.request, debug=True)

    assert result.recommendations, "the trap network must yield feasible itineraries"
    best = result.recommendations[0]

    assert best.cities == ["Prague", "Vienna"]
    assert best.route_nodes == ["DUS", "Prague", "Vienna", "DUS"]
    assert best.total_cost == TRAP_ROUTE_COSTS[("Prague", "Vienna")] == 92.0
    # The winning trip's first leg is the more expensive one.
    assert best.legs[0].destination == "Prague"
    assert best.legs[0].price_per_person == 55.0

    # ... and the greedy alternative is present but ranked below it.
    routes = {tuple(itinerary.cities): itinerary for itinerary in result.recommendations}
    assert ("London", "Brussels") in routes
    assert routes[("London", "Brussels")].score < best.score
    assert routes[("London", "Brussels")].total_cost == 120.0


def test_all_trap_round_trips_are_discovered(trap):
    """The search explores alternative branches rather than one greedy path.

    Checked against the raw completed set: Pareto and diversity filtering are
    allowed to drop inferior branches from the final five, but the search must
    have found them.
    """
    completed = _run_trap_optimizer(trap)
    found = {state.cities for state in completed}
    assert set(TRAP_ROUTE_COSTS) <= found
    cheapest_per_route = {
        route: min(s.total_cost for s in completed if s.cities == route)
        for route in TRAP_ROUTE_COSTS
    }
    assert cheapest_per_route == TRAP_ROUTE_COSTS


# ---------------------------------------------------------------------------
# Beam mechanics
# ---------------------------------------------------------------------------
def _optimizer(config, transport, destinations, request, origin_airports, window):
    scoring = ScoringEngine(config, destinations)
    estimator = CachedReturnEstimator(
        transport, origin_airports=origin_airports, dates=window
    )
    validator = ConstraintValidator(
        config,
        origin_airports=origin_airports,
        destination_ids=[d.id for d in destinations.all()],
        return_estimator=estimator,
    )
    return BeamSearchOptimizer(
        config,
        transport_provider=transport,
        destination_provider=destinations,
        validator=validator,
        scoring=scoring,
        return_estimator=estimator,
    )


def _window() -> list[date]:
    days, day = [], WINDOW_FROM
    while day <= WINDOW_TO:
        days.append(day)
        day += timedelta(days=1)
    return days


def _run_trap_optimizer(trap, config: PlannerConfig | None = None):
    """Run beam search over the trap network and return the completed states."""
    active = config or trap.config
    optimizer = _optimizer(
        active, trap.transport, trap.destinations, trap.request, ["DUS"], _window()
    )
    return optimizer.search(
        trap.request, origin_airports=["DUS"], start_dates=[WINDOW_FROM]
    )


def test_beam_width_is_respected(transport, destinations, koln_request):
    """No iteration ever carries more than ``beam_width`` states forward."""
    config = PlannerConfig(beam_width=7)
    optimizer = _optimizer(
        config, transport, destinations, koln_request, ["CGN", "DUS"], _window()
    )
    trace = SearchDebug()
    optimizer.search(
        koln_request,
        origin_airports=["CGN", "DUS"],
        start_dates=[WINDOW_FROM],
        debug=trace,
    )
    assert trace.iterations
    for iteration in trace.iterations:
        assert iteration.kept <= config.beam_width


def test_beam_search_prunes_and_reports_it(transport, destinations, koln_request):
    """Pruning actually happens and is visible in the structured trace."""
    config = PlannerConfig(beam_width=5)
    optimizer = _optimizer(
        config, transport, destinations, koln_request, ["CGN", "DUS"], _window()
    )
    trace = SearchDebug()
    optimizer.search(
        koln_request,
        origin_airports=["CGN", "DUS"],
        start_dates=[WINDOW_FROM],
        debug=trace,
    )
    first = trace.iterations[0]
    assert first.generated > first.kept
    assert first.pruned_by_beam > 0
    assert first.pruned_examples
    assert "Beam pruning" in first.render()


def test_wider_beam_never_finds_fewer_itineraries(transport, destinations, koln_request):
    """A wider beam explores a superset-ish space; it must not do worse."""
    counts = []
    for width in (3, 25):
        config = PlannerConfig(beam_width=width)
        optimizer = _optimizer(
            config, transport, destinations, koln_request, ["CGN", "DUS"], _window()
        )
        completed = optimizer.search(
            koln_request,
            origin_airports=["CGN", "DUS"],
            start_dates=[WINDOW_FROM],
        )
        counts.append(len(completed))
    assert counts[1] > counts[0]


def test_search_is_deterministic(trap):
    """Same input, same dataset, same output - twice."""
    first = trap.planner.plan(trap.request)
    second = trap.planner.plan(trap.request)
    assert [i.route_label() for i in first.recommendations] == [
        i.route_label() for i in second.recommendations
    ]
    assert [i.score for i in first.recommendations] == [
        i.score for i in second.recommendations
    ]


def test_return_leg_is_mandatory(trap):
    """Every recommendation ends at an origin airport."""
    result = trap.planner.plan(trap.request)
    for itinerary in result.recommendations:
        assert itinerary.return_airport == "DUS"
        assert itinerary.legs[-1].destination == "DUS"
        assert itinerary.route_nodes[0] == "DUS"
        assert itinerary.route_nodes[-1] == "DUS"


def test_stay_durations_are_explored_not_hardcoded(trap):
    """The same city sequence is reachable with several different stay lengths."""
    completed = _run_trap_optimizer(trap)
    prague_only = {
        state.stay_days for state in completed if state.cities == ("Prague",)
    }
    assert len(prague_only) > 1, f"expected several stay lengths, got {prague_only}"


@pytest.mark.parametrize("max_cities", [1, 2, 3])
def test_max_cities_is_enforced(trap, max_cities):
    from travel_planner.services.planner import TravelPlanner

    config = trap.config.model_copy(update={"max_cities": max_cities})
    planner = TravelPlanner(trap.transport, trap.destinations, config=config)
    result = planner.plan(trap.request)
    assert result.recommendations
    for itinerary in result.recommendations:
        assert len(itinerary.cities) <= max_cities
