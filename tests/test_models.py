"""Domain model invariants."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from detoura.models.destination import Destination
from detoura.models.search import SearchState
from detoura.models.transport import TransportOption, TransportType
from detoura.models.trip import TravelPreferences, TripRequest

from .conftest import leg, make_state, trip_request


# ---------------------------------------------------------------------------
# TransportOption
# ---------------------------------------------------------------------------
def test_duration_is_derived_when_omitted():
    option = TransportOption(
        id="x",
        origin="DUS",
        destination="Prague",
        departure=datetime(2026, 9, 10, 8, 0),
        arrival=datetime(2026, 9, 10, 9, 15),
        price_per_person=55.0,
        transport_type=TransportType.FLIGHT,
        duration_minutes=None,
    )
    assert option.duration_minutes == 75


def test_inconsistent_duration_is_rejected():
    with pytest.raises(ValidationError, match="disagrees"):
        TransportOption(
            id="x",
            origin="DUS",
            destination="Prague",
            departure=datetime(2026, 9, 10, 8, 0),
            arrival=datetime(2026, 9, 10, 9, 15),
            price_per_person=55.0,
            transport_type=TransportType.FLIGHT,
            duration_minutes=999,
        )


def test_arrival_before_departure_is_rejected():
    with pytest.raises(ValidationError, match="precedes departure"):
        TransportOption(
            id="x",
            origin="DUS",
            destination="Prague",
            departure=datetime(2026, 9, 10, 9, 0),
            arrival=datetime(2026, 9, 10, 8, 0),
            price_per_person=55.0,
            transport_type=TransportType.FLIGHT,
            duration_minutes=0,
        )


def test_total_price_multiplies_by_travelers():
    """Per-person price must never be confused with the party total."""
    option = leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0)
    assert option.price_per_person == 55.0
    assert option.total_price(1) == 55.0
    assert option.total_price(2) == 110.0
    assert option.total_price(4) == 220.0


def test_transport_option_is_frozen_and_hashable():
    option = leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0)
    with pytest.raises(ValidationError):
        option.price_per_person = 1.0
    assert len({option, option}) == 1


# ---------------------------------------------------------------------------
# Destination / preferences
# ---------------------------------------------------------------------------
def test_destination_attribute_vector():
    destination = Destination(
        id="Prague",
        name="Prague",
        country="Czechia",
        history=0.95,
        nature=0.4,
        nightlife=0.85,
        culture=0.85,
        food=0.7,
        recommended_min_days=2,
        recommended_max_days=4,
    )
    assert destination.attribute_vector() == {
        "history": 0.95,
        "nature": 0.4,
        "nightlife": 0.85,
        "culture": 0.85,
        "food": 0.7,
    }


def test_destination_rejects_inverted_day_range():
    with pytest.raises(ValidationError, match="recommended_max_days"):
        Destination(
            id="X",
            name="X",
            country="Y",
            history=0.5,
            nature=0.5,
            nightlife=0.5,
            culture=0.5,
            food=0.5,
            recommended_min_days=5,
            recommended_max_days=2,
        )


def test_preferences_have_defaults_and_exclude_multiple_cities_from_attributes():
    preferences = TravelPreferences()
    assert preferences.history == 0.5
    assert preferences.multiple_cities == 0.5
    assert "multiple_cities" not in preferences.attribute_weights()
    assert set(preferences.attribute_weights()) == {
        "history",
        "nature",
        "nightlife",
        "culture",
        "food",
    }


@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_preferences_must_be_normalized(value):
    with pytest.raises(ValidationError):
        TravelPreferences(history=value)


# ---------------------------------------------------------------------------
# TripRequest
# ---------------------------------------------------------------------------
def test_request_rejects_inverted_window():
    with pytest.raises(ValidationError, match="date_to"):
        trip_request(date_from=date(2026, 9, 15), date_to=date(2026, 9, 10))


def test_request_rejects_duration_longer_than_window():
    with pytest.raises(ValidationError, match="does not fit"):
        trip_request(duration_days=30)


def test_request_rejects_contradictory_destinations():
    with pytest.raises(ValidationError, match="both mandatory and avoided"):
        trip_request(must_visit=["Paris"], avoid_destinations=["paris"])


def test_request_rejects_empty_transport_preferences():
    with pytest.raises(ValidationError, match="transport_preferences"):
        trip_request(transport_preferences=[])


def test_start_dates_respect_flexibility():
    fixed = trip_request(date_flexible=False)
    assert fixed.candidate_start_dates() == [date(2026, 9, 10)]

    flexible = trip_request(date_flexible=True)
    # A 5-day trip in a 10th-15th window can start on the 10th or the 11th.
    assert flexible.candidate_start_dates() == [date(2026, 9, 10), date(2026, 9, 11)]


def test_max_trip_minutes():
    assert trip_request(duration_days=5).max_trip_minutes == 5 * 24 * 60


# ---------------------------------------------------------------------------
# SearchState
# ---------------------------------------------------------------------------
def test_state_extend_is_immutable_and_accumulates():
    start = datetime(2026, 9, 10, 8, 0)
    outbound = leg("DUS", "Prague", start, 75, 55.0)
    empty = SearchState(
        origin_airport="DUS",
        current_location="DUS",
        start_datetime=start,
        current_datetime=start,
    )
    extended = empty.extend(outbound, travelers=2)

    assert empty.route == () and empty.total_cost == 0.0
    assert extended.route == (outbound,)
    assert extended.total_cost == 110.0
    assert extended.cities == ("Prague",)
    assert extended.visited_cities == {"Prague"}
    assert extended.current_location == "Prague"
    assert extended.total_travel_minutes == 75
    assert not extended.completed


def test_return_leg_marks_completion_without_adding_a_city():
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
        leg("Prague", "DUS", datetime(2026, 9, 12, 8, 0), 75, 60.0),
    ]
    state = make_state(legs, travelers=1)
    assert state.completed
    assert state.cities == ("Prague",)
    assert state.current_location == "DUS"
    assert state.total_cost == 115.0
    assert state.stay_days == (2,)


def test_state_signature_and_elapsed():
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
        leg("Prague", "DUS", datetime(2026, 9, 12, 8, 0), 75, 60.0),
    ]
    state = make_state(legs)
    assert state.signature()[0] == "DUS"
    assert len(state.signature()) == 3
    # Elapsed is measured from midnight of the start date.
    assert state.elapsed_minutes == int(
        (state.current_datetime - state.start_datetime).total_seconds() // 60
    )
    assert state.elapsed_minutes == 2 * 24 * 60 + 9 * 60 + 15


def test_state_rejects_zero_travelers():
    option = leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0)
    with pytest.raises(ValueError, match="travelers"):
        option.total_price(0)


def test_transport_option_rejects_self_loop():
    with pytest.raises(ValidationError, match="identical"):
        TransportOption(
            id="x",
            origin="DUS",
            destination="DUS",
            departure=datetime(2026, 9, 10, 8, 0),
            arrival=datetime(2026, 9, 10, 8, 0) + timedelta(minutes=10),
            price_per_person=1.0,
            transport_type=TransportType.BUS,
            duration_minutes=10,
        )
