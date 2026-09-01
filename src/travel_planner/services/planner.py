"""The planner service - the entry point of the domain layer.

Usable entirely without FastAPI::

    planner = TravelPlanner()
    result = planner.plan(request)

The API package is only an adapter around this class.
"""

from __future__ import annotations

import time as _time
from datetime import date, timedelta
from typing import Sequence

from ..algorithms.beam_search import BeamSearchOptimizer
from ..algorithms.diversity import diversify
from ..algorithms.pareto import pareto_filter
from ..algorithms.scoring import ScoringEngine
from ..algorithms.travel_value import TravelValueScorer
from ..config import PlannerConfig
from ..constraints.validator import ConstraintValidator
from ..models.debug import FilteredItinerary, FilterStage, SearchDebug
from ..models.itinerary import (
    CostBreakdown,
    Itinerary,
    PlannerMetadata,
    PlanResult,
    StaySummary,
)
from ..models.search import SearchState
from ..models.transfer import GroundTransferOption
from ..models.trip import TripRequest
from ..profiles import ProfileName, RecommendationProfile, get_profile
from ..providers.accommodation import (
    AccommodationDataProvider,
    NoAccommodationProvider,
    SyntheticAccommodationDataProvider,
)
from ..providers.destinations import DestinationProvider, StaticDestinationProvider
from ..providers.ground_transfer import (
    FreeGroundTransferProvider,
    GroundTransferProvider,
    SyntheticGroundTransferProvider,
)
from ..providers.transport import SyntheticTransportDataProvider, TransportDataProvider
from .accommodation_estimator import (
    CachedAccommodationEstimator,
    ZeroAccommodationEstimator,
)
from .baseline import BaselinePlanner, compare_to_baseline
from .explanation import explanation_factors
from .origin_resolver import OriginResolver, StaticOriginResolver
from .return_estimator import CachedReturnEstimator

#: Cap on how many filtered-out itineraries the trace records per stage.
MAX_FILTER_RECORDS = 50


class TravelPlanner:
    """Wires the providers, constraints, search, scoring and filters together."""

    def __init__(
        self,
        transport_provider: TransportDataProvider | None = None,
        destination_provider: DestinationProvider | None = None,
        *,
        config: PlannerConfig | None = None,
        origin_resolver: OriginResolver | None = None,
        accommodation_provider: AccommodationDataProvider | None = None,
        ground_transfer_provider: GroundTransferProvider | None = None,
    ) -> None:
        self.config = config or PlannerConfig()
        self.transport = transport_provider or SyntheticTransportDataProvider()
        self.destinations = destination_provider or StaticDestinationProvider()
        self.origin_resolver = origin_resolver or StaticOriginResolver()

        # V2 providers. Disabling a feature swaps in an explicit null provider
        # rather than sprinkling ``if enabled`` through the algorithms.
        if accommodation_provider is not None:
            self.accommodation = accommodation_provider
        elif self.config.enable_accommodation:
            self.accommodation = SyntheticAccommodationDataProvider()
        else:
            self.accommodation = NoAccommodationProvider()

        if ground_transfer_provider is not None:
            self.ground_transfer = ground_transfer_provider
        elif self.config.enable_ground_transfer:
            self.ground_transfer = SyntheticGroundTransferProvider()
        else:
            self.ground_transfer = FreeGroundTransferProvider()

        self.scoring = ScoringEngine(self.config, self.destinations)
        self.travel_value = TravelValueScorer(
            self.config, self.destinations, base=self.scoring
        )
        self.accommodation_estimator = (
            CachedAccommodationEstimator(self.accommodation)
            if self.config.enable_accommodation
            else ZeroAccommodationEstimator()
        )
        self.baseline_planner = BaselinePlanner(
            self.config,
            transport_provider=self.transport,
            destination_provider=self.destinations,
            accommodation_provider=self.accommodation,
            ground_transfer_provider=self.ground_transfer,
        )

    # ------------------------------------------------------------------
    def plan(
        self,
        request: TripRequest,
        *,
        debug: bool = False,
        profile: ProfileName | str | None = None,
    ) -> PlanResult:
        """Plan a trip and return ranked, diverse itineraries.

        ``profile`` overrides ``request.profile``, which itself overrides
        ``config.profile`` (BEST_VALUE by default).
        """
        started = _time.perf_counter()
        active = get_profile(profile or request.profile or self.config.profile)
        # The trace is always collected: it is small, bounded by
        # ``debug_example_limit``, and it feeds the run counters in the
        # metadata. Only its exposure in the result is gated by ``debug``.
        trace = SearchDebug()

        candidates = self.origin_resolver.resolve(request.origin, self.config)
        origin_airports = [candidate.code for candidate in candidates]
        start_dates = request.candidate_start_dates()

        warnings: list[str] = []
        if not start_dates:
            warnings.append(
                f"no start date fits a {request.duration_days}-day trip inside "
                f"{request.date_from}..{request.date_to}"
            )

        transfers = self._ground_transfers(request.origin, origin_airports)
        cheapest_transfer = min(
            (t.price_per_person for t in transfers.values()), default=0.0
        )

        window_dates = self._window_dates(request)
        return_estimator = CachedReturnEstimator(
            self.transport,
            origin_airports=origin_airports,
            dates=window_dates,
            allowed_transport_types=[t.value for t in request.transport_preferences],
            min_return_transfer_price_per_person=cheapest_transfer,
        )
        validator = ConstraintValidator(
            self.config,
            origin_airports=origin_airports,
            destination_ids=[d.id for d in self.destinations.all()],
            return_estimator=return_estimator,
            accommodation_estimator=self.accommodation_estimator,
        )
        optimizer = BeamSearchOptimizer(
            self.config,
            transport_provider=self.transport,
            destination_provider=self.destinations,
            validator=validator,
            scoring=self.scoring,
            return_estimator=return_estimator,
            travel_value=self.travel_value,
            profile=active,
            accommodation_provider=self.accommodation,
            accommodation_estimator=self.accommodation_estimator,
            ground_transfers=transfers,
        )

        completed = optimizer.search(
            request,
            origin_airports=origin_airports,
            start_dates=start_dates,
            debug=trace,
        )

        baseline = self.baseline_planner.compute(
            request, origin_airports=origin_airports, start_dates=start_dates
        )
        if request.preferred_destinations and baseline is None:
            warnings.append(
                "no baseline round trip to "
                f"{request.preferred_destinations[0]!r} fits the budget, window and duration"
            )

        selected, pareto_kept = self._post_process(completed, request, trace, active)

        recommendations: list[Itinerary] = []
        for rank, state in enumerate(selected, start=1):
            itinerary = self._to_itinerary(state, request, rank, active)
            itinerary.baseline_comparison = compare_to_baseline(itinerary, baseline)
            itinerary.explanation_factors = explanation_factors(
                itinerary, request, self.config, baseline
            )
            recommendations.append(itinerary)

        trace.scored_itineraries = [
            {
                "route": itinerary.route_label(),
                "score": itinerary.score,
                "cost": itinerary.total_cost,
                "components": itinerary.score_breakdown.model_dump(exclude={"weights"})
                if itinerary.score_breakdown
                else {},
            }
            for itinerary in recommendations
        ]
        trace.notes.extend(warnings)

        metadata = PlannerMetadata(
            origin=request.origin,
            profile=active.name,
            origin_airports=origin_airports,
            start_dates=[d.isoformat() for d in start_dates],
            beam_width=self.config.beam_width,
            max_cities=self.config.max_cities,
            states_generated=trace.total_generated,
            states_rejected=trace.total_rejected,
            completed_itineraries=len(completed),
            pareto_kept=pareto_kept,
            diversity_kept=len(selected),
            returned=len(recommendations),
            elapsed_seconds=round(_time.perf_counter() - started, 4),
            currency=request.currency,
            warnings=warnings,
        )
        return PlanResult(
            profile=active.name,
            baseline=baseline,
            recommendations=recommendations,
            metadata=metadata,
            debug=trace if debug else None,
        )

    # ------------------------------------------------------------------
    # Post-processing: Pareto -> diversity -> top N
    # ------------------------------------------------------------------
    def _post_process(
        self,
        completed: Sequence[SearchState],
        request: TripRequest,
        trace: SearchDebug,
        profile: RecommendationProfile,
    ) -> tuple[list[SearchState], int]:
        candidates = list(completed)
        trace.pareto_input = len(candidates)

        if self.config.enable_pareto and candidates:
            result = pareto_filter(
                candidates, lambda state: self.scoring.objectives(state, request)
            )
            for removed, dominator in result.dominated[:MAX_FILTER_RECORDS]:
                trace.filtered.append(
                    FilteredItinerary(
                        route=self._labels(removed),
                        stage=FilterStage.PARETO,
                        reason="dominated on cost, travel time, cities and preference",
                        dominated_by=self._labels(dominator),
                    )
                )
            candidates = result.frontier
        pareto_kept = len(candidates)
        trace.pareto_kept = pareto_kept
        trace.diversity_input = pareto_kept

        candidates.sort(key=BeamSearchOptimizer._rank_key)

        if not self.config.enable_diversity:
            selected = candidates[: self.config.max_results]
            trace.diversity_kept = len(selected)
            return selected, pareto_kept

        threshold = (
            profile.diversity_similarity_threshold
            if profile.diversity_similarity_threshold is not None
            else self.config.diversity_similarity_threshold
        )
        diversity = diversify(
            candidates,
            lambda state: state.city_signature(),
            limit=self.config.max_results,
            similarity_threshold=threshold,
        )
        trace.diversity_kept = len(diversity.selected)
        for rejection in diversity.rejected[:MAX_FILTER_RECORDS]:
            trace.filtered.append(
                FilteredItinerary(
                    route=self._labels(rejection.item),
                    stage=FilterStage.DIVERSITY,
                    reason="too similar to a better-ranked itinerary",
                    dominated_by=self._labels(rejection.similar_to),
                    similarity=rejection.similarity,
                )
            )
        return diversity.selected, pareto_kept

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _labels(state: SearchState) -> list[str]:
        if not state.route:
            return [state.origin_airport]
        return [state.route[0].origin] + [leg.destination for leg in state.route]

    def _to_itinerary(
        self,
        state: SearchState,
        request: TripRequest,
        rank: int,
        profile: RecommendationProfile,
    ) -> Itinerary:
        value = self.travel_value.score(state, request, profile)
        breakdown = self.scoring.score(state, request)
        departure = state.route[0].departure
        arrival = state.route[-1].arrival
        elapsed_minutes = int((arrival - departure).total_seconds() // 60)
        return Itinerary(
            rank=rank,
            score=round(value.total, 6),
            total_cost=state.total_cost,
            currency=request.currency,
            duration_days=round(elapsed_minutes / (24 * 60), 2),
            origin_airport=state.route[0].origin,
            return_airport=state.current_location,
            cities=list(state.cities),
            stay_days=list(state.stay_days),
            legs=list(state.route),
            total_travel_minutes=state.total_travel_minutes,
            departure=departure,
            arrival=arrival,
            cost_breakdown=CostBreakdown(
                transport=state.transport_cost,
                accommodation=state.accommodation_cost,
                ground_transfer=state.ground_transfer_cost,
            ),
            stays=[
                StaySummary(
                    city=stay.city,
                    arrival=stay.arrival,
                    departure=stay.departure,
                    nights=stay.nights,
                    accommodation_cost=stay.accommodation_cost,
                    accommodation_name=(
                        stay.accommodation.name if stay.accommodation else None
                    ),
                    accommodation_tier=(
                        stay.accommodation.tier.value if stay.accommodation else None
                    ),
                    usable_minutes=stay.usable_minutes,
                )
                for stay in state.stays
            ],
            ground_transfer_minutes=state.ground_transfer_minutes,
            usable_destination_minutes=state.usable_destination_minutes,
            profile=profile.name,
            value_breakdown=value,
            score_breakdown=breakdown,
        )

    def _ground_transfers(
        self, origin: str, airports: Sequence[str]
    ) -> dict[str, GroundTransferOption]:
        """Cheapest way to reach each candidate departure airport from home."""
        transfers: dict[str, GroundTransferOption] = {}
        for airport in airports:
            options = self.ground_transfer.search(origin, airport)
            if options:
                transfers[airport] = options[0]
        return transfers

    @staticmethod
    def _window_dates(request: TripRequest) -> list[date]:
        """Every calendar date inside the requested window."""
        dates: list[date] = []
        day = request.date_from
        while day <= request.date_to:
            dates.append(day)
            day += timedelta(days=1)
        return dates
