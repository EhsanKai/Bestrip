"""Multi-objective scoring.

Every component returns a value in ``[0, 1]``; the total is the weighted mean
using :class:`~travel_planner.config.ScoreWeights`, so it is also in ``[0, 1]``
and comparable across requests.

The formulas are deliberately explicit rather than clever: each one is unit
tested in ``tests/test_scoring.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

from ..config import PlannerConfig
from ..data.destinations import canonical_key
from ..models.destination import ATTRIBUTES, Destination
from ..models.itinerary import ScoreBreakdown
from ..models.search import SearchState
from ..models.transport import TransportType
from ..models.trip import TripRequest
from ..providers.destinations import DestinationProvider

#: How comfortable each mode is, independent of price or duration.
MODE_COMFORT: dict[TransportType, float] = {
    TransportType.FLIGHT: 1.00,
    TransportType.TRAIN: 0.85,
    TransportType.BUS: 0.55,
}

#: A stay this many days outside a city's recommended range scores zero fit.
STAY_FIT_TOLERANCE_DAYS = 3.0

#: Average leg duration (minutes) at which the comfort term reaches zero.
LEG_DURATION_REFERENCE_MINUTES = 360.0

#: Travel days per trip day at or below which the pace feels relaxed ...
COMFORTABLE_LEGS_PER_DAY = 0.5
#: ... and at or above which it feels frantic.
MAX_LEGS_PER_DAY = 1.5

#: Weight of the plain stay-quality term inside DestinationScore.
STAY_FIT_WEIGHT = 1.0


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


@dataclass(frozen=True, slots=True)
class Objectives:
    """The vector compared by the Pareto filter."""

    cost: float
    """Lower is better."""
    travel_minutes: int
    """Lower is better."""
    city_count: int
    """Higher is better."""
    preference_score: float
    """Higher is better."""


class ScoringEngine:
    """Turns a search state into a :class:`ScoreBreakdown`."""

    def __init__(self, config: PlannerConfig, destinations: DestinationProvider) -> None:
        self.config = config
        self.destinations = destinations

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _destination(self, city: str) -> Destination | None:
        return self.destinations.get(city)

    def _stays(self, state: SearchState) -> list[int]:
        """Stay lengths padded to one entry per visited city.

        A partial state has no stay recorded for the city it is currently in;
        the optimistic assumption is the configured minimum.
        """
        stays = list(state.stay_days)
        while len(stays) < len(state.cities):
            stays.append(self.config.min_city_stay_days)
        return stays[: len(state.cities)]

    # ------------------------------------------------------------------
    # Components
    # ------------------------------------------------------------------
    def budget_score(self, total_cost: float, budget: float) -> float:
        """``1 - cost / budget``, clamped. Over-budget routes score zero."""
        if budget <= 0:
            return 0.0
        return clamp01(1.0 - total_cost / budget)

    def attribute_match(self, destination: Destination, request: TripRequest) -> float:
        """Weighted overlap between a city's profile and the user's weights."""
        weights = request.preferences.attribute_weights()
        total_weight = sum(weights.values())
        if total_weight <= 0:
            # The user cares about none of the attributes: stay neutral rather
            # than pretending every city is a perfect match.
            return 0.5
        vector = destination.attribute_vector()
        return clamp01(
            sum(weights[name] * vector[name] for name in ATTRIBUTES) / total_weight
        )

    def multi_city_component(self, city_count: int) -> float:
        """How "multi-city" an itinerary is, normalized to ``[0, 1]``.

        ``1 - 1/n``: one city scores 0, and each extra city helps less than the
        one before - going from one city to two is the meaningful jump, going
        from three to four much less so.
        """
        if city_count < 1:
            return 0.0
        return clamp01(1.0 - 1.0 / city_count)

    def preference_score(self, state: SearchState, request: TripRequest) -> float:
        """Blends how well the cities fit the user with how many there are.

        ``multiple_cities`` acts as the weight of the city-count term, so a user
        with ``multiple_cities = 0`` gets no benefit at all from extra cities,
        while ``multiple_cities = 1`` weights it equally with city quality.
        """
        if not state.cities:
            return 0.0
        matches = []
        for city in state.cities:
            destination = self._destination(city)
            matches.append(
                self.attribute_match(destination, request) if destination else 0.5
            )
        attribute_component = sum(matches) / len(matches)

        multi_weight = request.preferences.multiple_cities
        multi_component = self.multi_city_component(len(state.cities))
        return clamp01(
            (attribute_component + multi_weight * multi_component) / (1.0 + multi_weight)
        )

    def stay_fit(self, city: str, stay_days: int) -> float:
        """How well a planned stay matches the city's recommended range."""
        destination = self._destination(city)
        if destination is None:
            return 0.5
        if destination.recommended_min_days <= stay_days <= destination.recommended_max_days:
            return 1.0
        gap = (
            destination.recommended_min_days - stay_days
            if stay_days < destination.recommended_min_days
            else stay_days - destination.recommended_max_days
        )
        return clamp01(1.0 - gap / STAY_FIT_TOLERANCE_DAYS)

    def destination_score(self, state: SearchState, request: TripRequest) -> float:
        """Stay quality, plus coverage of preferred and mandatory destinations."""
        if not state.cities:
            return 0.0
        stays = self._stays(state)
        base = sum(
            self.stay_fit(city, stay) for city, stay in zip(state.cities, stays)
        ) / len(state.cities)

        visited_keys = {canonical_key(city) for city in state.cities}
        numerator = STAY_FIT_WEIGHT * base
        denominator = STAY_FIT_WEIGHT

        if request.preferred_destinations:
            wanted = {canonical_key(name) for name in request.preferred_destinations}
            coverage = len(visited_keys & wanted) / len(wanted)
            numerator += self.config.preferred_destination_bonus * coverage
            denominator += self.config.preferred_destination_bonus

        if request.must_visit:
            mandatory = {canonical_key(name) for name in request.must_visit}
            coverage = len(visited_keys & mandatory) / len(mandatory)
            numerator += self.config.must_visit_bonus * coverage
            denominator += self.config.must_visit_bonus

        return clamp01(numerator / denominator)

    def convenience_score(self, state: SearchState) -> float:
        """Penalizes leg count, uncomfortable modes, long hops and a frantic pace.

        The pace term is what stops the optimizer from buying a ridiculous
        itinerary with a low price: four cities in four days means packing up
        and moving on almost every day, and that is unpleasant no matter how
        cheap the tickets are.
        """
        if not state.route:
            return 0.0
        min_legs = 2
        max_legs = self.config.max_cities + 1
        span = max(max_legs - min_legs, 1)
        leg_efficiency = clamp01(1.0 - (state.leg_count - min_legs) / span)

        comfort = sum(MODE_COMFORT.get(leg.transport_type, 0.7) for leg in state.route)
        comfort /= state.leg_count

        average_leg = state.total_travel_minutes / state.leg_count
        hop_comfort = clamp01(1.0 - average_leg / LEG_DURATION_REFERENCE_MINUTES)

        elapsed_days = max(state.elapsed_minutes, 1) / (24 * 60)
        legs_per_day = state.leg_count / elapsed_days
        pace = clamp01(
            (MAX_LEGS_PER_DAY - legs_per_day) / (MAX_LEGS_PER_DAY - COMFORTABLE_LEGS_PER_DAY)
        )

        return clamp01((leg_efficiency + comfort + hop_comfort + pace) / 4.0)

    def time_score(
        self, state: SearchState, request: TripRequest, *, optimistic: bool = False
    ) -> float:
        """Rewards spending the trip on the ground rather than in transit.

        Also rewards actually using the requested duration: a 5-day allowance
        burned in 36 hours is not the trip the user asked for.

        ``optimistic`` is used when ranking *partial* states. A half-built
        itinerary has not used its days yet but still could, so charging it for
        the unused time would make the beam prefer states that are already
        nearly over - and the search would never reach three-city routes.
        """
        elapsed = max(state.elapsed_minutes, 1)
        travel_fraction = state.total_travel_minutes / elapsed
        transit_quality = clamp01(
            1.0 - travel_fraction / self.config.max_travel_time_fraction
        )
        duration_use = (
            1.0
            if optimistic
            else clamp01(elapsed / max(request.max_trip_minutes, 1))
        )
        return clamp01(0.5 * transit_quality + 0.5 * duration_use)

    def diversity_score(self, state: SearchState) -> float:
        """Variety *within* one itinerary: countries seen and modes used."""
        if not state.cities or not state.route:
            return 0.0
        countries = set()
        for city in state.cities:
            destination = self._destination(city)
            countries.add(destination.country if destination else city)
        country_variety = len(countries) / len(state.cities)

        modes = {leg.transport_type for leg in state.route}
        mode_variety = len(modes) / min(state.leg_count, len(TransportType))

        return clamp01(0.6 * country_variety + 0.4 * mode_variety)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------
    def components(
        self, state: SearchState, request: TripRequest, *, optimistic: bool = False
    ) -> dict[str, float]:
        """The six raw component scores, each in ``[0, 1]``."""
        return {
            "budget": self.budget_score(state.total_cost, request.budget),
            "preference": self.preference_score(state, request),
            "destination": self.destination_score(state, request),
            "convenience": self.convenience_score(state),
            "time": self.time_score(state, request, optimistic=optimistic),
            "diversity": self.diversity_score(state),
        }

    def weighted_total(self, components: dict[str, float]) -> float:
        weights = self.config.score_weights.normalized()
        return sum(weights[name] * value for name, value in components.items())

    def score(self, state: SearchState, request: TripRequest) -> ScoreBreakdown:
        """The weighted total plus every component that produced it."""
        weights = self.config.score_weights.normalized()
        components = self.components(state, request)
        total = self.weighted_total(components)
        return ScoreBreakdown(
            **components,
            total=round(total, 6),
            weights=weights,
            notes=[
                f"cost {state.total_cost:.2f}/{request.budget:.2f} {request.currency}",
                f"{len(state.cities)} city/cities: {', '.join(state.cities) or '-'}",
                f"{state.total_travel_minutes} min in transit over {state.leg_count} leg(s)",
            ],
        )

    def hypothetical_completion(
        self,
        state: SearchState,
        request: TripRequest,
        *,
        return_price_per_person: float | None,
        return_minutes: int | None,
    ) -> SearchState:
        """The partial state as if it flew home on the cheapest possible leg."""
        price = return_price_per_person if return_price_per_person is not None else 0.0
        minutes = return_minutes if return_minutes is not None else 0
        return replace(
            state,
            total_cost=round(state.total_cost + price * request.travelers, 2),
            total_travel_minutes=state.total_travel_minutes + minutes,
            current_datetime=state.current_datetime
            + timedelta(minutes=minutes + self.config.min_city_stay_days * 24 * 60),
            completed=True,
        )

    def estimate(
        self,
        state: SearchState,
        request: TripRequest,
        *,
        return_price_per_person: float | None,
        return_minutes: int | None,
    ) -> ScoreBreakdown:
        """Optimistically score a partial state as if it went home right now.

        This is what makes the search non-greedy: a state parked in a city with
        an expensive way home is judged on the *complete* trip it implies, not
        on the cheap leg that got it there.
        """
        if state.completed or not state.route:
            return self.score(state, request)
        return self.score(
            self.hypothetical_completion(
                state,
                request,
                return_price_per_person=return_price_per_person,
                return_minutes=return_minutes,
            ),
            request,
        )

    def estimate_total(
        self,
        state: SearchState,
        request: TripRequest,
        *,
        return_price_per_person: float | None,
        return_minutes: int | None,
    ) -> float:
        """Fast path for beam ranking: the estimated total only, no breakdown."""
        if state.completed or not state.route:
            return self.weighted_total(self.components(state, request))
        hypothetical = self.hypothetical_completion(
            state,
            request,
            return_price_per_person=return_price_per_person,
            return_minutes=return_minutes,
        )
        return self.weighted_total(
            self.components(hypothetical, request, optimistic=True)
        )

    def objectives(self, state: SearchState, request: TripRequest) -> Objectives:
        """The multi-objective vector used for Pareto filtering."""
        return Objectives(
            cost=state.total_cost,
            travel_minutes=state.total_travel_minutes,
            city_count=state.city_count,
            preference_score=round(self.preference_score(state, request), 6),
        )
