"""Recommendation profiles and the Travel Value objective."""

from __future__ import annotations

from datetime import datetime

import pytest

from travel_planner.algorithms.travel_value import TravelValueScorer
from travel_planner.config import PlannerConfig
from travel_planner.models.trip import TravelPreferences
from travel_planner.profiles import (
    DEFAULT_PROFILE,
    PROFILES,
    ProfileName,
    RecommendationProfile,
    TravelValueWeights,
    get_profile,
)
from travel_planner.services.planner import TravelPlanner

from .conftest import leg, make_state, room, trip_request

CHEAPEST = PROFILES[ProfileName.CHEAPEST]
BEST_VALUE = PROFILES[ProfileName.BEST_VALUE]
ADVENTURE = PROFILES[ProfileName.ADVENTURE]


@pytest.fixture
def scorer(config, destinations) -> TravelValueScorer:
    return TravelValueScorer(config, destinations)


# ---------------------------------------------------------------------------
# Profile registry
# ---------------------------------------------------------------------------
def test_the_three_profiles_exist():
    assert set(PROFILES) == set(ProfileName)
    for name, profile in PROFILES.items():
        assert profile.name is name
        assert profile.description


def test_best_value_is_the_default():
    assert DEFAULT_PROFILE is ProfileName.BEST_VALUE
    assert get_profile(None).name is ProfileName.BEST_VALUE
    assert PlannerConfig().profile is ProfileName.BEST_VALUE
    assert trip_request().profile is None  # defers to the config


def test_profiles_can_be_looked_up_by_name_or_string():
    assert get_profile("ADVENTURE") is ADVENTURE
    assert get_profile(ProfileName.CHEAPEST) is CHEAPEST


def test_weights_are_normalized():
    for profile in PROFILES.values():
        normalized = profile.weights.normalized()
        assert sum(normalized.values()) == pytest.approx(1.0)
        assert set(normalized) == {
            "cost",
            "experience",
            "preferences",
            "time",
            "diversity",
        }


def test_profiles_emphasise_different_things():
    assert CHEAPEST.weights.cost > BEST_VALUE.weights.cost
    assert BEST_VALUE.weights.experience > CHEAPEST.weights.experience
    assert ADVENTURE.weights.diversity > BEST_VALUE.weights.diversity
    # CHEAPEST must be strictly monotone in price, so no reward for spending.
    assert CHEAPEST.budget_utilization_weight == 0.0
    assert BEST_VALUE.budget_utilization_weight > 0.0


def test_weights_must_not_all_be_zero():
    with pytest.raises(ValueError, match="positive"):
        TravelValueWeights(
            cost=0.0, experience=0.0, preferences=0.0, time=0.0, diversity=0.0
        )


# ---------------------------------------------------------------------------
# CostScore
# ---------------------------------------------------------------------------
def test_cheapest_cost_score_is_strictly_monotone(scorer):
    """Under CHEAPEST, spending a euro more is always worse."""
    request = trip_request(budget=400)
    scores = [scorer.cost_score(cost, request, CHEAPEST) for cost in (50, 150, 250, 350)]
    assert scores == sorted(scores, reverse=True)
    assert scorer.cost_score(0, request, CHEAPEST) == 1.0
    assert scorer.cost_score(400, request, CHEAPEST) == 0.0


def test_best_value_barely_penalizes_spending_up_to_the_target(scorer):
    """100 -> 150 should cost almost nothing; 150 -> 350 should cost real score."""
    request = trip_request(budget=400)
    cheap = scorer.cost_score(100, request, BEST_VALUE)
    moderate = scorer.cost_score(150, request, BEST_VALUE)
    expensive = scorer.cost_score(350, request, BEST_VALUE)
    assert abs(cheap - moderate) < 0.05
    assert moderate - expensive > 0.15


def test_over_budget_costs_score_zero_headroom(scorer):
    request = trip_request(budget=400)
    assert scorer.cost_score(1000, request, CHEAPEST) == 0.0


# ---------------------------------------------------------------------------
# Experience, diversity
# ---------------------------------------------------------------------------
def test_experience_rewards_stays_that_fit_the_city(scorer):
    request = trip_request(travelers=1)
    proper = make_state(
        [
            leg("DUS", "Rome", datetime(2026, 9, 10, 9, 0), 140, 58.0),
            leg("Rome", "DUS", datetime(2026, 9, 14, 18, 0), 140, 62.0),
        ]
    )
    flying_visit = make_state(
        [
            leg("DUS", "Rome", datetime(2026, 9, 10, 20, 0), 140, 58.0),
            leg("Rome", "DUS", datetime(2026, 9, 11, 7, 0), 140, 62.0),
        ]
    )
    assert scorer.experience_score(proper) > scorer.experience_score(flying_visit)
    assert scorer.time_score(proper, request) > scorer.time_score(flying_visit, request)


def test_pace_quality_penalizes_cramming(scorer):
    relaxed = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
            leg("Prague", "Vienna", datetime(2026, 9, 12, 8, 0), 240, 20.0),
            leg("Vienna", "DUS", datetime(2026, 9, 14, 8, 0), 95, 25.0),
        ]
    )
    frantic = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
            leg("Prague", "Vienna", datetime(2026, 9, 11, 8, 0), 240, 20.0),
            leg("Vienna", "Budapest", datetime(2026, 9, 12, 8, 0), 160, 22.0),
            leg("Budapest", "DUS", datetime(2026, 9, 13, 8, 0), 100, 40.0),
        ]
    )
    assert scorer.pace_quality(frantic) < scorer.pace_quality(relaxed)


def test_diversity_rewards_more_places_but_with_diminishing_returns(scorer):
    one = scorer.base.multi_city_component(1)
    two = scorer.base.multi_city_component(2)
    three = scorer.base.multi_city_component(3)
    four = scorer.base.multi_city_component(4)
    assert one == 0.0
    assert two - one > three - two > four - three


# ---------------------------------------------------------------------------
# Critical scenario: Travel Value (spec section 39)
# ---------------------------------------------------------------------------
def _trip_a():
    """One city, cheap, good destination time."""
    return make_state(
        [
            leg("DUS", "Berlin", datetime(2026, 9, 10, 9, 0), 70, 20.0),
            leg("Berlin", "DUS", datetime(2026, 9, 14, 19, 0), 70, 25.0),
        ],
        travelers=1,
        rooms={
            "Berlin": room(
                "Berlin", __import__("datetime").date(2026, 9, 10),
                __import__("datetime").date(2026, 9, 14), 11.25,
            )
        },
    )


def _trip_b():
    """Two cities, dearer, still good destination time and a strong match."""
    return make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 75, 25.0),
            leg("Prague", "Vienna", datetime(2026, 9, 12, 10, 0), 240, 12.0),
            leg("Vienna", "DUS", datetime(2026, 9, 14, 19, 0), 95, 25.0),
        ],
        travelers=1,
        rooms={
            "Prague": room(
                "Prague", __import__("datetime").date(2026, 9, 10),
                __import__("datetime").date(2026, 9, 12), 14.0,
            ),
            "Vienna": room(
                "Vienna", __import__("datetime").date(2026, 9, 12),
                __import__("datetime").date(2026, 9, 14), 20.0,
            ),
        },
    )


def test_best_value_may_prefer_the_dearer_two_city_trip(scorer):
    """A 90 one-city trip vs a 130 two-city trip, multiple_cities = 0.9.

    This is why profiles exist: CHEAPEST must take A, BEST_VALUE is free to
    take B, and neither answer is wrong.
    """
    request = trip_request(
        travelers=1,
        budget=400,
        preferred_destinations=[],
        preferences=TravelPreferences(
            history=0.9, culture=0.9, nature=0.4, nightlife=0.3, food=0.6,
            multiple_cities=0.9,
        ),
    )
    a, b = _trip_a(), _trip_b()
    assert a.total_cost == 90.0
    assert b.total_cost == 130.0

    assert scorer.total(a, request, CHEAPEST) > scorer.total(b, request, CHEAPEST)
    assert scorer.total(b, request, BEST_VALUE) > scorer.total(a, request, BEST_VALUE)
    assert scorer.total(b, request, ADVENTURE) > scorer.total(a, request, ADVENTURE)


# ---------------------------------------------------------------------------
# Critical scenario: Adventure (spec section 40)
# ---------------------------------------------------------------------------
def _three_cities(transit_minutes: tuple[int, int, int, int] = (75, 240, 160, 100)):
    """Prague, Vienna and Budapest inside the same five days."""
    out, hop_a, hop_b, home = transit_minutes
    return make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), out, 25.0),
            leg("Prague", "Vienna", datetime(2026, 9, 12, 10, 0), hop_a, 12.0),
            leg("Vienna", "Budapest", datetime(2026, 9, 13, 10, 0), hop_b, 15.0),
            leg("Budapest", "DUS", datetime(2026, 9, 14, 19, 0), home, 30.0),
        ],
        travelers=1,
    )


def test_adventure_values_the_extra_city_more_than_the_other_profiles(scorer):
    """Three cities in five days means short stays, which every profile dislikes.

    What separates ADVENTURE is *how much* it minds: the third city costs it
    far less score than it costs CHEAPEST or BEST_VALUE. Asserting the gap
    rather than the winner is the honest form of the requirement - squeezing
    three cities into five days really is a worse stay, and the model should
    keep saying so.
    """
    request = trip_request(travelers=1, budget=400, preferred_destinations=[])

    # Same money, same days, same window - only the number of cities differs,
    # so the deltas below isolate exactly that.
    two = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 75, 25.0),
            leg("Prague", "Vienna", datetime(2026, 9, 12, 10, 0), 240, 12.0),
            leg("Vienna", "DUS", datetime(2026, 9, 14, 19, 0), 95, 25.0),
        ],
        travelers=1,
    )
    three = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 75, 25.0),
            leg("Prague", "Vienna", datetime(2026, 9, 12, 10, 0), 240, 12.0),
            leg("Vienna", "Budapest", datetime(2026, 9, 13, 10, 0), 160, 15.0),
            leg("Budapest", "DUS", datetime(2026, 9, 14, 19, 0), 100, 10.0),
        ],
        travelers=1,
    )
    assert two.total_cost == three.total_cost == 62.0

    def delta(profile: RecommendationProfile) -> float:
        return scorer.total(three, request, profile) - scorer.total(two, request, profile)

    assert delta(ADVENTURE) > delta(BEST_VALUE)

    # CHEAPEST is not comparable this way: nearly all of its weight sits on
    # cost, which is identical here, so it barely reacts at all. What is
    # comparable is how much *diversity* each profile pays for the extra city.
    diversity_gain = scorer.diversity_score(three) - scorer.diversity_score(two)
    assert diversity_gain > 0
    contributions = {
        profile.name: profile.weights.normalized()["diversity"] * diversity_gain
        for profile in (CHEAPEST, BEST_VALUE, ADVENTURE)
    }
    assert contributions[ProfileName.ADVENTURE] > contributions[ProfileName.BEST_VALUE]
    assert contributions[ProfileName.ADVENTURE] > contributions[ProfileName.CHEAPEST]


def test_adventure_still_refuses_an_absurd_amount_of_transit(scorer):
    """Same three cities, 45 hours in transit: not an adventure, just a slog."""
    request = trip_request(travelers=1, budget=400, preferred_destinations=[])
    sane = _three_cities()
    punishing = _three_cities((600, 700, 800, 700))

    assert scorer.total(punishing, request, ADVENTURE) < scorer.total(
        sane, request, ADVENTURE
    )
    assert scorer.time_score(punishing, request) < scorer.time_score(sane, request)


# ---------------------------------------------------------------------------
# End to end through the planner
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def by_profile():
    """The same request planned under all three profiles."""
    planner = TravelPlanner()
    request = trip_request()
    return {
        name: planner.plan(request, profile=name) for name in ProfileName
    }


def test_each_profile_returns_results(by_profile):
    for name, result in by_profile.items():
        assert result.recommendations, name
        assert result.profile is name
        assert result.metadata.profile is name


def test_cheapest_really_is_the_cheapest(by_profile):
    """No other profile's top pick undercuts CHEAPEST's."""
    cheapest = by_profile[ProfileName.CHEAPEST].recommendations[0]
    for name, result in by_profile.items():
        assert cheapest.total_cost <= result.recommendations[0].total_cost, name


def test_adventure_sees_more_places(by_profile):
    adventure = by_profile[ProfileName.ADVENTURE].recommendations
    cheapest = by_profile[ProfileName.CHEAPEST].recommendations
    assert sum(len(i.cities) for i in adventure) > sum(len(i.cities) for i in cheapest)
    assert len(adventure[0].cities) >= 2


def test_profiles_produce_different_rankings(by_profile):
    rankings = {
        name: [i.route_label() for i in result.recommendations]
        for name, result in by_profile.items()
    }
    assert rankings[ProfileName.CHEAPEST] != rankings[ProfileName.BEST_VALUE]
    assert rankings[ProfileName.ADVENTURE] != rankings[ProfileName.BEST_VALUE]


def test_every_profile_respects_the_hard_constraints(by_profile):
    request = trip_request()
    for name, result in by_profile.items():
        for itinerary in result.recommendations:
            assert itinerary.total_cost <= request.budget, name
            assert itinerary.duration_days <= request.duration_days, name
            assert "Paris" not in itinerary.cities, name
            assert itinerary.route_nodes[-1] in result.metadata.origin_airports, name


def test_the_request_can_carry_the_profile():
    planner = TravelPlanner()
    from_request = planner.plan(trip_request(profile=ProfileName.CHEAPEST))
    from_argument = planner.plan(trip_request(), profile=ProfileName.CHEAPEST)
    assert from_request.profile is ProfileName.CHEAPEST
    assert [i.route_label() for i in from_request.recommendations] == [
        i.route_label() for i in from_argument.recommendations
    ]


def test_the_argument_beats_the_request():
    planner = TravelPlanner()
    result = planner.plan(
        trip_request(profile=ProfileName.CHEAPEST), profile=ProfileName.ADVENTURE
    )
    assert result.profile is ProfileName.ADVENTURE


def test_each_profile_is_deterministic():
    planner = TravelPlanner()
    request = trip_request()
    for name in ProfileName:
        first = planner.plan(request, profile=name)
        second = planner.plan(request, profile=name)
        assert first.recommendations == second.recommendations


def test_value_breakdown_names_its_profile(by_profile):
    for name, result in by_profile.items():
        for itinerary in result.recommendations:
            assert itinerary.value_breakdown.profile is name
            assert itinerary.profile is name
            assert itinerary.value_breakdown.total == pytest.approx(itinerary.score)


def test_a_custom_profile_can_be_supplied(destinations, config):
    """Profiles are data, so a caller can define their own."""
    scorer = TravelValueScorer(config, destinations)
    picky = RecommendationProfile(
        name=ProfileName.BEST_VALUE,
        weights=TravelValueWeights(
            cost=0.0, experience=0.0, preferences=1.0, time=0.0, diversity=0.0
        ),
    )
    request = trip_request(travelers=1, budget=400, preferred_destinations=[])
    components = scorer.components(_trip_b(), request, picky)
    assert scorer.weighted_total(components, picky) == pytest.approx(
        components["preferences"]
    )
