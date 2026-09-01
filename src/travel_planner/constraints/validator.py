"""Constraint validation.

All feasibility rules live here. Beam search never decides on its own whether a
state is legal - it builds a candidate and asks the validator, which answers
with a structured :class:`ConstraintResult` naming the exact rule that failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

from ..config import PlannerConfig
from ..data.destinations import canonical_key
from ..models.debug import RejectionReason
from ..models.search import SearchState
from ..models.trip import TripRequest


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    """The outcome of validating a state."""

    valid: bool
    reason: RejectionReason | None = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.valid

    @staticmethod
    def ok() -> "ConstraintResult":
        return _OK

    @staticmethod
    def fail(reason: RejectionReason, detail: str = "") -> "ConstraintResult":
        return ConstraintResult(valid=False, reason=reason, detail=detail)


_OK = ConstraintResult(valid=True)


@runtime_checkable
class ReturnEstimator(Protocol):
    """Admissible lower bounds on getting home from a city.

    Used for search pruning (spec 37): a state whose *cheapest possible* return
    already blows the budget can never be completed and is dropped early.
    """

    def min_return_price_per_person(self, city: str) -> float | None: ...

    def min_return_minutes(self, city: str) -> int | None: ...


@runtime_checkable
class AccommodationEstimator(Protocol):
    """Admissible lower bound on the cost of sleeping somewhere.

    Declared here alongside :class:`ReturnEstimator` so ``constraints`` stays a
    leaf: the concrete caching implementations live in ``services``.
    """

    def min_stay_cost(self, city: str, nights: int, travelers: int) -> float: ...


@dataclass(frozen=True, slots=True)
class _Resolved:
    must_visit: frozenset[str]
    avoid: frozenset[str]


class ConstraintValidator:
    """Validates partial and completed search states against a request."""

    def __init__(
        self,
        config: PlannerConfig,
        *,
        origin_airports: Iterable[str],
        destination_ids: Iterable[str],
        return_estimator: ReturnEstimator | None = None,
        accommodation_estimator: AccommodationEstimator | None = None,
    ) -> None:
        self.config = config
        self.origin_airports = frozenset(origin_airports)
        self.destination_ids = frozenset(destination_ids)
        self.return_estimator = return_estimator
        self.accommodation_estimator = accommodation_estimator
        self._resolution_cache: dict[tuple[tuple[str, ...], tuple[str, ...]], _Resolved] = {}

    # ------------------------------------------------------------------
    # Name resolution
    # ------------------------------------------------------------------
    def _canonical_destination(self, name: str) -> str:
        """Map a user-supplied name onto a catalog id when one exists."""
        key = canonical_key(name)
        for destination_id in self.destination_ids:
            if canonical_key(destination_id) == key:
                return destination_id
        return name

    def resolve(self, request: TripRequest) -> _Resolved:
        cache_key = (tuple(request.must_visit), tuple(request.avoid_destinations))
        cached = self._resolution_cache.get(cache_key)
        if cached is None:
            cached = _Resolved(
                must_visit=frozenset(
                    self._canonical_destination(name) for name in request.must_visit
                ),
                avoid=frozenset(
                    self._canonical_destination(name) for name in request.avoid_destinations
                ),
            )
            self._resolution_cache[cache_key] = cached
        return cached

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def validate(self, state: SearchState, request: TripRequest) -> ConstraintResult:
        """Validate ``state``, applying completion rules once it has returned."""
        result = self._validate_common(state, request)
        if not result.valid:
            return result
        if state.completed:
            return self._validate_completed(state, request)
        return self._validate_partial(state, request)

    # ------------------------------------------------------------------
    # Rules shared by partial and completed states
    # ------------------------------------------------------------------
    def _validate_common(self, state: SearchState, request: TripRequest) -> ConstraintResult:
        resolved = self.resolve(request)

        if state.total_cost > request.budget + 1e-9:
            return ConstraintResult.fail(
                RejectionReason.BUDGET_EXCEEDED,
                f"total {state.total_cost:.2f} > budget {request.budget:.2f}",
            )

        if state.trip_span_minutes > request.max_trip_minutes:
            return ConstraintResult.fail(
                RejectionReason.DURATION_EXCEEDED,
                f"trip spans {state.trip_span_minutes} min > allowed "
                f"{request.max_trip_minutes} min",
            )

        if state.city_count > self.config.max_cities:
            return ConstraintResult.fail(
                RejectionReason.MAX_CITIES_EXCEEDED,
                f"{state.city_count} cities > max_cities {self.config.max_cities}",
            )

        forbidden = state.visited_cities & resolved.avoid
        if forbidden:
            return ConstraintResult.fail(
                RejectionReason.AVOIDED_DESTINATION,
                f"visits avoided destination(s) {sorted(forbidden)}",
            )

        if len(set(state.cities)) != len(state.cities):
            return ConstraintResult.fail(
                RejectionReason.DUPLICATE_DESTINATION,
                f"city visited twice in {list(state.cities)}",
            )

        allowed_types = set(request.transport_preferences)
        for leg in state.route:
            if leg.transport_type not in allowed_types:
                return ConstraintResult.fail(
                    RejectionReason.TRANSPORT_TYPE_NOT_ALLOWED,
                    f"{leg.id} uses {leg.transport_type.value}, "
                    f"allowed: {sorted(t.value for t in allowed_types)}",
                )

        for leg in state.route:
            if not (request.date_from <= leg.departure.date() <= request.date_to):
                return ConstraintResult.fail(
                    RejectionReason.DATE_WINDOW_VIOLATED,
                    f"{leg.id} departs {leg.departure.date()} outside "
                    f"{request.date_from}..{request.date_to}",
                )
            if not (request.date_from <= leg.arrival.date() <= request.date_to):
                return ConstraintResult.fail(
                    RejectionReason.DATE_WINDOW_VIOLATED,
                    f"{leg.id} arrives {leg.arrival.date()} outside "
                    f"{request.date_from}..{request.date_to}",
                )

        # Legs must actually chain: each departure is at or after the previous
        # arrival, and a stay must respect the configured minimum.
        for previous, following in zip(state.route, state.route[1:]):
            if following.origin != previous.destination:
                return ConstraintResult.fail(
                    RejectionReason.INVALID_CONNECTION,
                    f"{following.id} departs {following.origin}, "
                    f"but the traveler is in {previous.destination}",
                )
            if following.departure < previous.arrival:
                return ConstraintResult.fail(
                    RejectionReason.INVALID_CONNECTION,
                    f"{following.id} departs before arriving on {previous.id}",
                )
            stay = (following.departure.date() - previous.arrival.date()).days
            if stay < self.config.min_city_stay_days:
                return ConstraintResult.fail(
                    RejectionReason.MIN_CITY_STAY_VIOLATED,
                    f"stay in {previous.destination} is {stay} day(s), "
                    f"minimum {self.config.min_city_stay_days}",
                )

        return _OK

    # ------------------------------------------------------------------
    # Partial states: is completion still conceivable?
    # ------------------------------------------------------------------
    def _validate_partial(self, state: SearchState, request: TripRequest) -> ConstraintResult:
        if state.current_location in self.origin_airports and state.route:
            return ConstraintResult.fail(
                RejectionReason.INVALID_CONNECTION,
                "a mid-trip leg may not terminate at an origin airport",
            )

        # A route with more mandatory destinations still to see than city slots
        # left can never be completed, whatever happens next.
        missing = self.resolve(request).must_visit - state.visited_cities
        remaining_slots = self.config.max_cities - state.city_count
        if len(missing) > remaining_slots:
            return ConstraintResult.fail(
                RejectionReason.MISSING_MANDATORY_DESTINATION,
                f"{len(missing)} mandatory destination(s) left ({sorted(missing)}) "
                f"but only {remaining_slots} city slot(s) remain",
            )

        if not state.route:
            return _OK

        remaining_budget = request.budget - state.total_cost

        # The traveler still has to sleep somewhere before they can leave, so
        # the cheapest possible remaining nights are charged against what is
        # left of the budget. Admissible: no bookable stay is cheaper.
        if self.accommodation_estimator is not None:
            floor = self.accommodation_estimator.min_stay_cost(
                state.current_location,
                self.config.min_city_stay_days,
                request.travelers,
            )
            if floor > remaining_budget + 1e-9:
                return ConstraintResult.fail(
                    RejectionReason.UNAFFORDABLE_ACCOMMODATION,
                    f"the cheapest {self.config.min_city_stay_days} night(s) in "
                    f"{state.current_location} cost {floor:.2f}, only "
                    f"{remaining_budget:.2f} left",
                )
            remaining_budget -= floor

        if not self.return_estimator:
            return _OK

        min_price = self.return_estimator.min_return_price_per_person(state.current_location)
        if min_price is None:
            return ConstraintResult.fail(
                RejectionReason.UNREACHABLE_RETURN_BUDGET,
                f"no return connection from {state.current_location}",
            )
        min_return_cost = min_price * request.travelers
        if min_return_cost > remaining_budget + 1e-9:
            return ConstraintResult.fail(
                RejectionReason.UNREACHABLE_RETURN_BUDGET,
                f"cheapest return from {state.current_location} costs "
                f"{min_return_cost:.2f}, only {remaining_budget:.2f} left",
            )

        min_minutes = self.return_estimator.min_return_minutes(state.current_location)
        if min_minutes is None:
            return ConstraintResult.fail(
                RejectionReason.UNREACHABLE_RETURN_TIME,
                f"no return connection from {state.current_location}",
            )
        # The traveler must still sit out the minimum stay before leaving.
        remaining_minutes = request.max_trip_minutes - state.trip_span_minutes
        needed = min_minutes + self.config.min_city_stay_days * 24 * 60
        if needed > remaining_minutes:
            return ConstraintResult.fail(
                RejectionReason.UNREACHABLE_RETURN_TIME,
                f"returning from {state.current_location} needs at least {needed} min, "
                f"only {remaining_minutes} min left",
            )

        return _OK

    # ------------------------------------------------------------------
    # Completed states
    # ------------------------------------------------------------------
    def _validate_completed(self, state: SearchState, request: TripRequest) -> ConstraintResult:
        resolved = self.resolve(request)

        if state.current_location not in self.origin_airports:
            return ConstraintResult.fail(
                RejectionReason.NOT_RETURNED_TO_ORIGIN,
                f"itinerary ends in {state.current_location}, not at an origin airport",
            )

        if not state.cities:
            return ConstraintResult.fail(
                RejectionReason.NO_CITIES_VISITED,
                "an itinerary must visit at least one destination",
            )

        missing = resolved.must_visit - state.visited_cities
        if missing:
            return ConstraintResult.fail(
                RejectionReason.MISSING_MANDATORY_DESTINATION,
                f"missing mandatory destination(s) {sorted(missing)}",
            )

        minimum = self.config.min_duration_utilization * request.max_trip_minutes
        if state.trip_span_minutes < minimum:
            return ConstraintResult.fail(
                RejectionReason.DURATION_UNDERUSED,
                f"trip spans {state.trip_span_minutes} min, less than "
                f"{self.config.min_duration_utilization:.0%} of the requested "
                f"{request.max_trip_minutes} min",
            )

        return _OK
