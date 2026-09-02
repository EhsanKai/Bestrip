"""The adaptive beam: stop guessing a width, measure one (V4).

V3 shipped a fixed beam and its README admitted the default missed better
itineraries - ``beam_width=40`` found a trip that the default 20 never reached.
Widening the guess would only have moved the problem. This widens until
widening stops paying, and records what each rung bought.
"""

from __future__ import annotations

import pytest

from detoura.config import PlannerConfig
from detoura.profiles import ProfileName
from detoura.services.planner import TravelPlanner

from .conftest import trip_request


def request_for_the_ladder():
    """A request with enough room that a wider beam has something to find."""
    return trip_request(budget=450, duration_days=5, travelers=2, date_flexible=True)


@pytest.fixture(scope="module")
def fixed():
    return TravelPlanner(config=PlannerConfig()).plan(request_for_the_ladder())


@pytest.fixture(scope="module")
def adaptive():
    return TravelPlanner(config=PlannerConfig(adaptive_beam=True)).plan(
        request_for_the_ladder()
    )


# ---------------------------------------------------------------------------
# It is off unless asked for
# ---------------------------------------------------------------------------
def test_the_beam_is_fixed_by_default():
    """V3's published numbers must stay reproducible."""
    assert PlannerConfig().adaptive_beam is False


def test_a_fixed_run_reports_no_ladder(fixed):
    assert fixed.metadata.beam_rounds == []
    assert fixed.metadata.beam_width == fixed.metadata.configured_beam_width * len(
        fixed.metadata.start_dates
    )


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------
def test_it_climbs_and_reports_every_rung(adaptive):
    rounds = adaptive.metadata.beam_rounds
    assert len(rounds) >= 2
    widths = [r["beam_width"] for r in rounds]
    assert widths == sorted(widths)
    assert widths == [widths[0] * 2**i for i in range(len(widths))]
    for rung in rounds:
        assert rung["completed"] > 0
        assert rung["states_generated"] > 0


def test_a_wider_beam_never_finds_a_worse_best(adaptive):
    """The property that makes the stopping rule safe."""
    scores = [r["best_score"] for r in adaptive.metadata.beam_rounds]
    assert scores == sorted(scores), scores


def test_each_rung_explores_more_than_the_last(adaptive):
    completed = [r["completed"] for r in adaptive.metadata.beam_rounds]
    assert completed == sorted(completed)
    assert completed[-1] > completed[0]


def test_the_recorded_improvement_matches_the_scores(adaptive):
    rounds = adaptive.metadata.beam_rounds
    assert rounds[0]["improvement"] == 0.0
    for previous, current in zip(rounds, rounds[1:]):
        assert current["improvement"] == pytest.approx(
            current["best_score"] - previous["best_score"], abs=1e-6
        )


def test_it_stops_when_widening_stops_paying(adaptive):
    """The stopping rule, asserted rather than assumed.

    The climb ends either because the last rung gained less than the tolerance
    or because it reached the configured ceiling - and never for any other
    reason.
    """
    config = PlannerConfig(adaptive_beam=True)
    rounds = adaptive.metadata.beam_rounds
    last = rounds[-1]
    stalled = len(rounds) > 1 and last["improvement"] <= config.adaptive_beam_tolerance
    capped = last["beam_width"] >= config.adaptive_beam_max_width
    exhausted = len(rounds) == config.adaptive_beam_max_rounds
    assert stalled or capped or exhausted


def test_every_rung_before_the_last_paid_its_way(adaptive):
    """No rung is climbed after one that already failed the tolerance."""
    config = PlannerConfig(adaptive_beam=True)
    for rung in adaptive.metadata.beam_rounds[1:-1]:
        assert rung["improvement"] > config.adaptive_beam_tolerance


# ---------------------------------------------------------------------------
# It actually does the thing V3 said was missing
# ---------------------------------------------------------------------------
def test_it_finds_a_better_itinerary_than_the_fixed_default(fixed, adaptive):
    """The whole point. V3 could only report this gap; V4 closes it."""
    assert adaptive.recommendations[0].score > fixed.recommendations[0].score
    assert adaptive.metadata.beam_width > fixed.metadata.beam_width


def test_the_ladders_first_rung_reproduces_the_fixed_search(fixed, adaptive):
    """Rung one *is* the V3 search, so the comparison is like for like."""
    first = adaptive.metadata.beam_rounds[0]
    assert first["beam_width"] == fixed.metadata.beam_width
    assert first["best_score"] == pytest.approx(
        fixed.recommendations[0].score, abs=1e-6
    )
    assert first["completed"] == fixed.metadata.completed_itineraries


# ---------------------------------------------------------------------------
# Cost is reported honestly
# ---------------------------------------------------------------------------
def test_the_reported_cost_includes_the_discarded_rounds(adaptive):
    """Reporting only the final round would understate what the ladder cost."""
    ladder = sum(r["states_generated"] for r in adaptive.metadata.beam_rounds)
    assert adaptive.metadata.states_generated == ladder
    assert adaptive.metadata.states_generated > adaptive.metadata.beam_rounds[-1][
        "states_generated"
    ]


def test_the_trace_describes_the_search_that_produced_the_answer(adaptive):
    """Intermediate rounds are cost, not the run - they must not be narrated."""
    planner = TravelPlanner(config=PlannerConfig(adaptive_beam=True))
    result = planner.plan(request_for_the_ladder(), debug=True)
    final_width = result.metadata.beam_rounds[-1]["beam_width"]
    assert result.debug.effective_beam_width == final_width
    for iteration in result.debug.iterations:
        assert iteration.beam_width == final_width
        assert iteration.kept <= final_width


def test_it_reuses_the_provider_caches_across_rounds():
    """A widened round must re-search, not re-fetch."""
    planner = TravelPlanner(config=PlannerConfig(adaptive_beam=True))
    request = request_for_the_ladder()
    first = planner.plan(request).metadata.provider_metrics["misses"]
    again = planner.plan(request).metadata.provider_metrics["misses"]
    assert again == 0
    assert first > 0


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
def test_the_round_ceiling_is_honoured():
    planner = TravelPlanner(
        config=PlannerConfig(adaptive_beam=True, adaptive_beam_max_rounds=2)
    )
    result = planner.plan(request_for_the_ladder())
    assert len(result.metadata.beam_rounds) <= 2


def test_the_width_ceiling_is_honoured():
    planner = TravelPlanner(
        config=PlannerConfig(
            adaptive_beam=True, adaptive_beam_max_width=45, adaptive_beam_max_rounds=5
        )
    )
    result = planner.plan(request_for_the_ladder())
    for rung in result.metadata.beam_rounds:
        assert rung["beam_width"] <= 45


def test_the_tolerance_alone_no_longer_stops_the_ladder():
    """V5.2.1 changed this behaviour on purpose.

    In V4 a generous tolerance stopped the climb after two rounds, because the
    score was the only signal. The spec's instruction - *"do not stop solely on
    raw score improvement"* - makes that wrong: a round that raises the score by
    nothing and finds three city combinations nobody had seen is exactly the
    round "search deeper" exists to run.

    So the tolerance is now one signal of three, and setting it absurdly high
    no longer ends the search on its own. What ends it is a round that buys
    nothing on *any* axis - asserted in the test below.
    """
    planner = TravelPlanner(
        config=PlannerConfig(adaptive_beam=True, adaptive_beam_tolerance=1.0)
    )
    result = planner.plan(request_for_the_ladder())
    rounds = result.metadata.beam_rounds
    assert len(rounds) > 2
    # It kept climbing because widening kept finding things, not by accident.
    assert any(
        r["new_city_sets"] > 0 or r["competitive_found"] > 0 for r in rounds[1:]
    )


def test_the_ladder_stops_when_a_round_discovers_nothing():
    """The replacement stopping rule, asserted directly."""
    planner = TravelPlanner(config=PlannerConfig(adaptive_beam=True))
    result = planner.plan(request_for_the_ladder())
    rounds = result.metadata.beam_rounds
    final = rounds[-1]
    if final["stop_reason"] == "diminishing_returns":
        assert final["improvement"] <= PlannerConfig().adaptive_beam_tolerance
        assert final["new_city_sets"] == 0
        assert final["competitive_found"] == 0
    else:
        # Any other ending must name itself rather than being a silent stop.
        assert final["stop_reason"] in {
            "width_ceiling",
            "rounds_exhausted",
            "time_budget",
        }


def test_a_time_budget_ends_the_climb():
    """DEEP promises a duration; an unbounded ladder cannot keep that promise."""
    planner = TravelPlanner(
        config=PlannerConfig(
            adaptive_beam=True,
            adaptive_beam_time_budget_seconds=0.01,
            adaptive_beam_max_rounds=5,
        )
    )
    result = planner.plan(request_for_the_ladder())
    rounds = result.metadata.beam_rounds
    # One full round always runs: the budget decides whether the *next* one
    # starts, so it can never truncate a round mid-flight.
    assert len(rounds) >= 1
    assert rounds[-1]["stop_reason"] in {"time_budget", "diminishing_returns"}
    assert result.recommendations


def test_it_stays_deterministic():
    planner = TravelPlanner(config=PlannerConfig(adaptive_beam=True))
    request = request_for_the_ladder()
    first = planner.plan(request)
    second = planner.plan(request)
    assert [i.route_label() for i in first.recommendations] == [
        i.route_label() for i in second.recommendations
    ]
    assert first.metadata.beam_rounds == second.metadata.beam_rounds


def test_it_respects_the_hard_constraints():
    """A wider search must not buy quality by breaking the request."""
    planner = TravelPlanner(config=PlannerConfig(adaptive_beam=True))
    request = request_for_the_ladder()
    for profile in ProfileName:
        result = planner.plan(request, profile=profile)
        for itinerary in result.recommendations:
            assert itinerary.total_cost <= request.budget
            assert "Paris" not in itinerary.cities
            assert len(set(itinerary.cities)) == len(itinerary.cities)


def test_an_infeasible_request_does_not_climb_forever():
    """Nothing to find at any width: the ladder must not keep paying to look."""
    planner = TravelPlanner(config=PlannerConfig(adaptive_beam=True))
    result = planner.plan(trip_request(budget=30))
    assert result.recommendations == []
    assert len(result.metadata.beam_rounds) == 2  # one climb, no gain, stop
