"""Travel intensity: how hard an itinerary works the traveler."""

from __future__ import annotations

from datetime import datetime

import pytest

from travel_planner.algorithms.intensity import (
    MAX_TRANSIT_SHARE,
    STYLE_TOLERANCE,
    IntensityScorer,
)
from travel_planner.algorithms.travel_value import TravelValueScorer
from travel_planner.models.trip import TravelStyle
from travel_planner.profiles import PROFILES, ProfileName

from .conftest import leg, make_state, transfer, trip_request

BEST_VALUE = PROFILES[ProfileName.BEST_VALUE]
ADVENTURE = PROFILES[ProfileName.ADVENTURE]


@pytest.fixture
def scorer() -> IntensityScorer:
    return IntensityScorer()


def calm_two_city():
    """Five days, two cities, about four hours in transit.

    Priced to total exactly 200, matching :func:`frantic_four_city`, so any
    comparison between the two isolates *how* the trip travels rather than what
    it costs.
    """
    return make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 75, 70.0),
            leg("Prague", "Vienna", datetime(2026, 9, 12, 10, 0), 90, 60.0),
            leg("Vienna", "DUS", datetime(2026, 9, 15, 18, 0), 95, 70.0),
        ]
    )


def frantic_four_city():
    """Five days, four cities, about eighteen hours in transit."""
    return make_state(
        [
            leg("DUS", "London", datetime(2026, 9, 10, 7, 0), 240, 40.0),
            leg("London", "Rome", datetime(2026, 9, 11, 7, 0), 260, 40.0),
            leg("Rome", "Copenhagen", datetime(2026, 9, 12, 7, 0), 270, 40.0),
            leg("Copenhagen", "Madrid", datetime(2026, 9, 13, 7, 0), 250, 40.0),
            leg("Madrid", "DUS", datetime(2026, 9, 15, 18, 0), 60, 40.0),
        ]
    )


# ---------------------------------------------------------------------------
# The metric
# ---------------------------------------------------------------------------
def test_intensity_is_the_transit_share(scorer):
    state = calm_two_city()
    assessment = scorer.assess(state)
    assert assessment.intensity == pytest.approx(
        state.total_transport_minutes / state.trip_span_minutes, abs=1e-6
    )
    assert assessment.transport_minutes == state.total_transport_minutes


def test_the_two_trips_the_spec_contrasts(scorer):
    """Same days, same city count band, very different trips."""
    calm = scorer.assess(calm_two_city())
    frantic = scorer.assess(frantic_four_city())
    assert calm.intensity < frantic.intensity
    assert calm.legs_per_day < frantic.legs_per_day
    assert calm.score > frantic.score
    assert frantic.is_high
    assert not calm.is_high


def test_ground_transfers_count_as_transit(scorer):
    ride = transfer("Köln", "FRA", 30.0, 90)
    without = scorer.assess(calm_two_city())
    with_ride = scorer.assess(
        make_state(
            [
                leg("FRA", "Prague", datetime(2026, 9, 10, 9, 0), 75, 40.0),
                leg("Prague", "Vienna", datetime(2026, 9, 12, 10, 0), 90, 20.0),
                leg("Vienna", "FRA", datetime(2026, 9, 15, 18, 0), 95, 30.0),
            ],
            outbound_transfer=ride,
            return_transfer=ride,
        )
    )
    assert with_ride.intensity > without.intensity
    assert with_ride.transport_minutes == without.transport_minutes + 180


def test_airport_churn_is_counted(scorer):
    """Flights and transfers each cost a check-in that duration alone misses."""
    from travel_planner.models.transport import TransportType

    flights = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 120, 40.0),
            leg("Prague", "DUS", datetime(2026, 9, 14, 18, 0), 120, 40.0),
        ]
    )
    trains = make_state(
        [
            leg(
                "DUS", "Prague", datetime(2026, 9, 10, 9, 0), 120, 40.0,
                TransportType.TRAIN,
            ),
            leg(
                "Prague", "DUS", datetime(2026, 9, 14, 18, 0), 120, 40.0,
                TransportType.TRAIN,
            ),
        ]
    )
    assert scorer.assess(trains).airport_changes_per_day == 0.0
    assert scorer.assess(flights).airport_changes_per_day > 0.0
    assert scorer.assess(trains).score > scorer.assess(flights).score


@pytest.mark.parametrize("style", list(TravelStyle))
def test_travel_style_scales_tolerance(scorer, style):
    state = frantic_four_city()
    assert 0.0 <= scorer.assess(state, style).score <= 1.0
    # Intensity itself is a fact; only how much it *bothers* you varies.
    assert scorer.assess(state, style).intensity == scorer.assess(state).intensity


def test_a_packed_traveler_minds_less(scorer):
    state = frantic_four_city()
    relaxed = scorer.assess(state, TravelStyle.RELAXED).score
    packed = scorer.assess(state, TravelStyle.PACKED).score
    assert packed > relaxed
    assert STYLE_TOLERANCE[TravelStyle.PACKED] > STYLE_TOLERANCE[TravelStyle.RELAXED]


def test_multi_city_is_not_punished_for_its_own_sake(scorer):
    """Two cities at a sane pace must not score worse than one."""
    one_city = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 75, 40.0),
            leg("Prague", "DUS", datetime(2026, 9, 15, 18, 0), 75, 40.0),
        ]
    )
    assert scorer.assess(calm_two_city()).score > 0.5
    assert scorer.assess(calm_two_city()).score > (
        scorer.assess(frantic_four_city()).score
    )
    # One city is calmer still, which is honest - the reward for the second
    # city comes from diversity and city-count fit, not from intensity.
    assert scorer.assess(one_city).score >= scorer.assess(calm_two_city()).score


# ---------------------------------------------------------------------------
# Effect on Travel Value
# ---------------------------------------------------------------------------
def test_intensity_feeds_travel_value(config, destinations):
    value = TravelValueScorer(config, destinations)
    request = trip_request(travelers=1, budget=600, preferred_destinations=[])
    assert value.intensity_score(calm_two_city(), request) > value.intensity_score(
        frantic_four_city(), request
    )


def test_adventure_pays_for_exploration_but_not_for_a_slog(config, destinations):
    """ADVENTURE may buy more cities; it may not buy four airports in four days."""
    value = TravelValueScorer(config, destinations)
    request = trip_request(travelers=1, budget=600, preferred_destinations=[])
    calm, frantic = calm_two_city(), frantic_four_city()
    assert calm.total_cost == frantic.total_cost == 200.0

    # The frantic trip does buy more diversity ...
    assert value.diversity_score(frantic) > value.diversity_score(calm)
    # ... and ADVENTURE minds the intensity less than BEST_VALUE does ...
    gap_adventure = value.total(calm, request, ADVENTURE) - value.total(
        frantic, request, ADVENTURE
    )
    gap_best_value = value.total(calm, request, BEST_VALUE) - value.total(
        frantic, request, BEST_VALUE
    )
    assert gap_adventure < gap_best_value
    # ... but not enough to make the slog the better trip.
    assert value.total(calm, request, ADVENTURE) > value.total(
        frantic, request, ADVENTURE
    )


def test_travel_intensity_reaches_the_itinerary(planner):
    result = planner.plan(trip_request(budget=650))
    for itinerary in result.recommendations:
        assert 0.0 <= itinerary.travel_intensity <= 1.0
        assert itinerary.value_breakdown.legs_per_day > 0
        assert itinerary.travel_intensity == pytest.approx(
            itinerary.total_transport_minutes
            / ((itinerary.arrival - itinerary.departure).total_seconds() / 60),
            abs=1e-4,
        )


def test_the_intensity_threshold_is_documented():
    assert 0.0 < MAX_TRANSIT_SHARE < 1.0
