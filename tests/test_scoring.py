"""Scoring engine: each component, then the weighted total."""

from __future__ import annotations

from datetime import datetime

import pytest

from travel_planner.algorithms.scoring import ScoringEngine
from travel_planner.config import PlannerConfig, ScoreWeights
from travel_planner.models.transport import TransportType
from travel_planner.models.trip import TravelPreferences

from .conftest import leg, make_state, trip_request


@pytest.fixture
def engine(config, destinations) -> ScoringEngine:
    return ScoringEngine(config, destinations)


def round_trip(city: str, price_out: float, price_back: float, travelers: int = 1):
    legs = [
        leg("DUS", city, datetime(2026, 9, 10, 8, 0), 90, price_out),
        leg(city, "DUS", datetime(2026, 9, 14, 8, 0), 90, price_back),
    ]
    return make_state(legs, travelers=travelers)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("cost", "budget", "expected"),
    [(0, 250, 1.0), (125, 250, 0.5), (250, 250, 0.0), (400, 250, 0.0)],
)
def test_budget_score_formula(engine, cost, budget, expected):
    assert engine.budget_score(cost, budget) == pytest.approx(expected)


def test_budget_score_is_clamped_for_over_budget_routes(engine):
    assert engine.budget_score(1_000, 250) == 0.0


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------
def test_attribute_match_favours_the_better_fitting_city(engine, destinations):
    """A history/culture traveler should prefer Rome over Zurich."""
    request = trip_request(
        preferences=TravelPreferences(
            history=1.0, culture=1.0, nature=0.0, nightlife=0.0, food=0.0
        )
    )
    rome = engine.attribute_match(destinations.get("Rome"), request)
    zurich = engine.attribute_match(destinations.get("Zurich"), request)
    assert rome > zurich
    # The formula is a plain weighted average of the requested attributes.
    assert rome == pytest.approx((1.00 + 0.95) / 2)


def test_attribute_match_is_neutral_when_nothing_is_wanted(engine, destinations):
    request = trip_request(
        preferences=TravelPreferences(
            history=0.0, nature=0.0, nightlife=0.0, culture=0.0, food=0.0
        )
    )
    assert engine.attribute_match(destinations.get("Rome"), request) == 0.5


@pytest.mark.parametrize(
    ("cities", "expected"), [(1, 0.0), (2, 0.5), (3, 2 / 3), (4, 0.75)]
)
def test_multi_city_component_has_diminishing_returns(engine, cities, expected):
    assert engine.multi_city_component(cities) == pytest.approx(expected)


def test_multiple_cities_preference_controls_the_multi_city_reward(engine):
    """multiple_cities = 0 gives no benefit; = 1 gives a clear one."""
    one_city = round_trip("Prague", 55, 60)
    two_cities = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
            leg("Prague", "Vienna", datetime(2026, 9, 12, 8, 0), 270, 12.0),
            leg("Vienna", "DUS", datetime(2026, 9, 14, 8, 0), 95, 25.0),
        ]
    )

    indifferent = trip_request(preferences=TravelPreferences(multiple_cities=0.0))
    keen = trip_request(preferences=TravelPreferences(multiple_cities=1.0))

    gain_indifferent = engine.preference_score(
        two_cities, indifferent
    ) - engine.preference_score(one_city, indifferent)
    gain_keen = engine.preference_score(two_cities, keen) - engine.preference_score(
        one_city, keen
    )

    assert gain_keen > gain_indifferent
    assert gain_keen > 0.1


def test_multi_city_gain_is_zero_when_cities_are_equally_good(engine, destinations):
    """With multiple_cities = 0 the city count must not help at all."""
    request = trip_request(preferences=TravelPreferences(multiple_cities=0.0))
    one = round_trip("Prague", 55, 60)
    two = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
            leg("Prague", "Prague2", datetime(2026, 9, 12, 8, 0), 270, 12.0),
            leg("Prague2", "DUS", datetime(2026, 9, 14, 8, 0), 95, 25.0),
        ]
    )
    # "Prague2" is unknown to the catalog and scores the neutral 0.5, so any
    # difference here comes from the city count alone - which must be none.
    assert engine.preference_score(two, request) == pytest.approx(
        (engine.attribute_match(destinations.get("Prague"), request) + 0.5) / 2
    )


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------
def test_preferred_destinations_improve_the_score(engine):
    """Visiting a preferred city scores higher than visiting an equivalent one."""
    madrid = round_trip("Madrid", 48, 52)
    barcelona = round_trip("Barcelona", 48, 52)
    request = trip_request(preferred_destinations=["Madrid"])

    assert engine.destination_score(madrid, request) > engine.destination_score(
        barcelona, request
    )
    # The whole weighted score moves in the same direction.
    assert engine.score(madrid, request).total > engine.score(barcelona, request).total


def test_preference_bonus_disappears_without_preferred_destinations(engine):
    madrid = round_trip("Madrid", 48, 52)
    barcelona = round_trip("Barcelona", 48, 52)
    neutral = trip_request(preferred_destinations=[])
    assert engine.destination_score(madrid, neutral) == pytest.approx(
        engine.destination_score(barcelona, neutral)
    )


def test_must_visit_coverage_also_raises_the_destination_score(engine):
    london = round_trip("London", 40, 60)
    request_with = trip_request(preferred_destinations=[], must_visit=["London"])
    request_without = trip_request(preferred_destinations=[], must_visit=[])
    assert engine.destination_score(london, request_with) >= engine.destination_score(
        london, request_without
    )


@pytest.mark.parametrize(
    ("stay", "expected"),
    # London recommends 2-5 days; fit decays by 1/3 per day outside the range.
    [(2, 1.0), (5, 1.0), (1, 1 - 1 / 3), (7, 1 - 2 / 3), (8, 0.0)],
)
def test_stay_fit_against_recommended_range(engine, stay, expected):
    assert engine.stay_fit("London", stay) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Convenience & time
# ---------------------------------------------------------------------------
def test_absurd_itineraries_score_badly_on_convenience_and_time(engine):
    """London -> Rome -> Copenhagen -> Madrid in four days must score poorly.

    Even when it is cheap: the point of the travel-time penalty is that the
    optimizer cannot buy a nonsense route with a low price.
    """
    absurd = make_state(
        [
            leg("DUS", "London", datetime(2026, 9, 10, 6, 0), 90, 20.0),
            leg("London", "Rome", datetime(2026, 9, 11, 6, 0), 160, 20.0),
            leg("Rome", "Copenhagen", datetime(2026, 9, 12, 6, 0), 170, 20.0),
            leg("Copenhagen", "Madrid", datetime(2026, 9, 13, 6, 0), 210, 20.0),
            leg("Madrid", "DUS", datetime(2026, 9, 14, 6, 0), 170, 20.0),
        ]
    )
    sane = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
            leg("Prague", "Vienna", datetime(2026, 9, 12, 8, 0), 240, 20.0),
            leg("Vienna", "DUS", datetime(2026, 9, 14, 8, 0), 95, 25.0),
        ]
    )
    request = trip_request(travelers=1, budget=250)
    assert absurd.total_cost == sane.total_cost == 100.0

    assert engine.convenience_score(absurd) < engine.convenience_score(sane)
    assert engine.time_score(absurd, request) < engine.time_score(sane, request)
    # Same price, four times the cities - and it still loses.
    assert engine.score(absurd, request).total < engine.score(sane, request).total


def test_a_frantic_pace_is_penalized(engine):
    """Moving city almost every day scores worse than a two-stop trip."""
    frantic = make_state(
        [
            leg("DUS", "London", datetime(2026, 9, 10, 6, 0), 90, 20.0),
            leg("London", "Rome", datetime(2026, 9, 11, 6, 0), 160, 20.0),
            leg("Rome", "Copenhagen", datetime(2026, 9, 12, 6, 0), 170, 20.0),
            leg("Copenhagen", "Madrid", datetime(2026, 9, 13, 6, 0), 210, 20.0),
            leg("Madrid", "DUS", datetime(2026, 9, 14, 6, 0), 170, 20.0),
        ]
    )
    relaxed = make_state(
        [
            leg("DUS", "London", datetime(2026, 9, 10, 6, 0), 90, 20.0),
            leg("London", "Rome", datetime(2026, 9, 12, 6, 0), 160, 20.0),
            leg("Rome", "DUS", datetime(2026, 9, 14, 6, 0), 170, 20.0),
        ]
    )
    assert engine.convenience_score(frantic) < engine.convenience_score(relaxed)


def test_time_score_punishes_a_high_transit_fraction(engine):
    request = trip_request(travelers=1)
    quick_hops = round_trip("Prague", 55, 60)
    long_hops = make_state(
        [
            leg("DUS", "Madrid", datetime(2026, 9, 10, 8, 0), 600, 55.0),
            leg("Madrid", "DUS", datetime(2026, 9, 14, 8, 0), 600, 60.0),
        ]
    )
    assert engine.time_score(long_hops, request) < engine.time_score(
        quick_hops, request
    )


def test_optimistic_time_score_ignores_unused_days(engine):
    """Partial states are not charged for days they have not spent yet."""
    request = trip_request()
    short = make_state(
        [leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0)], completed=False
    )
    assert engine.time_score(short, request, optimistic=True) > engine.time_score(
        short, request
    )


def test_bus_legs_are_less_convenient_than_flights(engine):
    flight = round_trip("Prague", 55, 60)
    bus = make_state(
        [
            leg(
                "DUS", "Prague", datetime(2026, 9, 10, 8, 0), 90, 55.0, TransportType.BUS
            ),
            leg(
                "Prague", "DUS", datetime(2026, 9, 14, 8, 0), 90, 60.0, TransportType.BUS
            ),
        ]
    )
    assert engine.convenience_score(bus) < engine.convenience_score(flight)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def test_weights_are_configurable_and_normalized():
    weights = ScoreWeights(
        budget=0.40, preference=0.20, destination=0.15, convenience=0.10, time=0.10,
        diversity=0.05,
    )
    assert weights.total == pytest.approx(1.0)
    assert weights.normalized()["budget"] == pytest.approx(0.40)

    doubled = ScoreWeights(
        budget=0.80, preference=0.40, destination=0.30, convenience=0.20, time=0.20,
        diversity=0.10,
    )
    assert doubled.normalized() == pytest.approx(weights.normalized())


def test_score_breakdown_is_complete_and_in_range(engine):
    request = trip_request(travelers=1)
    breakdown = engine.score(round_trip("Prague", 55, 60), request)
    for name in ("budget", "preference", "destination", "convenience", "time", "diversity"):
        assert 0.0 <= getattr(breakdown, name) <= 1.0
    assert 0.0 <= breakdown.total <= 1.0
    assert breakdown.weights["budget"] == pytest.approx(0.40)
    assert breakdown.notes


def test_score_respects_reweighting(destinations):
    """Turning the budget weight off changes which itinerary wins."""
    cheap_boring = round_trip("Zurich", 20, 20)
    pricey_fitting = round_trip("Rome", 60, 64)
    request = trip_request(
        travelers=1,
        budget=250,
        preferred_destinations=[],
        preferences=TravelPreferences(
            history=1.0, culture=1.0, nature=0.0, nightlife=0.0, food=0.5,
            multiple_cities=0.0,
        ),
    )

    budget_driven = ScoringEngine(
        PlannerConfig(score_weights=ScoreWeights(budget=1.0, preference=0.0,
                                                 destination=0.0, convenience=0.0,
                                                 time=0.0, diversity=0.0)),
        destinations,
    )
    taste_driven = ScoringEngine(
        PlannerConfig(score_weights=ScoreWeights(budget=0.0, preference=1.0,
                                                 destination=0.0, convenience=0.0,
                                                 time=0.0, diversity=0.0)),
        destinations,
    )

    assert budget_driven.score(cheap_boring, request).total > budget_driven.score(
        pricey_fitting, request
    ).total
    assert taste_driven.score(pricey_fitting, request).total > taste_driven.score(
        cheap_boring, request
    ).total


def test_objectives_vector(engine):
    request = trip_request(travelers=2)
    state = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
            leg("Prague", "Vienna", datetime(2026, 9, 12, 8, 0), 270, 12.0),
            leg("Vienna", "DUS", datetime(2026, 9, 14, 8, 0), 95, 25.0),
        ],
        travelers=2,
    )
    objectives = engine.objectives(state, request)
    assert objectives.cost == 184.0
    assert objectives.travel_minutes == 440
    assert objectives.city_count == 2
    assert 0.0 <= objectives.preference_score <= 1.0
