"""Constraint engine: every rule, in isolation."""

from __future__ import annotations

from datetime import datetime

import pytest

from travel_planner.config import PlannerConfig
from travel_planner.constraints.validator import ConstraintValidator
from travel_planner.models.debug import RejectionReason
from travel_planner.models.transport import TransportType

from .conftest import leg, make_state, trip_request

AIRPORTS = ["CGN", "DUS", "FRA", "EIN"]
CITIES = ["London", "Brussels", "Prague", "Vienna", "Madrid", "Paris", "Amsterdam"]


@pytest.fixture
def validator(config: PlannerConfig) -> ConstraintValidator:
    return ConstraintValidator(
        config, origin_airports=AIRPORTS, destination_ids=CITIES
    )


def prague_vienna(travelers: int = 1, price_out: float = 55.0):
    """``DUS -> Prague -> Vienna -> DUS``, 4.4 days, well inside the window."""
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, price_out),
        leg("Prague", "Vienna", datetime(2026, 9, 12, 8, 0), 270, 12.0),
        leg("Vienna", "DUS", datetime(2026, 9, 14, 8, 0), 95, 25.0),
    ]
    return make_state(legs, travelers=travelers)


# ---------------------------------------------------------------------------
def test_valid_itinerary_passes(validator):
    result = validator.validate(prague_vienna(), trip_request(travelers=1))
    assert result.valid
    assert result.reason is None
    assert bool(result) is True


def test_budget_exceeded_is_rejected(validator):
    state = prague_vienna(travelers=2)
    assert state.total_cost == 184.0
    assert validator.validate(state, trip_request(budget=250)).valid

    result = validator.validate(state, trip_request(budget=150))
    assert not result.valid
    assert result.reason is RejectionReason.BUDGET_EXCEEDED
    assert "184" in result.detail


def test_travelers_scale_the_total_cost(validator):
    """Total price is per-person price x travelers, and the budget sees the total."""
    assert prague_vienna(travelers=1).total_cost == 92.0
    assert prague_vienna(travelers=2).total_cost == 184.0
    assert prague_vienna(travelers=3).total_cost == 276.0

    request = trip_request(budget=200)
    assert validator.validate(prague_vienna(travelers=2), request).valid
    assert (
        validator.validate(prague_vienna(travelers=3), request).reason
        is RejectionReason.BUDGET_EXCEEDED
    )


def test_duration_exceeded_is_rejected(validator):
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 20.0),
        leg("Prague", "DUS", datetime(2026, 9, 15, 8, 0), 75, 20.0),
    ]
    state = make_state(legs)
    result = validator.validate(state, trip_request(duration_days=5))
    assert result.reason is RejectionReason.DURATION_EXCEEDED


def test_duration_underused_is_rejected(validator):
    """A 5-day request is not satisfied by a 36-hour round trip."""
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 20.0),
        leg("Prague", "DUS", datetime(2026, 9, 11, 8, 0), 75, 20.0),
    ]
    result = validator.validate(make_state(legs), trip_request())
    assert result.reason is RejectionReason.DURATION_UNDERUSED


def test_duration_underuse_check_can_be_switched_off():
    relaxed = ConstraintValidator(
        PlannerConfig(min_duration_utilization=0.0),
        origin_airports=AIRPORTS,
        destination_ids=CITIES,
    )
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 20.0),
        leg("Prague", "DUS", datetime(2026, 9, 11, 8, 0), 75, 20.0),
    ]
    assert relaxed.validate(make_state(legs), trip_request()).valid


def test_avoided_destination_is_rejected_not_merely_penalized(validator):
    legs = [
        leg("DUS", "Paris", datetime(2026, 9, 10, 8, 0), 90, 20.0),
        leg("Paris", "DUS", datetime(2026, 9, 14, 8, 0), 90, 20.0),
    ]
    result = validator.validate(
        make_state(legs), trip_request(avoid_destinations=["Paris"])
    )
    assert result.reason is RejectionReason.AVOIDED_DESTINATION
    # ... and it is fine when not avoided.
    assert validator.validate(
        make_state(legs), trip_request(avoid_destinations=[])
    ).valid


def test_avoid_matching_is_accent_and_case_insensitive(validator):
    legs = [
        leg("DUS", "Vienna", datetime(2026, 9, 10, 8, 0), 95, 20.0),
        leg("Vienna", "DUS", datetime(2026, 9, 14, 8, 0), 95, 20.0),
    ]
    result = validator.validate(
        make_state(legs), trip_request(avoid_destinations=["wien"])
    )
    assert result.reason is RejectionReason.AVOIDED_DESTINATION


def test_must_visit_is_mandatory_for_completed_itineraries(validator):
    state = prague_vienna()
    result = validator.validate(state, trip_request(must_visit=["London"]))
    assert result.reason is RejectionReason.MISSING_MANDATORY_DESTINATION
    assert "London" in result.detail
    # Prague is visited, so requiring it is satisfied.
    assert validator.validate(state, trip_request(must_visit=["Prague"])).valid


def test_must_visit_does_not_reject_partial_states(validator):
    """A half-built route has not failed the mandatory rule yet."""
    partial = make_state(
        [leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0)], completed=False
    )
    assert validator.validate(partial, trip_request(must_visit=["Vienna"])).valid


def test_duplicate_destination_is_rejected(validator):
    legs = [
        leg("DUS", "London", datetime(2026, 9, 10, 8, 0), 90, 20.0),
        leg("London", "Brussels", datetime(2026, 9, 11, 8, 0), 140, 20.0),
        leg("Brussels", "London", datetime(2026, 9, 12, 8, 0), 140, 20.0),
        leg("London", "DUS", datetime(2026, 9, 13, 8, 0), 90, 20.0),
    ]
    result = validator.validate(make_state(legs), trip_request())
    assert result.reason is RejectionReason.DUPLICATE_DESTINATION


def test_minimum_city_stay_is_enforced(validator):
    """Arriving and leaving on the same calendar day is not a city stay."""
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 20.0),
        leg("Prague", "Vienna", datetime(2026, 9, 10, 18, 0), 270, 12.0),
        leg("Vienna", "DUS", datetime(2026, 9, 14, 8, 0), 95, 25.0),
    ]
    result = validator.validate(make_state(legs), trip_request())
    assert result.reason is RejectionReason.MIN_CITY_STAY_VIOLATED


def test_transport_type_must_be_allowed(validator):
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 20.0),
        leg("Prague", "Vienna", datetime(2026, 9, 12, 8, 0), 270, 12.0, TransportType.BUS),
        leg("Vienna", "DUS", datetime(2026, 9, 14, 8, 0), 95, 25.0),
    ]
    state = make_state(legs)
    flights_and_trains = trip_request(
        transport_preferences=[TransportType.FLIGHT, TransportType.TRAIN]
    )
    result = validator.validate(state, flights_and_trains)
    assert result.reason is RejectionReason.TRANSPORT_TYPE_NOT_ALLOWED

    assert validator.validate(
        state,
        trip_request(
            transport_preferences=[TransportType.FLIGHT, TransportType.BUS]
        ),
    ).valid


def test_date_window_is_enforced(validator):
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 8, 8, 0), 75, 20.0),
        leg("Prague", "DUS", datetime(2026, 9, 12, 8, 0), 75, 20.0),
    ]
    result = validator.validate(make_state(legs), trip_request())
    assert result.reason is RejectionReason.DATE_WINDOW_VIOLATED
    assert "2026-09-08" in result.detail


def test_arrival_outside_the_window_is_rejected(validator):
    """A leg departing on the last allowed day may not land after it."""
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 11, 8, 0), 75, 20.0),
        leg("Prague", "DUS", datetime(2026, 9, 15, 23, 30), 75, 20.0),
    ]
    result = validator.validate(make_state(legs), trip_request())
    assert result.reason in {
        RejectionReason.DATE_WINDOW_VIOLATED,
        RejectionReason.DURATION_EXCEEDED,
    }


def test_completed_itinerary_must_end_at_an_origin_airport(validator):
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 20.0),
        leg("Prague", "Vienna", datetime(2026, 9, 14, 8, 0), 270, 12.0),
    ]
    state = make_state(legs)  # last leg marked as the return, but lands in Vienna
    result = validator.validate(state, trip_request())
    assert result.reason is RejectionReason.NOT_RETURNED_TO_ORIGIN


def test_returning_to_a_different_origin_airport_is_allowed(validator):
    """Fly out of DUS, come back into CGN - both are origin airports."""
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
        leg("Prague", "CGN", datetime(2026, 9, 14, 8, 0), 80, 52.0),
    ]
    assert validator.validate(make_state(legs), trip_request(travelers=1)).valid


def test_broken_connection_chain_is_rejected(validator):
    legs = [
        leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 20.0),
        leg("Vienna", "DUS", datetime(2026, 9, 14, 8, 0), 95, 25.0),
    ]
    result = validator.validate(make_state(legs), trip_request())
    assert result.reason is RejectionReason.INVALID_CONNECTION


def test_max_cities_is_enforced():
    small = ConstraintValidator(
        PlannerConfig(max_cities=1), origin_airports=AIRPORTS, destination_ids=CITIES
    )
    result = small.validate(prague_vienna(), trip_request())
    assert result.reason is RejectionReason.MAX_CITIES_EXCEEDED


# ---------------------------------------------------------------------------
# Pruning bounds
# ---------------------------------------------------------------------------
class _Estimator:
    def __init__(self, price: float | None, minutes: int | None) -> None:
        self.price, self.minutes = price, minutes

    def min_return_price_per_person(self, city: str) -> float | None:
        return self.price

    def min_return_minutes(self, city: str) -> int | None:
        return self.minutes


def test_unreachable_return_budget_prunes_partial_states(config):
    """A state whose cheapest way home already blows the budget is dropped."""
    partial = make_state(
        [leg("DUS", "London", datetime(2026, 9, 10, 8, 0), 90, 35.0)], completed=False
    )
    validator = ConstraintValidator(
        config,
        origin_airports=AIRPORTS,
        destination_ids=CITIES,
        return_estimator=_Estimator(price=95.0, minutes=90),
    )
    # 35 + 95 = 130 per person; for two travelers that is 260 > 200.
    partial_two = make_state(
        [leg("DUS", "London", datetime(2026, 9, 10, 8, 0), 90, 35.0)],
        travelers=2,
        completed=False,
    )
    result = validator.validate(partial_two, trip_request(budget=200, travelers=2))
    assert result.reason is RejectionReason.UNREACHABLE_RETURN_BUDGET

    # With a generous budget the same state survives.
    assert validator.validate(partial, trip_request(budget=500, travelers=1)).valid


def test_unreachable_return_time_prunes_partial_states(config):
    validator = ConstraintValidator(
        config,
        origin_airports=AIRPORTS,
        destination_ids=CITIES,
        return_estimator=_Estimator(price=10.0, minutes=90),
    )
    # Duration is measured as the trip's span, so "no time left" means the
    # traveler has already been away for nearly the whole allowance.
    late = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 10.0),
            leg("Prague", "London", datetime(2026, 9, 14, 20, 0), 90, 10.0),
        ],
        completed=False,
        start=datetime(2026, 9, 10, 0, 0),
    )
    assert late.trip_span_minutes > 0.9 * trip_request().max_trip_minutes
    result = validator.validate(late, trip_request(budget=500))
    assert result.reason is RejectionReason.UNREACHABLE_RETURN_TIME


def test_missing_return_connection_is_pruned(config):
    validator = ConstraintValidator(
        config,
        origin_airports=AIRPORTS,
        destination_ids=CITIES,
        return_estimator=_Estimator(price=None, minutes=None),
    )
    partial = make_state(
        [leg("DUS", "London", datetime(2026, 9, 10, 8, 0), 90, 35.0)], completed=False
    )
    result = validator.validate(partial, trip_request(budget=500))
    assert result.reason is RejectionReason.UNREACHABLE_RETURN_BUDGET
