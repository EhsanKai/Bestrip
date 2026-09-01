"""Travel Value scoring (V2).

V1 asked "what is the cheapest transportation route?". V2 asks "what is the
best trip this money and time can buy?". The difference is the objective
function, and this module is it.

Five components, each in ``[0, 1]``, weighted by the active
:class:`~travel_planner.profiles.RecommendationProfile`:

======================  =========================================================
CostScore               efficiency *and* sensible use of the budget
ExperienceScore         destination quality, stay quality, sane pace
PreferenceScore         match against the user's taste and appetite for cities
TimeScore               usable destination time vs. time in transit
DiversityScore          countries, transport modes, number of places seen
======================  =========================================================

The V1 :class:`~travel_planner.algorithms.scoring.ScoringEngine` is reused for
its primitives (attribute match, stay fit, multi-city curve) rather than
duplicated, and is still computed alongside for diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import PlannerConfig
from ..models.itinerary import TravelValueBreakdown
from ..models.search import SearchState
from ..models.transport import TransportType
from ..models.trip import TripRequest
from ..profiles import RecommendationProfile
from ..providers.destinations import DestinationProvider
from .scoring import ScoringEngine, clamp01

#: Days-per-city below which an itinerary reads as rushed regardless of price.
MIN_DAYS_PER_CITY = 0.75


@dataclass(frozen=True, slots=True)
class TimeProfile:
    """How a trip's elapsed time actually broke down."""

    elapsed_minutes: int
    transport_minutes: int
    """Intercity transport plus ground transfers."""
    usable_destination_minutes: int
    max_usable_minutes: int

    @property
    def usable_ratio(self) -> float:
        if self.max_usable_minutes <= 0:
            return 0.0
        return clamp01(self.usable_destination_minutes / self.max_usable_minutes)

    @property
    def transport_fraction(self) -> float:
        if self.elapsed_minutes <= 0:
            return 1.0
        return self.transport_minutes / self.elapsed_minutes


class TravelValueScorer:
    """Scores a state as a complete travel experience under one profile."""

    def __init__(
        self,
        config: PlannerConfig,
        destinations: DestinationProvider,
        *,
        base: ScoringEngine | None = None,
    ) -> None:
        self.config = config
        self.destinations = destinations
        self.base = base or ScoringEngine(config, destinations)

    # ------------------------------------------------------------------
    # Cost
    # ------------------------------------------------------------------
    def cost_score(
        self,
        total_cost: float,
        request: TripRequest,
        profile: RecommendationProfile,
    ) -> float:
        """Blends "money left over" with "money sensibly used".

        Pure efficiency (``1 - cost/budget``) makes the planner hoard the user's
        money, which is not what a traveler wants. The utilization term is
        *saturating*: spending up to ``budget_utilization_target`` is close to
        free, and only beyond it does extra spending really cost score. A
        profile with ``budget_utilization_weight = 0`` gets pure efficiency,
        which is what CHEAPEST needs to stay strictly monotone.
        """
        if request.budget <= 0:
            return 0.0
        utilization = total_cost / request.budget
        efficiency = clamp01(1.0 - utilization)
        sensible_use = clamp01(utilization / self.config.budget_utilization_target)
        weight = profile.budget_utilization_weight
        return clamp01((1.0 - weight) * efficiency + weight * sensible_use)

    # ------------------------------------------------------------------
    # Experience
    # ------------------------------------------------------------------
    def destination_quality(self, state: SearchState) -> float:
        """Intrinsic appeal of the cities, independent of the user's taste.

        Taste is PreferenceScore's job; this keeps the two from double-counting.
        """
        if not state.cities:
            return 0.0
        scores = []
        for city in state.cities:
            destination = self.destinations.get(city)
            if destination is None:
                scores.append(0.5)
                continue
            vector = destination.attribute_vector()
            scores.append(sum(vector.values()) / len(vector))
        return clamp01(sum(scores) / len(scores))

    def stay_quality(self, state: SearchState) -> float:
        """How well each stay matches what the city is worth, in *usable* time.

        A 15-hour stop in London scores far below two real days there, because
        the stay is measured against the city's recommended range in usable
        days rather than in calendar nights.
        """
        if not state.stays:
            return 0.0
        day = max(self.config.usable_day_minutes, 1)
        scores = []
        for stay in state.stays:
            usable_days = stay.usable_minutes / day
            destination = self.destinations.get(stay.city)
            if destination is None:
                scores.append(0.5)
                continue
            if usable_days >= destination.recommended_min_days:
                # Being at or beyond the recommended minimum is good; the V1
                # stay_fit curve handles overstaying.
                scores.append(self.base.stay_fit(stay.city, round(usable_days)))
            else:
                scores.append(
                    clamp01(usable_days / max(destination.recommended_min_days, 1e-9))
                )
        return clamp01(sum(scores) / len(scores))

    def pace_quality(self, state: SearchState) -> float:
        """Penalizes cramming cities into days, however cheap the tickets are."""
        if not state.cities:
            return 0.0
        elapsed_days = max(state.trip_span_minutes, 1) / (24 * 60)
        days_per_city = elapsed_days / len(state.cities)
        comfortable = self.config.comfortable_days_per_city
        if days_per_city >= comfortable:
            return 1.0
        span = max(comfortable - MIN_DAYS_PER_CITY, 1e-9)
        return clamp01((days_per_city - MIN_DAYS_PER_CITY) / span)

    def experience_score(self, state: SearchState) -> float:
        """Destination quality, stay quality and pace, equally weighted."""
        if not state.cities:
            return 0.0
        return clamp01(
            (
                self.destination_quality(state)
                + self.stay_quality(state)
                + self.pace_quality(state)
            )
            / 3.0
        )

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------
    def preference_score(self, state: SearchState, request: TripRequest) -> float:
        """Taste match plus appetite for multiple cities, plus wish-list coverage."""
        taste = self.base.preference_score(state, request)
        wishes = self.base.destination_score(state, request)
        return clamp01(0.65 * taste + 0.35 * wishes)

    # ------------------------------------------------------------------
    # Time
    # ------------------------------------------------------------------
    def time_profile(self, state: SearchState, request: TripRequest) -> TimeProfile:
        return TimeProfile(
            elapsed_minutes=state.trip_span_minutes,
            transport_minutes=state.total_transport_minutes,
            usable_destination_minutes=state.usable_destination_minutes,
            max_usable_minutes=request.duration_days * self.config.usable_day_minutes,
        )

    def time_score(
        self, state: SearchState, request: TripRequest, *, optimistic: bool = False
    ) -> float:
        """Rewards days that can actually be spent somewhere.

        ``optimistic`` is used for ranking partial states: a half-built trip has
        not accumulated its usable time yet but still could, and charging it for
        that would make the beam prefer trips that are nearly over.
        """
        profile = self.time_profile(state, request)
        transit_quality = clamp01(
            1.0 - profile.transport_fraction / self.config.max_travel_time_fraction
        )
        usable = 1.0 if optimistic else profile.usable_ratio
        return clamp01(0.6 * usable + 0.4 * transit_quality)

    # ------------------------------------------------------------------
    # Diversity
    # ------------------------------------------------------------------
    def diversity_score(self, state: SearchState) -> float:
        """Variety within the trip: countries, modes, and how many places.

        The city-count term uses the saturating ``1 - 1/n`` curve, so ADVENTURE
        is pulled towards multi-city trips without "more is always better".
        """
        if not state.cities or not state.route:
            return 0.0
        countries = set()
        for city in state.cities:
            destination = self.destinations.get(city)
            countries.add(destination.country if destination else city)
        country_variety = len(countries) / len(state.cities)
        modes = {leg.transport_type for leg in state.route}
        mode_variety = len(modes) / min(state.leg_count, len(TransportType))
        places = self.base.multi_city_component(len(state.cities))
        return clamp01((country_variety + mode_variety + places) / 3.0)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------
    def components(
        self,
        state: SearchState,
        request: TripRequest,
        profile: RecommendationProfile,
        *,
        optimistic: bool = False,
    ) -> dict[str, float]:
        return {
            "cost": self.cost_score(state.total_cost, request, profile),
            "experience": self.experience_score(state),
            "preferences": self.preference_score(state, request),
            "time": self.time_score(state, request, optimistic=optimistic),
            "diversity": self.diversity_score(state),
        }

    def weighted_total(
        self, components: dict[str, float], profile: RecommendationProfile
    ) -> float:
        weights = profile.weights.normalized()
        return sum(weights[name] * value for name, value in components.items())

    def score(
        self,
        state: SearchState,
        request: TripRequest,
        profile: RecommendationProfile,
    ) -> TravelValueBreakdown:
        """The full, explainable Travel Value of a completed itinerary."""
        components = self.components(state, request, profile)
        timing = self.time_profile(state, request)
        return TravelValueBreakdown(
            profile=profile.name,
            **components,
            total=round(self.weighted_total(components, profile), 6),
            weights=profile.weights.normalized(),
            budget_utilization=round(
                state.total_cost / request.budget if request.budget else 0.0, 4
            ),
            usable_destination_minutes=timing.usable_destination_minutes,
            transport_minutes=timing.transport_minutes,
            usable_ratio=round(timing.usable_ratio, 4),
        )

    def total(
        self,
        state: SearchState,
        request: TripRequest,
        profile: RecommendationProfile,
        *,
        optimistic: bool = False,
    ) -> float:
        """Fast path for beam ranking: the weighted total only, no breakdown."""
        return self.weighted_total(
            self.components(state, request, profile, optimistic=optimistic), profile
        )
