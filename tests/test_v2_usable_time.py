"""Usable destination time: a trip day is not a sightseeing day."""

from __future__ import annotations

from datetime import datetime, time

import pytest

from travel_planner.algorithms.travel_value import TravelValueScorer
from travel_planner.config import PlannerConfig
from travel_planner.profiles import PROFILES, ProfileName
from travel_planner.usable_time import (
    DEFAULT_DAY_END,
    DEFAULT_DAY_START,
    usable_day_minutes,
    usable_minutes,
)

from .conftest import leg, make_state, trip_request

FULL_DAY = 13 * 60  # 08:00 -> 21:00


def test_a_usable_day_is_the_configured_window():
    assert usable_day_minutes(DEFAULT_DAY_START, DEFAULT_DAY_END) == FULL_DAY
    assert usable_day_minutes(time(9, 0), time(17, 0)) == 8 * 60


def test_one_whole_day_between_arrival_and_departure():
    """Arrive at 08:00, leave at 21:00 the next day: one full day plus one."""
    assert (
        usable_minutes(datetime(2026, 9, 10, 8, 0), datetime(2026, 9, 11, 21, 0))
        == 2 * FULL_DAY
    )


def test_a_late_arrival_buys_almost_nothing():
    """Landing at 23:30 contributes zero to the arrival day."""
    late = usable_minutes(
        datetime(2026, 9, 10, 23, 30), datetime(2026, 9, 11, 21, 0)
    )
    early = usable_minutes(
        datetime(2026, 9, 10, 9, 0), datetime(2026, 9, 11, 21, 0)
    )
    assert late == FULL_DAY  # only the second day counts
    assert early == FULL_DAY + 12 * 60
    assert late < early


def test_an_early_departure_costs_the_last_morning():
    """A 06:00 flight home contributes zero to the departure day."""
    early_out = usable_minutes(
        datetime(2026, 9, 10, 8, 0), datetime(2026, 9, 12, 6, 0)
    )
    late_out = usable_minutes(
        datetime(2026, 9, 10, 8, 0), datetime(2026, 9, 12, 20, 0)
    )
    assert early_out == 2 * FULL_DAY
    assert late_out == 2 * FULL_DAY + 12 * 60
    assert early_out < late_out


@pytest.mark.parametrize(
    ("arrival", "departure", "expected"),
    [
        # Both ends outside the window: nothing usable at all.
        ((21, 30), (7, 0), 0),
        # A single afternoon.
        ((14, 0), (18, 0), 4 * 60),
        # Departure before arrival is nonsense, not negative time.
        ((18, 0), (14, 0), 0),
    ],
)
def test_same_day_edge_cases(arrival, departure, expected):
    assert (
        usable_minutes(
            datetime(2026, 9, 10, *arrival), datetime(2026, 9, 10, *departure)
        )
        == expected
    )


def test_the_window_is_configurable():
    night_owl = usable_minutes(
        datetime(2026, 9, 10, 8, 0),
        datetime(2026, 9, 10, 23, 0),
        day_start=time(8, 0),
        day_end=time(23, 0),
    )
    assert night_owl == 15 * 60


# ---------------------------------------------------------------------------
# Effect on the state and on scoring
# ---------------------------------------------------------------------------
def _trip(arrival_hour: int, departure_hour: int, nights: int = 3):
    """A London round trip whose stay begins and ends at the given hours."""
    outbound = leg(
        "DUS", "London", datetime(2026, 9, 10, arrival_hour - 1, 30), 90, 35.0
    )
    inbound = leg(
        "London", "DUS", datetime(2026, 9, 10 + nights, departure_hour, 0), 90, 35.0
    )
    return make_state([outbound, inbound])


def test_state_accumulates_usable_minutes():
    civilised = _trip(10, 18)
    assert civilised.usable_destination_minutes > 0
    assert civilised.stays[0].usable_minutes == civilised.usable_destination_minutes


def test_a_late_arrival_reduces_the_state_usable_time():
    assert _trip(23, 18).usable_destination_minutes < _trip(10, 18).usable_destination_minutes


def test_an_early_departure_reduces_the_state_usable_time():
    assert _trip(10, 6).usable_destination_minutes < _trip(10, 18).usable_destination_minutes


def test_time_score_follows_usable_time(destinations):
    config = PlannerConfig()
    scorer = TravelValueScorer(config, destinations)
    request = trip_request(travelers=1)
    good, bad = _trip(10, 18), _trip(23, 6)
    assert scorer.time_score(good, request) > scorer.time_score(bad, request)


def test_optimistic_time_score_does_not_charge_for_unspent_days(destinations):
    """Ranking a half-built trip must not favour trips that are nearly over."""
    config = PlannerConfig()
    scorer = TravelValueScorer(config, destinations)
    request = trip_request(travelers=1)
    partial = make_state(
        [leg("DUS", "London", datetime(2026, 9, 10, 8, 0), 90, 35.0)], completed=False
    )
    assert scorer.time_score(partial, request, optimistic=True) > scorer.time_score(
        partial, request
    )


def test_stay_quality_measures_usable_days_not_calendar_nights(destinations):
    """London recommends two days, and a one-night flying visit does not deliver them.

    Both trips below book exactly one night. The difference is entirely in the
    clock: landing at 10:00 and leaving at 20:00 the next day is most of two
    usable days, while landing at 23:00 and leaving at 06:00 is none at all -
    and the score has to see that.
    """
    config = PlannerConfig()
    scorer = TravelValueScorer(config, destinations)
    generous = _trip(9, 20, nights=1)
    token = _trip(22, 6, nights=1)
    assert generous.stay_days == token.stay_days == (1,)
    assert token.usable_destination_minutes == 0
    assert scorer.stay_quality(generous) > scorer.stay_quality(token)
    assert scorer.stay_quality(token) == 0.0


def test_usable_time_reaches_the_itinerary(planner, koln_request):
    result = planner.plan(koln_request)
    for itinerary in result.recommendations:
        assert itinerary.usable_destination_minutes > 0
        assert itinerary.value_breakdown.usable_destination_minutes == (
            itinerary.usable_destination_minutes
        )
        assert 0.0 <= itinerary.value_breakdown.usable_ratio <= 1.0
        assert sum(s.usable_minutes for s in itinerary.stays) == (
            itinerary.usable_destination_minutes
        )


def test_a_transit_heavy_trip_scores_worse_on_time(destinations):
    """Same days away, very different amounts of it spent moving."""
    config = PlannerConfig()
    scorer = TravelValueScorer(config, destinations)
    request = trip_request(travelers=1)

    direct = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
            leg("Prague", "DUS", datetime(2026, 9, 14, 8, 0), 75, 55.0),
        ]
    )
    grinding = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 600, 55.0),
            leg("Prague", "DUS", datetime(2026, 9, 14, 8, 0), 600, 55.0),
        ]
    )
    profile = PROFILES[ProfileName.BEST_VALUE]
    assert scorer.time_score(grinding, request) < scorer.time_score(direct, request)
    assert scorer.total(grinding, request, profile) < scorer.total(
        direct, request, profile
    )
