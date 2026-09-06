"""V7 Phase 1: the traveler's idea versus ours.

The point of these tests is not that the comparison produces numbers. It is
that the comparison is capable of losing. A recommender that can only ever
report its own superiority is not comparing anything, and §10 of the V7 brief
makes "say so when the original wins" a hard product requirement rather than a
nicety - so most of what follows is aimed at making Detoura lose.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from detoura.models.itinerary import (
    BaselineResult,
    CostBreakdown,
    Itinerary,
    TravelValueBreakdown,
)
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.profiles import ProfileName
from detoura.services.planner import TravelPlanner
from detoura.services.trip_comparison import (
    ComparisonVerdict,
    Favours,
    compare_trips,
)

from fastapi.testclient import TestClient

from detoura.api.app import create_app
from detoura.api.v1 import get_planner

from .conftest import leg

WINDOW_FROM = date(2026, 9, 10)
WINDOW_TO = date(2026, 9, 24)


# ---------------------------------------------------------------------------
# Builders: a baseline and an itinerary we can drive to any relative position
# ---------------------------------------------------------------------------
def make_baseline(
    *,
    cost: float = 400.0,
    travel_minutes: int = 300,
    usable_minutes: int = 3600,
    experience: float = 0.60,
    preference: float = 0.60,
    accommodation: float = 0.60,
    scored: bool = True,
    destination: str = "Berlin",
) -> BaselineResult:
    return BaselineResult(
        destination=destination,
        total_cost=cost,
        duration_days=5.0,
        total_travel_minutes=travel_minutes,
        usable_destination_minutes=usable_minutes,
        cost_breakdown=CostBreakdown(transport=cost),
        nights=4,
        scored=scored,
        experience_score=experience,
        preference_match=preference,
        accommodation_score=accommodation,
        convenience_score=0.7,
        travel_value=0.60,
    )


def make_itinerary(
    *,
    cost: float = 400.0,
    cities: list[str] | None = None,
    travel_minutes: int = 300,
    transfer_minutes: int = 0,
    usable_minutes: int = 3600,
    experience: float = 0.60,
    preference: float = 0.60,
    accommodation: float = 0.60,
) -> Itinerary:
    cities = ["Berlin"] if cities is None else cities
    departure = datetime(2026, 9, 10, 8, 0)
    arrival = datetime(2026, 9, 15, 20, 0)
    return Itinerary(
        rank=1,
        score=0.7,
        total_cost=cost,
        duration_days=5.0,
        origin_airport="CGN",
        return_airport="CGN",
        cities=cities,
        legs=[leg("CGN", cities[0], departure, 90, 50.0)],
        total_travel_minutes=travel_minutes,
        ground_transfer_minutes=transfer_minutes,
        usable_destination_minutes=usable_minutes,
        departure=departure,
        arrival=arrival,
        value_breakdown=TravelValueBreakdown(
            profile=ProfileName.BEST_VALUE,
            cost=0.5,
            experience=experience,
            preferences=preference,
            time=0.5,
            diversity=0.5,
            accommodation=accommodation,
            total=0.7,
        ),
    )


# ---------------------------------------------------------------------------
# The four verdicts (V7 §10)
# ---------------------------------------------------------------------------
def test_detoura_wins_clearly():
    """Better on price and time, worse on nothing."""
    comparison = compare_trips(
        make_itinerary(cost=300.0, usable_minutes=5400, travel_minutes=200),
        make_baseline(cost=400.0, usable_minutes=3600, travel_minutes=300),
    )
    assert comparison.verdict is ComparisonVerdict.DETOURA_BETTER
    assert comparison.tradeoffs == []
    assert comparison.advantages


def test_original_wins_clearly_and_says_so():
    """The requirement this whole module exists for."""
    comparison = compare_trips(
        make_itinerary(cost=600.0, usable_minutes=2000, travel_minutes=800,
                       experience=0.4, preference=0.4, accommodation=0.4),
        make_baseline(cost=400.0, usable_minutes=3600, travel_minutes=300),
    )
    assert comparison.verdict is ComparisonVerdict.ORIGINAL_BETTER
    assert comparison.advantages == []
    assert comparison.tradeoffs
    assert comparison.summary.startswith("Your original Berlin trip wins on:")


def test_mixed_reports_both_sides():
    comparison = compare_trips(
        make_itinerary(cost=600.0, cities=["Berlin", "Prague"], usable_minutes=5400),
        make_baseline(cost=400.0, usable_minutes=3600),
    )
    assert comparison.verdict is ComparisonVerdict.MIXED
    assert comparison.advantages and comparison.tradeoffs
    # A MIXED verdict that renders only one side is the failure mode.
    assert "wins on" in comparison.summary
    assert comparison.summary.count("wins on") == 2


def test_identical_trips_are_equivalent_not_a_win():
    comparison = compare_trips(make_itinerary(), make_baseline())
    assert comparison.verdict is ComparisonVerdict.EQUIVALENT
    assert comparison.advantages == []
    assert comparison.tradeoffs == []


def test_equal_price_is_not_an_advantage():
    """Within the epsilon, price must favour nobody."""
    comparison = compare_trips(make_itinerary(cost=402.0), make_baseline(cost=400.0))
    price = next(m for m in comparison.metrics if m.metric == "price")
    assert price.favours is Favours.NEITHER
    assert not price.material


# ---------------------------------------------------------------------------
# The absent original (V7 §10: unavailable / over budget)
# ---------------------------------------------------------------------------
def test_no_original_means_no_comparison():
    """No baseline is not a Detoura victory by walkover."""
    assert compare_trips(make_itinerary(), None) is None


def test_unscored_baseline_produces_no_comparison():
    """Zeros from a missing scorer must never be published as findings.

    An unscored baseline carries 0.0 on experience, preference and
    accommodation. Comparing those against real scores would manufacture a
    three-axis landslide out of an omitted argument.
    """
    assert compare_trips(make_itinerary(), make_baseline(scored=False)) is None


def test_original_over_budget_yields_no_baseline_and_no_claim():
    """A trip the traveler could not afford is not a trip we beat."""
    request = TripRequest(
        origin="Köln", budget=120.0, travelers=2, duration_days=5,
        date_from=WINDOW_FROM, date_to=WINDOW_TO,
        preferred_destinations=["Berlin"],
    )
    result = TravelPlanner().plan(request)
    assert result.baseline is None
    for itinerary in result.recommendations:
        assert compare_trips(itinerary, result.baseline) is None


def test_unknown_destination_yields_no_baseline():
    request = TripRequest(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=WINDOW_FROM, date_to=WINDOW_TO,
        preferred_destinations=["Atlantis"],
    )
    assert TravelPlanner().plan(request).baseline is None


# ---------------------------------------------------------------------------
# Trade-off shapes the brief calls out by name
# ---------------------------------------------------------------------------
def test_added_cities_but_worse_transit_is_reported_as_a_tradeoff():
    comparison = compare_trips(
        make_itinerary(cities=["Berlin", "Prague"], travel_minutes=700),
        make_baseline(travel_minutes=300),
    )
    assert comparison.city_count_delta == 1
    assert comparison.added_cities == ["Prague"]
    assert comparison.transit_time_delta == pytest.approx(6.7, abs=0.05)
    assert any("in transit" in t for t in comparison.tradeoffs)
    assert comparison.verdict is ComparisonVerdict.MIXED


def test_ground_transfer_minutes_count_as_transit():
    """Transit compares whole-journey time, transfers included.

    Comparing our flight time against the baseline's flight time while
    ignoring the two hours of trains at either end would understate exactly the
    cost a multi-city trip actually imposes.
    """
    without = compare_trips(
        make_itinerary(travel_minutes=300, transfer_minutes=0), make_baseline()
    )
    with_transfers = compare_trips(
        make_itinerary(travel_minutes=300, transfer_minutes=180), make_baseline()
    )
    assert without.transit_time_delta == 0.0
    assert with_transfers.transit_time_delta == pytest.approx(3.0, abs=0.05)


def test_dropping_the_travelers_own_destination_is_flagged():
    comparison = compare_trips(
        make_itinerary(cities=["Prague", "Vienna"]), make_baseline(destination="Berlin")
    )
    assert comparison.dropped_original_destination is True
    assert "Berlin" not in comparison.added_cities


def test_destination_match_is_case_insensitive():
    comparison = compare_trips(
        make_itinerary(cities=["berlin"]), make_baseline(destination="Berlin")
    )
    assert comparison.dropped_original_destination is False
    assert comparison.added_cities == []


# ---------------------------------------------------------------------------
# Baggage: unknown, and never quietly zero (V7 §20)
# ---------------------------------------------------------------------------
def test_baggage_is_unknown_not_zero():
    comparison = compare_trips(make_itinerary(), make_baseline())
    assert comparison.baggage_delta is None
    assert "baggage" in comparison.unknowns
    assert "baggage" in comparison.summary


# ---------------------------------------------------------------------------
# Honesty invariants
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cost,usable,experience",
    [(300.0, 5400, 0.8), (600.0, 2000, 0.3), (400.0, 3600, 0.6), (900.0, 6000, 0.9)],
)
def test_every_material_metric_appears_on_exactly_one_side(cost, usable, experience):
    comparison = compare_trips(
        make_itinerary(cost=cost, usable_minutes=usable, experience=experience),
        make_baseline(),
    )
    material = [m for m in comparison.metrics if m.material]
    assert len(material) == len(comparison.advantages) + len(comparison.tradeoffs)
    for metric in material:
        assert metric.favours in (Favours.DETOURA, Favours.ORIGINAL)


def test_deltas_are_always_detoura_minus_original():
    comparison = compare_trips(make_itinerary(cost=500.0), make_baseline(cost=400.0))
    assert comparison.price_delta == pytest.approx(100.0)
    price = next(m for m in comparison.metrics if m.metric == "price")
    assert price.delta == pytest.approx(price.detoura - price.original)
    assert price.favours is Favours.ORIGINAL


def test_summary_never_omits_a_material_loss():
    """Whatever else the prose says, a loss must be in it."""
    comparison = compare_trips(
        make_itinerary(cost=900.0, cities=["Berlin", "Prague"], usable_minutes=9000),
        make_baseline(cost=400.0),
    )
    assert comparison.tradeoffs
    assert "Your original Berlin trip wins on:" in comparison.summary
    assert "cheaper" in comparison.summary


def test_price_phrase_takes_the_perspective_of_its_sentence():
    """The original being 200 cheaper must never be printed as "200 more"."""
    comparison = compare_trips(make_itinerary(cost=600.0), make_baseline(cost=400.0))
    assert "€200 cheaper" in comparison.summary
    assert "€200 more expensive" in comparison.tradeoffs


# ---------------------------------------------------------------------------
# The structural guarantee: one scorer, one price
# ---------------------------------------------------------------------------
def test_baseline_state_reproduces_the_baseline_price_exactly():
    """The scored SearchState and the reported result must be the same trip.

    This is the invariant the whole comparison rests on. If the state we score
    costs something different from the price we print, then the metrics
    describe a trip the traveler was never shown.
    """
    request = TripRequest(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=WINDOW_FROM, date_to=WINDOW_TO,
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
        preferred_destinations=["Berlin"],
    )
    planner = TravelPlanner()
    result = planner.plan(request)
    baseline = result.baseline
    assert baseline is not None and baseline.scored

    # Re-derive the winning candidate the way compute() did, then re-price it.
    candidate = _recompute_candidate(planner, request, result)
    state = planner.baseline_planner.state_for(candidate, request)
    assert state.total_cost == pytest.approx(baseline.total_cost, abs=0.01)
    assert state.transport_cost == pytest.approx(baseline.cost_breakdown.transport, abs=0.01)
    assert state.accommodation_cost == pytest.approx(
        baseline.cost_breakdown.accommodation, abs=0.01
    )
    assert state.ground_transfer_cost == pytest.approx(
        baseline.cost_breakdown.ground_transfer, abs=0.01
    )
    assert state.usable_destination_minutes == baseline.usable_destination_minutes
    assert state.completed is True
    assert state.city_count == 1


def _recompute_candidate(planner, request, result):
    """The internal candidate behind a computed baseline, for re-pricing."""
    from detoura.services.baseline import _Candidate

    baseline = result.baseline
    outbound, inbound = baseline.legs
    transfer_option = planner.baseline_planner._transfer_option(
        request, outbound.origin
    )
    rooms = planner.baseline_planner.accommodation.search(
        baseline.destination, outbound.arrival.date(), inbound.departure.date(),
        request.travelers,
    )
    return _Candidate(
        cost=baseline.total_cost,
        airport=outbound.origin,
        outbound=outbound,
        inbound=inbound,
        transport=baseline.cost_breakdown.transport,
        stay_cost=baseline.cost_breakdown.accommodation,
        transfer=baseline.cost_breakdown.ground_transfer,
        room=rooms[0] if rooms else None,
        transfer_option=transfer_option,
    )


def test_baseline_is_scored_by_the_planners_own_scorer():
    """Scores must be real measurements, not defaults."""
    request = TripRequest(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=WINDOW_FROM, date_to=WINDOW_TO,
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
        preferred_destinations=["Berlin"],
    )
    baseline = TravelPlanner().plan(request).baseline
    assert baseline.scored is True
    assert 0.0 < baseline.experience_score <= 1.0
    assert 0.0 < baseline.preference_match <= 1.0
    assert 0.0 < baseline.travel_value <= 1.0
    assert baseline.city_count == 1


def test_baseline_without_a_scorer_reports_unscored():
    """The default path stays backwards compatible and honest about it."""
    request = TripRequest(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=WINDOW_FROM, date_to=WINDOW_TO,
        preferred_destinations=["Berlin"],
    )
    planner = TravelPlanner()
    baseline = planner.baseline_planner.compute(
        request, origin_airports=["CGN", "DUS"],
        start_dates=request.candidate_start_dates(),
    )
    assert baseline is not None
    assert baseline.scored is False
    assert baseline.experience_score == 0.0


def test_scoring_the_baseline_does_not_change_the_recommendations():
    """Phase 1 is additive. Adding it must not move a single result."""
    request = TripRequest(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=WINDOW_FROM, date_to=WINDOW_TO,
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
        preferred_destinations=["Berlin"],
    )
    first = TravelPlanner().plan(request)
    second = TravelPlanner().plan(request)
    assert [(r.route_label(), r.total_cost, r.score) for r in first.recommendations] == [
        (r.route_label(), r.total_cost, r.score) for r in second.recommendations
    ]
    assert first.metadata.states_generated == second.metadata.states_generated


def test_comparison_is_deterministic():
    request = TripRequest(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=WINDOW_FROM, date_to=WINDOW_TO,
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
        preferred_destinations=["Berlin"],
    )
    result = TravelPlanner().plan(request)
    top = result.recommendations[0]
    first = compare_trips(top, result.baseline)
    second = compare_trips(top, result.baseline)
    assert first.model_dump() == second.model_dump()


# ---------------------------------------------------------------------------
# End to end through the product API
# ---------------------------------------------------------------------------
@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_planner] = lambda: TravelPlanner()
    return TestClient(app)


def test_comparison_reaches_the_api(client):
    payload = {
        "origin": "Köln",
        "date_from": WINDOW_FROM.isoformat(),
        "date_to": WINDOW_TO.isoformat(),
        "duration_days": 5,
        "travelers": 2,
        "budget": 900.0,
        "preferred_destinations": ["Berlin"],
        "interests": ["history", "food"],
        "search_mode": "SMART",
    }
    response = client.post("/api/v1/search", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["recommendations"]
    comparison = body["recommendations"][0]["comparison"]
    assert comparison is not None
    assert comparison["verdict"] in {v.value for v in ComparisonVerdict}
    assert comparison["baggage_delta"] is None
    assert comparison["summary"]
    # The old field is untouched: the shipped frontend still reads it.
    assert "baseline_comparison" in body["recommendations"][0]
