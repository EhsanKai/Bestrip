"""Pareto frontier utility."""

from __future__ import annotations

from datetime import datetime

from travel_planner.algorithms.pareto import dominates, pareto_filter
from travel_planner.algorithms.scoring import Objectives, ScoringEngine

from .conftest import leg, make_state, trip_request


def objectives(cost, minutes, cities, preference) -> Objectives:
    return Objectives(
        cost=cost, travel_minutes=minutes, city_count=cities, preference_score=preference
    )


# ---------------------------------------------------------------------------
# dominates()
# ---------------------------------------------------------------------------
def test_strictly_better_on_everything_dominates():
    better = objectives(100, 200, 3, 0.9)
    worse = objectives(150, 300, 2, 0.5)
    assert dominates(better, worse)
    assert not dominates(worse, better)


def test_equal_vectors_do_not_dominate_each_other():
    """Domination needs a strict improvement somewhere."""
    a = objectives(100, 200, 2, 0.7)
    b = objectives(100, 200, 2, 0.7)
    assert not dominates(a, b)
    assert not dominates(b, a)


def test_one_strict_improvement_with_ties_elsewhere_dominates():
    cheaper = objectives(90, 200, 2, 0.7)
    dearer = objectives(100, 200, 2, 0.7)
    assert dominates(cheaper, dearer)


def test_trade_offs_are_not_dominated():
    """Cheap-but-slow and fast-but-pricey are both legitimate travel styles."""
    cheap_slow = objectives(90, 600, 2, 0.7)
    fast_pricey = objectives(150, 200, 2, 0.7)
    assert not dominates(cheap_slow, fast_pricey)
    assert not dominates(fast_pricey, cheap_slow)


def test_more_cities_counts_as_better():
    two = objectives(100, 200, 2, 0.7)
    three = objectives(100, 200, 3, 0.7)
    assert dominates(three, two)
    assert not dominates(two, three)


# ---------------------------------------------------------------------------
# pareto_filter()
# ---------------------------------------------------------------------------
def test_filter_keeps_the_frontier_and_reports_dominators():
    items = {
        "best_cost": objectives(80, 400, 2, 0.6),
        "best_time": objectives(160, 120, 2, 0.6),
        "dominated": objectives(200, 500, 1, 0.5),
    }
    keys = list(items)
    result = pareto_filter(keys, lambda key: items[key])

    assert set(result.frontier) == {"best_cost", "best_time"}
    assert [removed for removed, _ in result.dominated] == ["dominated"]
    dominator = result.dominated[0][1]
    assert dominator in {"best_cost", "best_time"}


def test_filter_preserves_input_order():
    items = [objectives(100 + i, 200, 2, 0.7) for i in range(5)]
    result = pareto_filter(list(range(5)), lambda i: items[i])
    assert result.frontier == sorted(result.frontier)


def test_filter_on_an_empty_input():
    result = pareto_filter([], lambda item: objectives(0, 0, 0, 0.0))
    assert result.frontier == []
    assert result.dominated == []


def test_filter_never_drops_everything():
    items = [objectives(100, 200, 2, 0.7), objectives(90, 300, 2, 0.7)]
    result = pareto_filter([0, 1], lambda i: items[i])
    assert len(result.frontier) == 2


# ---------------------------------------------------------------------------
# Integration with real itineraries
# ---------------------------------------------------------------------------
def test_a_strictly_worse_itinerary_is_filtered_out(config, destinations):
    engine = ScoringEngine(config, destinations)
    request = trip_request(travelers=1, preferred_destinations=[])

    good = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 40.0),
            leg("Prague", "DUS", datetime(2026, 9, 14, 8, 0), 75, 40.0),
        ]
    )
    worse = make_state(
        [
            leg("CGN", "Prague", datetime(2026, 9, 10, 8, 0), 120, 60.0),
            leg("Prague", "CGN", datetime(2026, 9, 14, 8, 0), 120, 60.0),
        ]
    )
    result = pareto_filter(
        [good, worse], lambda state: engine.objectives(state, request)
    )
    assert result.frontier == [good]
    assert result.dominated[0][0] is worse


def test_planner_reports_pareto_filtering(planner, koln_request):
    result = planner.plan(koln_request, debug=True)
    assert result.debug is not None
    assert result.debug.pareto_input > result.debug.pareto_kept > 0
    pareto_records = [
        record for record in result.debug.filtered if record.stage.value == "PARETO"
    ]
    assert pareto_records
    assert pareto_records[0].dominated_by


def test_pareto_can_be_disabled(transport, destinations, config, koln_request):
    from travel_planner.services.planner import TravelPlanner

    off = TravelPlanner(
        transport,
        destinations,
        config=config.model_copy(update={"enable_pareto": False}),
    )
    result = off.plan(koln_request, debug=True)
    assert result.debug.pareto_input == result.debug.pareto_kept
    assert result.recommendations
