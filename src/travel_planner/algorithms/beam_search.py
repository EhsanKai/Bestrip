"""Beam search over complete itineraries.

The search explores a *state space of whole trips*, not a sequence of cheapest
next hops. Two properties make it non-greedy:

1. Partial states are ranked by an **optimistic completion estimate** - the
   state's cost and travel time plus the cheapest possible way home from where
   it currently is. A cheap first leg into a city with an expensive return is
   therefore ranked on the expensive round trip it implies.
2. The beam keeps ``beam_width`` alternatives at every depth, so several
   different first moves stay alive and are judged on their finished trips.

The algorithm is fully deterministic: candidate generation iterates sorted
collections, and ranking breaks ties on cost and then on the state's leg-id
signature.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Iterable, Sequence

from ..config import PlannerConfig
from ..constraints.validator import ConstraintValidator, ReturnEstimator
from ..models.debug import IterationDebug, PrunedState, RejectedState, SearchDebug
from ..models.search import SearchState
from ..models.transport import TransportOption
from ..models.trip import TripRequest
from ..providers.destinations import DestinationProvider
from ..providers.transport import TransportDataProvider
from .scoring import ScoringEngine


def _route_labels(state: SearchState) -> list[str]:
    """Node sequence of a state, e.g. ``["DUS", "Prague", "Vienna", "DUS"]``."""
    if not state.route:
        return [state.origin_airport]
    return [state.route[0].origin] + [leg.destination for leg in state.route]


class BeamSearchOptimizer:
    """Explores round trips and returns every completed, feasible itinerary."""

    def __init__(
        self,
        config: PlannerConfig,
        *,
        transport_provider: TransportDataProvider,
        destination_provider: DestinationProvider,
        validator: ConstraintValidator,
        scoring: ScoringEngine,
        return_estimator: ReturnEstimator | None = None,
    ) -> None:
        self.config = config
        self.transport = transport_provider
        self.destinations = destination_provider
        self.validator = validator
        self.scoring = scoring
        self.return_estimator = return_estimator

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def search(
        self,
        request: TripRequest,
        *,
        origin_airports: Sequence[str],
        start_dates: Sequence[date],
        debug: SearchDebug | None = None,
    ) -> list[SearchState]:
        """Run beam search and return all completed states found, best first."""
        beam = self._initial_states(origin_airports, start_dates)
        if debug is not None:
            debug.origin_airports = list(origin_airports)
            debug.start_dates = [d.isoformat() for d in start_dates]
            debug.initial_states = len(beam)

        completed: list[SearchState] = []
        # One iteration adds one leg. A trip visiting ``max_cities`` cities
        # needs ``max_cities + 1`` legs including the return.
        for iteration in range(1, self.config.max_cities + 2):
            if not beam:
                break
            trace = IterationDebug(
                iteration=iteration,
                states_in=len(beam),
                beam_width=self.config.beam_width,
            )
            survivors, finished = self._expand_beam(beam, request, trace)
            completed.extend(finished)
            trace.completed_found = len(finished)
            trace.surviving = len(survivors)

            kept, pruned = self._prune(
                survivors, self.validator.resolve(request).must_visit
            )
            trace.kept = len(kept)
            trace.pruned_by_beam = len(pruned)
            trace.kept_routes = [" -> ".join(_route_labels(s)) for s in kept]
            if debug is not None:
                trace.pruned_examples = [
                    PrunedState(
                        iteration=iteration,
                        route=_route_labels(state),
                        estimated_score=round(state.score, 6),
                    )
                    for state in pruned[: self.config.debug_example_limit]
                ]
                debug.iterations.append(trace)
            beam = kept

        completed.sort(key=self._rank_key)
        if debug is not None:
            debug.completed_itineraries = len(completed)
        return completed

    # ------------------------------------------------------------------
    # Initial states
    # ------------------------------------------------------------------
    def _initial_states(
        self, origin_airports: Sequence[str], start_dates: Sequence[date]
    ) -> list[SearchState]:
        states: list[SearchState] = []
        for airport in origin_airports:
            for start_date in start_dates:
                moment = datetime.combine(start_date, time.min)
                states.append(
                    SearchState(
                        origin_airport=airport,
                        current_location=airport,
                        start_datetime=moment,
                        current_datetime=moment,
                    )
                )
        return states

    # ------------------------------------------------------------------
    # Expansion
    # ------------------------------------------------------------------
    def _departure_dates(self, state: SearchState, request: TripRequest) -> list[date]:
        """Dates the traveler may leave their current location on.

        For the first leg that is simply the trip's start date. Afterwards each
        configured stay length yields a different departure date - which is how
        "London for 1 day" and "London for 3 days" become distinct states rather
        than one hard-coded assumption.
        """
        if state.is_at_start:
            return [state.start_datetime.date()]
        arrival = state.current_datetime.date()
        dates = []
        for stay in self.config.stay_day_options:
            candidate = arrival + timedelta(days=stay)
            if candidate <= request.date_to:
                dates.append(candidate)
        return dates

    def _candidate_actions(
        self, state: SearchState, request: TripRequest
    ) -> Iterable[tuple[TransportOption, bool, int | None]]:
        """Yield ``(leg, is_return, stay_days)`` triples for every legal move.

        Action A ("continue") moves on to another destination city; action B
        ("return") flies home to one of the origin airports. Both are generated
        for every candidate stay length.
        """
        at_start = state.is_at_start
        may_continue = state.city_count < self.config.max_cities
        # Going home is only an option once every mandatory destination has
        # been seen - otherwise the itinerary could not be completed anyway.
        mandatory = self.validator.resolve(request).must_visit
        may_return = bool(state.cities) and not (mandatory - state.visited_cities)

        for departure_date in self._departure_dates(state, request):
            stay = (
                None
                if at_start
                else (departure_date - state.current_datetime.date()).days
            )
            if may_continue:
                for destination in self.destinations.all():
                    if destination.id == state.current_location:
                        continue
                    if destination.id in state.visited_cities:
                        continue
                    for option in self.transport.search(
                        state.current_location, destination.id, departure_date
                    ):
                        if option.departure < state.current_datetime:
                            continue
                        yield option, False, stay
            if may_return:
                for airport in sorted(self.validator.origin_airports):
                    for option in self.transport.search(
                        state.current_location, airport, departure_date
                    ):
                        if option.departure < state.current_datetime:
                            continue
                        yield option, True, stay

    def _expand_beam(
        self,
        beam: Sequence[SearchState],
        request: TripRequest,
        trace: IterationDebug,
    ) -> tuple[list[SearchState], list[SearchState]]:
        survivors: list[SearchState] = []
        finished: list[SearchState] = []

        for state in beam:
            for option, is_return, stay in self._candidate_actions(state, request):
                trace.generated += 1
                candidate = state.extend(
                    option,
                    travelers=request.travelers,
                    stay_days=stay,
                    is_return=is_return,
                )
                result = self.validator.validate(candidate, request)
                if not result.valid:
                    trace.rejected += 1
                    assert result.reason is not None
                    trace.rejection_counts[result.reason] = (
                        trace.rejection_counts.get(result.reason, 0) + 1
                    )
                    if len(trace.rejected_examples) < self.config.debug_example_limit:
                        trace.rejected_examples.append(
                            RejectedState(
                                iteration=trace.iteration,
                                route=_route_labels(candidate),
                                reason=result.reason,
                                detail=result.detail,
                            )
                        )
                    continue

                if candidate.completed:
                    finished.append(
                        candidate.with_score(
                            self.scoring.weighted_total(
                                self.scoring.components(candidate, request)
                            )
                        )
                    )
                else:
                    survivors.append(
                        candidate.with_score(self._estimate(candidate, request))
                    )

        return survivors, finished

    def _estimate(self, state: SearchState, request: TripRequest) -> float:
        price = minutes = None
        if self.return_estimator is not None:
            price = self.return_estimator.min_return_price_per_person(
                state.current_location
            )
            minutes = self.return_estimator.min_return_minutes(state.current_location)
        return self.scoring.estimate_total(
            state,
            request,
            return_price_per_person=price,
            return_minutes=minutes,
        )

    # ------------------------------------------------------------------
    # Beam pruning
    # ------------------------------------------------------------------
    @staticmethod
    def _rank_key(state: SearchState) -> tuple:
        """Deterministic ordering: best score, then cheapest, then stable id."""
        return (-round(state.score, 9), state.total_cost, state.signature())

    @staticmethod
    def _beam_rank_key(state: SearchState, mandatory: frozenset[str]) -> tuple:
        """Beam ordering: progress towards mandatory destinations comes first.

        A must-visit destination is a hard requirement, not a preference, so a
        state that has already been there is strictly closer to a usable answer
        than an otherwise better-looking one that has not. Without this the beam
        fills with attractive routes that are all doomed at completion time.
        """
        if not mandatory:
            return (0.0, *BeamSearchOptimizer._rank_key(state))
        progress = len(mandatory & state.visited_cities) / len(mandatory)
        return (-progress, *BeamSearchOptimizer._rank_key(state))

    def _prune(
        self, survivors: list[SearchState], mandatory: frozenset[str] = frozenset()
    ) -> tuple[list[SearchState], list[SearchState]]:
        """Keep the top ``beam_width`` states, spread across distinct routes.

        Returns ``(kept, pruned)`` so the debug trace can report exactly which
        states lost the beam competition and with what estimate.
        """
        survivors.sort(key=lambda state: self._beam_rank_key(state, mandatory))
        per_route: defaultdict[tuple[str, ...], int] = defaultdict(int)
        kept: list[SearchState] = []
        overflow: list[SearchState] = []
        for index, state in enumerate(survivors):
            if len(kept) == self.config.beam_width:
                overflow.extend(survivors[index:])
                break
            if per_route[state.cities] < self.config.beam_slots_per_route:
                per_route[state.cities] += 1
                kept.append(state)
            else:
                overflow.append(state)
        # If the per-route cap left the beam under-filled, top it up in rank
        # order so the configured beam width is always honoured.
        if len(kept) < self.config.beam_width and overflow:
            promoted = overflow[: self.config.beam_width - len(kept)]
            overflow = overflow[len(promoted) :]
            kept.extend(promoted)
            kept.sort(key=lambda state: self._beam_rank_key(state, mandatory))
        return kept, overflow
