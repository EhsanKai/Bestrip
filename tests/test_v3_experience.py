"""The destination experience model and preference matching."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from travel_planner.algorithms.experience import (
    DISLIKE_PENALTY,
    REVISIT_FACTOR,
    ExperienceEngine,
)
from travel_planner.algorithms.travel_value import TravelValueScorer
from travel_planner.data.destinations import DESTINATIONS
from travel_planner.models.destination import (
    ATTRIBUTES,
    EXPERIENCE_ATTRIBUTES,
    V3_ATTRIBUTES,
    Destination,
)
from travel_planner.models.trip import TravelPreferences
from travel_planner.profiles import PROFILES, ProfileName

from .conftest import leg, make_state, trip_request


@pytest.fixture
def engine(config, destinations) -> ExperienceEngine:
    return ExperienceEngine(config, destinations)


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------
def test_every_city_has_the_full_profile():
    for destination in DESTINATIONS:
        vector = destination.experience_vector()
        assert set(vector) == set(EXPERIENCE_ATTRIBUTES)
        assert all(0.0 <= value <= 1.0 for value in vector.values())


def test_v3_attributes_extend_rather_than_replace_the_v1_ones():
    assert set(ATTRIBUTES) < set(EXPERIENCE_ATTRIBUTES)
    assert set(V3_ATTRIBUTES).isdisjoint(ATTRIBUTES)
    assert len(EXPERIENCE_ATTRIBUTES) == 12


def test_a_v1_catalog_entry_still_constructs():
    """Backward compatibility: the V3 attributes all have defaults."""
    destination = Destination(
        id="Testville", name="Testville", country="Nowhere",
        history=0.5, nature=0.5, nightlife=0.5, culture=0.5, food=0.5,
    )
    assert destination.architecture == 0.5
    assert destination.beaches == 0.0
    assert 0.0 <= destination.richness <= 1.0


def test_cities_are_genuinely_different(destinations):
    """An experience model over identical cities cannot demonstrate anything."""
    berlin = destinations.get("Berlin")
    zurich = destinations.get("Zurich")
    assert berlin.nightlife > zurich.nightlife
    assert zurich.nature > berlin.nature
    assert destinations.get("Barcelona").beaches > destinations.get("Prague").beaches
    assert destinations.get("Rome").history > destinations.get("Zurich").history


def test_richness_is_derived_but_overridable():
    base = DESTINATIONS[0]
    assert base.experience_richness is None
    assert 0.0 < base.richness < 1.0
    pinned = base.model_copy(update={"experience_richness": 0.42})
    assert pinned.richness == 0.42


def test_beaches_do_not_drag_down_a_landlocked_city(destinations):
    """Prague should not read as poor for lacking a coastline."""
    prague = destinations.get("Prague")
    with_beaches = sum(prague.experience_vector().values()) / 12
    assert prague.richness > with_beaches


# ---------------------------------------------------------------------------
# Preference resolution
# ---------------------------------------------------------------------------
def test_preferred_experiences_raise_their_weight(engine):
    request = trip_request(preferred_experiences=["museums", "architecture"])
    weights, _ = engine.resolve_weights(request)
    assert weights["museums"] == 1.0
    assert weights["architecture"] == 1.0


def test_disliked_experiences_leave_the_positive_weights(engine):
    request = trip_request(disliked_experiences=["nightlife"])
    weights, disliked = engine.resolve_weights(request)
    assert "nightlife" not in weights
    assert disliked == {"nightlife"}


def test_unspecified_v3_attributes_are_simply_absent(engine):
    """A V1-era request is matched on what it actually said."""
    weights, _ = engine.resolve_weights(trip_request())
    assert set(weights) == set(ATTRIBUTES)


def test_preference_match_follows_the_weights(engine, destinations):
    request = trip_request(preferred_experiences=["nightlife"])
    weights, disliked = engine.resolve_weights(request)
    berlin = engine.preference_match(destinations.get("Berlin"), weights, disliked)
    zurich = engine.preference_match(destinations.get("Zurich"), weights, disliked)
    assert berlin > zurich


def test_a_dislike_actively_penalizes_not_merely_ignores(engine, destinations):
    """A party city must rank *below* a quiet one for someone avoiding nightlife."""
    berlin = destinations.get("Berlin")
    neutral = trip_request()
    avoiding = trip_request(disliked_experiences=["nightlife"])

    weights_n, disliked_n = engine.resolve_weights(neutral)
    weights_a, disliked_a = engine.resolve_weights(avoiding)
    assert engine.preference_match(berlin, weights_a, disliked_a) < (
        engine.preference_match(berlin, weights_n, disliked_n)
    )
    # The penalty is proportional to how strong the disliked attribute is.
    zurich = destinations.get("Zurich")
    assert engine.preference_match(zurich, weights_a, disliked_a) > (
        engine.preference_match(berlin, weights_a, disliked_a)
    )
    assert DISLIKE_PENALTY > 0


def test_unknown_experiences_are_rejected_at_the_request():
    with pytest.raises(ValidationError, match="unknown experience"):
        trip_request(preferred_experiences=["skiing"])


def test_an_experience_cannot_be_both_wanted_and_avoided():
    with pytest.raises(ValidationError, match="both preferred and disliked"):
        trip_request(preferred_experiences=["food"], disliked_experiences=["food"])


# ---------------------------------------------------------------------------
# Stay quality
# ---------------------------------------------------------------------------
def test_stay_quality_scales_below_the_recommended_minimum(engine, destinations):
    london = destinations.get("London")  # recommends 2-5 days
    assert engine.stay_quality(london, 2.0) == 1.0
    assert engine.stay_quality(london, 5.0) == 1.0
    assert engine.stay_quality(london, 1.0) == pytest.approx(0.5)
    assert engine.stay_quality(london, 0.0) == 0.0


def test_overstaying_decays_gently(engine, destinations):
    london = destinations.get("London")
    assert 0.0 < engine.stay_quality(london, 7.0) < 1.0
    assert engine.stay_quality(london, 7.0) > engine.stay_quality(london, 9.0)


# ---------------------------------------------------------------------------
# The combined score
# ---------------------------------------------------------------------------
def test_the_three_factors_multiply(engine):
    """A great city you have no time in is not a great experience."""
    request = trip_request()
    proper = engine.assess_city("Rome", 3.0, request)
    rushed = engine.assess_city("Rome", 0.5, request)
    assert proper.quality == rushed.quality        # same city
    assert proper.stay_quality > rushed.stay_quality
    assert proper.score > rushed.score * 1.5


def test_an_insight_explains_itself(engine):
    request = trip_request(
        preferred_experiences=["food", "museums"], disliked_experiences=["beaches"]
    )
    insight = engine.assess_city("Paris", 3.0, request)
    assert "food" in insight.strengths
    assert "museums" in insight.strengths
    assert "recommended" in insight.stay_note
    assert insight.dislikes_present == ()

    barcelona = engine.assess_city("Barcelona", 3.0, request)
    assert "beaches" in barcelona.dislikes_present


def test_weaknesses_are_reported(engine):
    request = trip_request(preferred_experiences=["nature", "beaches"])
    insight = engine.assess_city("Brussels", 2.0, request)
    assert "nature" in insight.weaknesses


def test_a_previously_visited_city_is_worth_less(engine):
    fresh = engine.assess_city("Prague", 3.0, trip_request())
    again = engine.assess_city("Prague", 3.0, trip_request(previously_visited=["Prague"]))
    assert again.previously_visited
    assert again.score == pytest.approx(fresh.score * REVISIT_FACTOR, abs=1e-6)


def test_previously_visited_matching_tolerates_spellings(engine):
    again = engine.assess_city("Vienna", 3.0, trip_request(previously_visited=["Wien"]))
    assert again.previously_visited


def test_the_fast_path_agrees_with_the_full_assessment(engine):
    """Beam ranking and the final report must not disagree."""
    request = trip_request(preferred_experiences=["culture"])
    context = engine.context(request)
    for city in ("Rome", "Prague", "Zurich", "Barcelona"):
        for days in (0.5, 2.0, 3.5):
            assert engine.city_score(city, days, context) == pytest.approx(
                engine.assess_city(city, days, request).score, abs=1e-6
            )


def test_unknown_cities_stay_neutral(engine):
    insight = engine.assess_city("Atlantis", 2.0, trip_request())
    assert insight.score == 0.5
    assert insight.stay_note == "unknown destination"


# ---------------------------------------------------------------------------
# Ideal city count
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("days", "expected_max"),
    [(2, 1), (5, 2), (10, 4)],
)
def test_ideal_city_count_grows_with_trip_length(engine, days, expected_max):
    """Derived from the catalog's recommended stays, not a hard-coded table.

    Two days is one city; five days is at most two; ten days opens up to four.
    The numbers come from the average recommended stay plus the day each move
    costs, so enriching the catalog moves them automatically.
    """
    from datetime import date, timedelta

    start = date(2026, 9, 1)
    request = trip_request(
        duration_days=days,
        date_from=start,
        date_to=start + timedelta(days=days + 2),
        preferences=TravelPreferences(multiple_cities=0.5),
    )
    assert engine.ideal_city_count(request, float(days)) <= expected_max


def test_an_explicit_city_count_wins(engine):
    request = trip_request(preferred_city_count=3)
    assert engine.ideal_city_count(request, 5.0) == 3


def test_appetite_shifts_the_ideal_count(engine):
    keen = trip_request(preferences=TravelPreferences(multiple_cities=1.0))
    homebody = trip_request(preferences=TravelPreferences(multiple_cities=0.0))
    assert engine.ideal_city_count(keen, 5.0) > engine.ideal_city_count(homebody, 5.0)


def test_the_profile_biases_the_ideal_count(config, destinations):
    scorer = TravelValueScorer(config, destinations)
    request = trip_request(preferences=TravelPreferences(multiple_cities=0.5))
    counts = {
        name: scorer.ideal_city_count(request, PROFILES[name])
        for name in ProfileName
    }
    assert counts[ProfileName.ADVENTURE] > counts[ProfileName.CHEAPEST]


def test_city_count_fit_penalizes_both_directions(config, destinations):
    scorer = TravelValueScorer(config, destinations)
    request = trip_request(preferred_city_count=2)
    profile = PROFILES[ProfileName.BEST_VALUE]

    one = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 75, 40.0),
            leg("Prague", "DUS", datetime(2026, 9, 14, 18, 0), 75, 40.0),
        ]
    )
    two = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 75, 40.0),
            leg("Prague", "Vienna", datetime(2026, 9, 12, 10, 0), 240, 20.0),
            leg("Vienna", "DUS", datetime(2026, 9, 14, 18, 0), 95, 30.0),
        ]
    )
    four = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 75, 40.0),
            leg("Prague", "Vienna", datetime(2026, 9, 11, 10, 0), 240, 20.0),
            leg("Vienna", "Budapest", datetime(2026, 9, 12, 10, 0), 160, 20.0),
            leg("Budapest", "Berlin", datetime(2026, 9, 13, 10, 0), 100, 30.0),
            leg("Berlin", "DUS", datetime(2026, 9, 14, 18, 0), 70, 30.0),
        ]
    )
    assert scorer.city_count_score(two, request, profile) == 1.0
    assert scorer.city_count_score(one, request, profile) < 1.0
    assert scorer.city_count_score(four, request, profile) < (
        scorer.city_count_score(one, request, profile)
    )


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------
def test_preferences_change_which_cities_are_recommended(planner):
    """The whole point: stated taste must move the answer."""
    nightlife = planner.plan(
        trip_request(
            budget=700, preferred_destinations=[],
            preferred_experiences=["nightlife"],
            preferences=TravelPreferences(multiple_cities=0.3),
        )
    )
    nature = planner.plan(
        trip_request(
            budget=700, preferred_destinations=[],
            preferred_experiences=["nature"],
            disliked_experiences=["nightlife"],
            preferences=TravelPreferences(multiple_cities=0.3),
        )
    )
    assert nightlife.recommendations and nature.recommendations
    party_cities = {c for i in nightlife.recommendations for c in i.cities}
    quiet_cities = {c for i in nature.recommendations for c in i.cities}
    assert party_cities != quiet_cities


def test_insights_reach_the_itinerary(planner):
    result = planner.plan(
        trip_request(budget=650, preferred_experiences=["culture", "history"])
    )
    for itinerary in result.recommendations:
        assert len(itinerary.destination_insights) == len(itinerary.cities)
        for insight in itinerary.destination_insights:
            assert insight.city in itinerary.cities
            assert 0.0 <= insight.score <= 1.0
            assert insight.stay_note


def test_a_disliked_experience_is_flagged(planner):
    from travel_planner.models.itinerary import ExplanationFactor

    result = planner.plan(
        trip_request(
            budget=900,
            preferred_destinations=["Barcelona"],
            disliked_experiences=["beaches"],
        )
    )
    flagged = [
        i
        for i in result.recommendations
        if ExplanationFactor.CONTAINS_DISLIKED_EXPERIENCE in i.explanation_factors
    ]
    beach_trips = [i for i in result.recommendations if "Barcelona" in i.cities]
    assert len(flagged) == len(beach_trips)


def test_experience_scoring_is_deterministic(config, destinations):
    request = trip_request(preferred_experiences=["food"])
    first = ExperienceEngine(config, destinations).assess(
        [("Rome", 2.0), ("Paris", 1.5)], request
    )
    second = ExperienceEngine(config, destinations).assess(
        [("Rome", 2.0), ("Paris", 1.5)], request
    )
    assert first == second
