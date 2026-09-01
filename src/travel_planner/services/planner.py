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
from ..config import PlannerConfig
from ..constraints.validator import ConstraintValidator
from ..models.debug import FilteredItinerary, FilterStage, SearchDebug
from ..models.itinerary import (
    Itinerary,
    PlannerMetadata,
    PlanResult,
)
from ..models.search import SearchState
from ..models.trip import TripRequest
from ..providers.destinations import DestinationProvider, StaticDestinationProvider
from ..providers.transport import SyntheticTransportDataProvider, TransportDataProvider
from .baseline import BaselinePlanner, compare_to_baseline
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
    ) -> None:
        self.config = config or PlannerConfig()
        self.transport = transport_provider or SyntheticTransportDataProvider()
        self.destinations = destination_provider or StaticDestinationProvider()
        self.origin_resolver = origin_resolver or StaticOriginResolver()
        self.scoring = ScoringEngine(self.config, self.destinations)
        self.baseline_planner = BaselinePlanner(
            self.config,
            transport_provider=self.transport,
            destination_provider=self.destinations,
        )

    # ------------------------------------------------------------------
    def plan(self, request: TripRequest, *, debug: bool = False) -> PlanResult:
        """Plan a trip and return ranked, diverse itineraries."""
        started = _time.perf_counter()
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

        window_dates = self._window_dates(request)
        return_estimator = CachedReturnEstimator(
            self.transport,
            origin_airports=origin_airports,
            dates=window_dates,
            allowed_transport_types=[t.value for t in request.transport_preferences],
        )
        validator = ConstraintValidator(
            self.config,
            origin_airports=origin_airports,
            destination_ids=[d.id for d in self.destinations.all()],
            return_estimator=return_estimator,
        )
        optimizer = BeamSearchOptimizer(
            self.config,
            transport_provider=self.transport,
            destination_provider=self.destinations,
            validator=validator,
            scoring=self.scoring,
            return_estimator=return_estimator,
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

        selected, pareto_kept = self._post_process(completed, request, trace)

        recommendations: list[Itinerary] = []
        for rank, state in enumerate(selected, start=1):
            itinerary = self._to_itinerary(state, request, rank)
            itinerary.baseline_comparison = compare_to_baseline(itinerary, baseline)
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

        diversity = diversify(
            candidates,
            lambda state: state.city_signature(),
            limit=self.config.max_results,
            similarity_threshold=self.config.diversity_similarity_threshold,
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
        self, state: SearchState, request: TripRequest, rank: int
    ) -> Itinerary:
        breakdown = self.scoring.score(state, request)
        departure = state.route[0].departure
        arrival = state.route[-1].arrival
        elapsed_minutes = int((arrival - departure).total_seconds() // 60)
        return Itinerary(
            rank=rank,
            score=round(breakdown.total, 6),
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
            score_breakdown=breakdown,
        )

    @staticmethod
    def _window_dates(request: TripRequest) -> list[date]:
        """Every calendar date inside the requested window."""
        dates: list[date] = []
        day = request.date_from
        while day <= request.date_to:
            dates.append(day)
            day += timedelta(days=1)
        return dates
