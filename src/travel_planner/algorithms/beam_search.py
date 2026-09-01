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
from ..constraints.validator import (
    AccommodationEstimator,
    ConstraintValidator,
    ReturnEstimator,
)
from ..models.accommodation import AccommodationOption
from ..models.debug import (
    IterationDebug,
    PrunedState,
    RejectedState,
    RejectionReason,
    SearchDebug,
)
from ..models.search import SearchState
from ..models.transfer import GroundTransferOption
from ..models.transport import TransportOption
from ..models.trip import TripRequest
from ..profiles import RecommendationProfile, get_profile
from ..providers.accommodation import AccommodationDataProvider
from ..providers.destinations import DestinationProvider
from ..providers.transport import TransportDataProvider
from ..usable_time import usable_minutes
from .scoring import ScoringEngine
from .travel_value import TravelValueScorer


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
        travel_value: TravelValueScorer | None = None,
        profile: RecommendationProfile | None = None,
        accommodation_provider: AccommodationDataProvider | None = None,
        accommodation_estimator: AccommodationEstimator | None = None,
        ground_transfers: dict[str, GroundTransferOption] | None = None,
    ) -> None:
        self.config = config
        self.transport = transport_provider
        self.destinations = destination_provider
        self.validator = validator
        self.scoring = scoring
        self.return_estimator = return_estimator
        # ``None`` means this optimizer prices stays at zero, whatever the
        # config flag says. Constructing an optimizer without a provider must
        # behave like V1 rather than silently generating no candidates at all.
        self.accommodation = accommodation_provider
        self.accommodation_estimator = accommodation_estimator
        self.ground_transfers = ground_transfers or {}
        # Ranking uses Travel Value under the active profile; the V1 engine is
        # still available for diagnostics and for the objectives vector.
        self.travel_value = travel_value or TravelValueScorer(
            config, destination_provider, base=scoring
        )
        self.profile = profile or get_profile(config.profile)

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
        beam = self._initial_states(origin_airports, start_dates, request.travelers)
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
        self,
        origin_airports: Sequence[str],
        start_dates: Sequence[date],
        travelers: int,
    ) -> list[SearchState]:
        """One state per (departure airport, start date), pre-charged for the
        journey from the user's front door to that airport."""
        states: list[SearchState] = []
        for airport in origin_airports:
            transfer = self.ground_transfers.get(airport)
            for start_date in start_dates:
                moment = datetime.combine(start_date, time.min)
                states.append(
                    SearchState(
                        origin_airport=airport,
                        current_location=airport,
                        start_datetime=moment,
                        current_datetime=moment,
                    ).with_outbound_transfer(transfer, travelers=travelers)
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

    def _book_accommodation(
        self, state: SearchState, leg: TransportOption, travelers: int
    ) -> list[AccommodationOption | None] | None:
        """Rooms the traveler could take for the stay this leg ends.

        Returns ``[None]`` when there is nothing to book - accommodation is off,
        the traveler is still at home, or no night is actually spent - so the
        caller always has exactly one branch in the V1-equivalent configuration.
        ``None`` means the stay is *required* but nothing is available, which is
        a real rejection rather than "no cost".
        """
        if (
            not state.cities
            or not self.config.enable_accommodation
            or self.accommodation is None
        ):
            return [None]
        check_in = state.current_datetime.date()
        check_out = leg.departure.date()
        if check_out <= check_in:
            # Same-day hop: no night is spent, so nothing to book.
            return [None]
        options = self.accommodation.search(
            state.current_location, check_in, check_out, travelers
        )
        if not options:
            return None
        return list(options[: self.config.accommodation_options_per_stay])

    def _candidate_actions(
        self, state: SearchState, request: TripRequest
    ) -> Iterable[tuple[TransportOption, bool, AccommodationOption | None, bool]]:
        """Yield ``(leg, is_return, accommodation, bookable)`` for every legal move.

        Action A ("continue") moves on to another destination city; action B
        ("return") flies home to one of the origin airports. Both are generated
        for every candidate stay length, and each is paired with the rooms that
        stay would require - so "London for one night in a cheap room" and
        "London for three nights" are genuinely different states.
        """
        may_continue = state.city_count < self.config.max_cities
        # Going home is only an option once every mandatory destination has
        # been seen - otherwise the itinerary could not be completed anyway.
        mandatory = self.validator.resolve(request).must_visit
        may_return = bool(state.cities) and not (mandatory - state.visited_cities)

        for departure_date in self._departure_dates(state, request):
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
                        rooms = self._book_accommodation(
                            state, option, request.travelers
                        )
                        if rooms is None:
                            yield option, False, None, False
                            continue
                        for room in rooms:
                            yield option, False, room, True
            if may_return:
                for airport in sorted(self.validator.origin_airports):
                    for option in self.transport.search(
                        state.current_location, airport, departure_date
                    ):
                        if option.departure < state.current_datetime:
                            continue
                        rooms = self._book_accommodation(
                            state, option, request.travelers
                        )
                        if rooms is None:
                            yield option, True, None, False
                            continue
                        for room in rooms:
                            yield option, True, room, True

    def _expand_beam(
        self,
        beam: Sequence[SearchState],
        request: TripRequest,
        trace: IterationDebug,
    ) -> tuple[list[SearchState], list[SearchState]]:
        survivors: list[SearchState] = []
        finished: list[SearchState] = []

        for state in beam:
            for option, is_return, room, bookable in self._candidate_actions(
                state, request
            ):
                trace.generated += 1
                if not bookable:
                    trace.rejected += 1
                    reason = RejectionReason.NO_ACCOMMODATION_AVAILABLE
                    trace.rejection_counts[reason] = (
                        trace.rejection_counts.get(reason, 0) + 1
                    )
                    continue
                candidate = state.extend(
                    option,
                    travelers=request.travelers,
                    is_return=is_return,
                    accommodation=room,
                    usable_minutes=self._usable_minutes(state, option),
                    return_transfer=(
                        self.ground_transfers.get(option.destination)
                        if is_return
                        else None
                    ),
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
                            self.travel_value.total(candidate, request, self.profile)
                        )
                    )
                else:
                    survivors.append(
                        candidate.with_score(self._estimate(candidate, request))
                    )

        return survivors, finished

    def _usable_minutes(self, state: SearchState, leg: TransportOption) -> int:
        """Sightseeing minutes the stay this leg ends actually yielded."""
        if not state.cities:
            return 0
        return usable_minutes(
            state.current_datetime,
            leg.departure,
            day_start=self.config.usable_day_start,
            day_end=self.config.usable_day_end,
        )

    def _estimate(self, state: SearchState, request: TripRequest) -> float:
        """Optimistic Travel Value of the cheapest way this state could finish.

        The completion charges the cheapest return leg, the ride home, *and*
        the nights the traveler must still pay for. Without that last term a
        cheap flight into an expensive city would look better than it is -
        which is exactly the trap V2 exists to avoid.
        """
        price = minutes = None
        if self.return_estimator is not None:
            price = self.return_estimator.min_return_price_per_person(
                state.current_location
            )
            minutes = self.return_estimator.min_return_minutes(state.current_location)

        remaining_accommodation = 0.0
        if self.accommodation_estimator is not None and state.route:
            remaining_accommodation = self.accommodation_estimator.min_stay_cost(
                state.current_location,
                self.config.min_city_stay_days,
                request.travelers,
            )

        if state.completed or not state.route:
            return self.travel_value.total(state, request, self.profile)

        hypothetical = self.scoring.hypothetical_completion(
            state,
            request,
            return_price_per_person=price,
            return_minutes=minutes,
            remaining_accommodation_cost=remaining_accommodation,
        )
        return self.travel_value.total(
            hypothetical, request, self.profile, optimistic=True
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
