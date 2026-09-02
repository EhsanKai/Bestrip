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
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from time import perf_counter
from typing import Iterable, Sequence

from ..config import PlannerConfig
from ..constraints.validator import (
    AccommodationEstimator,
    ConstraintValidator,
    ReturnEstimator,
)
from ..models.accommodation import AccommodationOption
from ..models.debug import (
    BeamRound,
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


@dataclass(frozen=True, slots=True)
class CandidateAction:
    """One legal move out of a search state.

    A named record rather than a tuple: this grew a field in every version, and
    positional unpacking makes a mis-ordered argument a silent behaviour change
    instead of an error.
    """

    leg: TransportOption
    is_return: bool
    accommodation: AccommodationOption | None
    cheapest_alternative: AccommodationOption | None
    rejection: RejectionReason | None = None
    """Set when the move is illegal before the validator ever sees it."""

    @property
    def bookable(self) -> bool:
        return self.rejection is None


#: How close to the best score an itinerary must be to count as a real
#: alternative rather than an also-ran (V5.2.1). Calibrated against observed
#: result sets: the gap between adjacent recommendations is typically 0.005 to
#: 0.02, so 0.03 spans roughly the top handful a traveler would actually weigh.
COMPETITIVE_MARGIN = 0.03


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
        #: Beam width actually in use; ``search`` scales it per run.
        self._beam_width = config.beam_width
        #: The bounded destination pool, resolved once per optimizer.
        self._pool: list | None = None

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
        """Run beam search and return all completed states found, best first.

        With ``adaptive_beam`` on this runs the search several times at
        increasing widths and returns the widest round's result; otherwise it
        is a single pass at the configured width.
        """
        if self.config.adaptive_beam:
            return self._adaptive_search(
                request,
                origin_airports=origin_airports,
                start_dates=start_dates,
                debug=debug,
            )
        return self._search_once(
            request,
            origin_airports=origin_airports,
            start_dates=start_dates,
            beam_width=self._effective_beam_width(len(start_dates)),
            debug=debug,
        )

    def _adaptive_search(
        self,
        request: TripRequest,
        *,
        origin_airports: Sequence[str],
        start_dates: Sequence[date],
        debug: SearchDebug | None = None,
    ) -> list[SearchState]:
        """Widen the beam while widening still buys something (V5.2.1).

        V4 stopped on one signal: did the best score improve? That is too
        narrow, and it fails in both directions. A round can raise the top
        score by nothing at all and still be the round that found the only
        three-city itinerary in the result - the traveler's *choice* got wider
        even though the winner did not change. And a round can nudge the score
        by a hair while discovering nothing, which is not worth another
        doubling.

        So a round is productive when it does any of:

        * beat the previous best score by more than the tolerance;
        * discover a **city combination** no narrower round found - a genuinely
          new possibility, which is what "search deeper" promises; or
        * find a **competitive** itinerary - one within ``COMPETITIVE_MARGIN``
          of the best, i.e. a real alternative rather than an also-ran.

        It stops when a round buys none of those, when it runs out of rounds or
        width, or when the wall-clock budget for the mode is spent. Every
        signal is a deterministic function of the itineraries found, so the
        ladder is still reproducible.

        Rounds share the optimizer's provider caches, so round *n+1* re-fetches
        nothing round *n* already asked for.
        """
        ceiling = self.config.adaptive_beam_max_width
        width = min(self._effective_beam_width(len(start_dates)), ceiling)
        budget = self.config.adaptive_beam_time_budget_seconds
        started = perf_counter()
        best_score = None
        completed: list[SearchState] = []
        rounds: list[BeamRound] = []
        seen_city_sets: set[frozenset[str]] = set()
        seen_signatures: set[tuple[str, ...]] = set()
        round_seconds: list[float] = []
        stop_reason = "rounds_exhausted"

        for attempt in range(self.config.adaptive_beam_max_rounds):
            # Only the final round's trace is kept: the intermediate ones
            # describe searches whose results were thrown away, and reporting
            # them as if they were the run would misstate what happened.
            round_debug = SearchDebug() if debug is not None else None
            found = self._search_once(
                request,
                origin_airports=origin_airports,
                start_dates=start_dates,
                beam_width=width,
                debug=round_debug,
            )
            score = found[0].score if found else 0.0
            improvement = 0.0 if best_score is None else round(score - best_score, 9)

            city_sets = {state.city_signature() for state in found}
            new_city_sets = len(city_sets - seen_city_sets)
            cutoff = score - COMPETITIVE_MARGIN
            competitive = {
                state.signature()
                for state in found
                if state.score >= cutoff
            }
            competitive_found = len(competitive - seen_signatures)
            seen_city_sets |= city_sets
            seen_signatures |= competitive

            rounds.append(
                BeamRound(
                    beam_width=width,
                    best_score=round(score, 6),
                    completed=len(found),
                    states_generated=(
                        round_debug.total_generated if round_debug else 0
                    ),
                    improvement=improvement,
                    accepted=True,
                    frontier_size=len(city_sets),
                    new_city_sets=new_city_sets if attempt else 0,
                    competitive_found=competitive_found if attempt else 0,
                )
            )
            round_seconds.append(round(perf_counter() - started, 4))
            completed, best_score = found, score
            if debug is not None and round_debug is not None:
                # The previous round's work is now discarded, but it was still
                # paid for: move its cost into the discarded counters before
                # this round's trace replaces it.
                debug.discarded_states_generated += sum(
                    it.generated for it in debug.iterations
                )
                debug.discarded_states_rejected += sum(
                    it.rejected for it in debug.iterations
                )
                debug.iterations = round_debug.iterations
                debug.initial_states = round_debug.initial_states
                debug.origin_airports = round_debug.origin_airports
                debug.start_dates = round_debug.start_dates
                debug.effective_beam_width = width

            # A round is worth its cost if it moved the score, widened the set
            # of possible trips, or added a real alternative. None of the
            # three means the next doubling will not either.
            bought_score = improvement > self.config.adaptive_beam_tolerance
            bought_options = new_city_sets > 0 or competitive_found > 0
            stalled = attempt > 0 and not (bought_score or bought_options)

            elapsed = perf_counter() - started
            if stalled:
                stop_reason = "diminishing_returns"
            elif width >= ceiling:
                stop_reason = "width_ceiling"
            elif budget is not None and elapsed >= budget:
                # Checked after the round rather than before the next one:
                # a mode that promises "10-15 seconds" must not start a
                # doubling it has no time to finish.
                stop_reason = "time_budget"
            else:
                width = min(width * 2, ceiling)
                continue
            break

        if rounds:
            rounds[-1] = rounds[-1].model_copy(update={"stop_reason": stop_reason})

        self._beam_width = width
        if debug is not None:
            debug.beam_rounds = rounds
            debug.beam_round_seconds = round_seconds
            debug.completed_itineraries = len(completed)
        return completed

    def _search_once(
        self,
        request: TripRequest,
        *,
        origin_airports: Sequence[str],
        start_dates: Sequence[date],
        beam_width: int,
        debug: SearchDebug | None = None,
    ) -> list[SearchState]:
        """One complete beam search at a fixed width."""
        self._beam_width = beam_width
        beam = self._initial_states(origin_airports, start_dates, request.travelers)
        if debug is not None:
            debug.effective_beam_width = self._beam_width
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
                beam_width=self._beam_width,
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

    def _effective_beam_width(self, start_date_count: int) -> int:
        """Beam width for this run, scaled by how many start dates are in play."""
        if not self.config.scale_beam_with_start_dates:
            return self.config.beam_width
        scaled = self.config.beam_width * max(start_date_count, 1)
        return min(scaled, self.config.max_effective_beam_width)

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
        stay_options = self.config.stay_day_options
        if self.config.max_stay_lengths is not None:
            stay_options = stay_options[: self.config.max_stay_lengths]
        dates = []
        for stay in stay_options:
            candidate = arrival + timedelta(days=stay)
            if candidate <= request.date_to:
                dates.append(candidate)
        return dates

    def _departures(
        self,
        origin: str,
        destination: str,
        day: date,
        after: datetime,
        travelers: int,
    ) -> tuple[list[TransportOption], list[TransportOption]]:
        """``(bookable departures, sold-out departures)`` for one leg.

        The provider may return far more than the search can afford to branch
        on - a real API certainly will - so only the cheapest
        ``max_transport_options_per_leg`` are kept. They arrive cheapest-first
        from the provider, so the cap is a prefix, not a re-sort.

        Sold-out fares (V4) are excluded *before* the cap, because a fare the
        party cannot board is not a cheaper option, it is not an option - and
        letting one occupy a cap slot would push a bookable fare out of the
        search. They are returned rather than discarded so the rejection reaches
        the trace instead of the cheapest fares silently vanishing.
        """
        options: list[TransportOption] = []
        sold_out: list[TransportOption] = []
        for option in self.transport.search(origin, destination, day):
            if option.departure < after:
                continue
            if self.config.require_availability and not option.has_seats_for(travelers):
                sold_out.append(option)
                continue
            options.append(option)
        return options[: self.config.max_transport_options_per_leg], sold_out

    def _candidate_pool(self) -> list:
        """The bounded set of destinations this run will ever price.

        Capping here rather than per expansion is what makes the limit mean
        something to a real integration: it bounds how many cities are ever
        fetched, not merely how many are branched on at one node. The pool is
        catalog order for now - a production system would rank it by expected
        value first, which is a strictly better pool of the same size.
        """
        if self._pool is None:
            catalog = self.destinations.all()
            limit = self.config.max_candidate_destinations
            self._pool = catalog if limit is None else catalog[:limit]
        return self._pool

    def _candidate_destinations(self, state: SearchState) -> list:
        """Destinations worth expanding towards from this state."""
        return [
            destination
            for destination in self._candidate_pool()
            if destination.id != state.current_location
            and destination.id not in state.visited_cities
        ]

    def _book_accommodation(
        self, state: SearchState, leg: TransportOption, travelers: int
    ) -> tuple[list[AccommodationOption | None], RejectionReason | None]:
        """``(rooms, rejection)`` for the stay this leg ends.

        ``([None], None)`` means there is nothing to book - accommodation is
        off, the traveler is still at home, or no night is actually spent - so
        the caller always has exactly one branch in the V1-equivalent
        configuration.

        A rejection means the stay is *required* but cannot be booked, which is
        a real rejection rather than "no cost". V4 distinguishes the two ways
        that happens: the city has nothing on offer at all, or what it has is
        sold out for this party. A traveler told "no rooms in Prague" when the
        truth is "the last double went" has been told the wrong thing.
        """
        if (
            not state.cities
            or not self.config.enable_accommodation
            or self.accommodation is None
        ):
            return [None], None
        check_in = state.current_datetime.date()
        check_out = leg.departure.date()
        if check_out <= check_in:
            # Same-day hop: no night is spent, so nothing to book.
            return [None], None
        options = self.accommodation.search(
            state.current_location, check_in, check_out, travelers
        )
        if not options:
            return [], RejectionReason.NO_ACCOMMODATION_AVAILABLE
        if self.config.require_availability:
            bookable = [
                option for option in options if option.has_capacity_for(travelers)
            ]
            if not bookable:
                return [], RejectionReason.SOLD_OUT_ACCOMMODATION
            options = bookable
        return list(options[: self.config.accommodation_options_per_stay]), None

    @staticmethod
    def _cheapest(rooms: Sequence[AccommodationOption | None]) -> AccommodationOption | None:
        """The cheapest room among the ones fetched for a stay.

        The provider contract is cheapest-first, but this does not rely on it:
        a real provider that sorts by relevance must not silently corrupt the
        value-for-money baseline.
        """
        priced = [room for room in rooms if room is not None]
        if not priced:
            return None
        return min(priced, key=lambda room: (room.price_per_night, room.id))

    def _candidate_actions(
        self, state: SearchState, request: TripRequest
    ) -> Iterable["CandidateAction"]:
        """Yield one :class:`CandidateAction` for every legal move.

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
            targets: list[tuple[str, bool]] = []
            if may_continue:
                targets += [
                    (destination.id, False)
                    for destination in self._candidate_destinations(state)
                ]
            if may_return:
                targets += [
                    (airport, True) for airport in sorted(self.validator.origin_airports)
                ]

            for target, is_return in targets:
                departures, sold_out = self._departures(
                    state.current_location,
                    target,
                    departure_date,
                    state.current_datetime,
                    request.travelers,
                )
                for option in sold_out:
                    yield CandidateAction(
                        option, is_return, None, None,
                        RejectionReason.SOLD_OUT_TRANSPORT,
                    )
                for option in departures:
                    rooms, rejection = self._book_accommodation(
                        state, option, request.travelers
                    )
                    if rejection is not None:
                        yield CandidateAction(option, is_return, None, None, rejection)
                        continue
                    cheapest = self._cheapest(rooms)
                    for room in rooms:
                        yield CandidateAction(option, is_return, room, cheapest)

    def _expand_beam(
        self,
        beam: Sequence[SearchState],
        request: TripRequest,
        trace: IterationDebug,
    ) -> tuple[list[SearchState], list[SearchState]]:
        survivors: list[SearchState] = []
        finished: list[SearchState] = []

        for state in beam:
            for action in self._candidate_actions(state, request):
                option = action.leg
                trace.generated += 1
                if action.rejection is not None:
                    trace.rejected += 1
                    reason = action.rejection
                    trace.rejection_counts[reason] = (
                        trace.rejection_counts.get(reason, 0) + 1
                    )
                    if len(trace.rejected_examples) < self.config.debug_example_limit:
                        trace.rejected_examples.append(
                            RejectedState(
                                iteration=trace.iteration,
                                route=_route_labels(state) + [option.destination],
                                reason=reason,
                                detail=(
                                    f"{option.origin} -> {option.destination} "
                                    f"on {option.departure:%Y-%m-%d %H:%M}"
                                ),
                            )
                        )
                    continue
                candidate = state.extend(
                    option,
                    travelers=request.travelers,
                    is_return=action.is_return,
                    accommodation=action.accommodation,
                    usable_minutes=self._usable_minutes(state, option),
                    return_transfer=(
                        self.ground_transfers.get(option.destination)
                        if action.is_return
                        else None
                    ),
                    cheapest_alternative=action.cheapest_alternative,
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
            if len(kept) == self._beam_width:
                overflow.extend(survivors[index:])
                break
            if per_route[state.cities] < self.config.beam_slots_per_route:
                per_route[state.cities] += 1
                kept.append(state)
            else:
                overflow.append(state)
        # If the per-route cap left the beam under-filled, top it up in rank
        # order so the configured beam width is always honoured.
        if len(kept) < self._beam_width and overflow:
            promoted = overflow[: self._beam_width - len(kept)]
            overflow = overflow[len(promoted) :]
            kept.extend(promoted)
            kept.sort(key=lambda state: self._beam_rank_key(state, mandatory))
        return kept, overflow
