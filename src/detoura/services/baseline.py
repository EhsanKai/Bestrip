"""Baseline planner.

The baseline is what a conventional search would return: the cheapest simple
round trip from one of the origin airports to the user's preferred destination
and back, inside the requested window and duration.

It exists purely as a reference point. When the user names no preferred
destination there is no baseline - the planner does not invent one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol, Sequence, runtime_checkable

from ..config import PlannerConfig
from ..models.itinerary import (
    BaselineComparison,
    BaselineResult,
    CostBreakdown,
    Itinerary,
)
from ..models.accommodation import AccommodationOption
from ..models.baggage import BaggageRequirement
from ..models.search import SearchState
from ..models.transfer import GroundTransferOption
from ..models.transport import TransportOption
from ..models.trip import TripRequest
from ..profiles import RecommendationProfile
from ..providers.accommodation import AccommodationDataProvider, NoAccommodationProvider
from ..providers.destinations import DestinationProvider
from ..providers.ground_transfer import FreeGroundTransferProvider, GroundTransferProvider
from ..providers.transport import TransportDataProvider
from ..usable_time import usable_minutes
from .baggage_pricing import quote_trip


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One feasible baseline round trip, with everything needed to re-price
    *and* re-score it. Previously a six-tuple; it grew two more members in V7
    and a positional tuple of eight is a bug waiting to be written."""

    cost: float
    airport: str
    outbound: TransportOption
    inbound: TransportOption
    transport: float
    stay_cost: float
    transfer: float
    room: AccommodationOption | None
    transfer_option: GroundTransferOption | None


@runtime_checkable
class BaselineScorer(Protocol):
    """The subset of :class:`~detoura.algorithms.travel_value.TravelValueScorer`
    the baseline needs.

    Declared structurally rather than imported so this module keeps depending
    only on models - and so a test can substitute a scorer without building the
    whole scoring stack.
    """

    def experience_score(self, state: SearchState, request: TripRequest) -> float: ...

    def preference_score(self, state: SearchState, request: TripRequest) -> float: ...

    def accommodation_score(self, state: SearchState, request: TripRequest) -> float: ...

    def convenience_score(self, state: SearchState) -> float: ...

    def score(
        self,
        state: SearchState,
        request: TripRequest,
        profile: RecommendationProfile,
    ): ...


class BaselinePlanner:
    """Finds the naive single-destination round trip."""

    def __init__(
        self,
        config: PlannerConfig,
        *,
        transport_provider: TransportDataProvider,
        destination_provider: DestinationProvider,
        accommodation_provider: AccommodationDataProvider | None = None,
        ground_transfer_provider: GroundTransferProvider | None = None,
    ) -> None:
        self.config = config
        self.transport = transport_provider
        self.destinations = destination_provider
        self.accommodation = accommodation_provider or NoAccommodationProvider()
        self.ground_transfer = ground_transfer_provider or FreeGroundTransferProvider()

    def compute(
        self,
        request: TripRequest,
        *,
        origin_airports: Sequence[str],
        start_dates: Sequence[date],
        scorer: BaselineScorer | None = None,
        profile: RecommendationProfile | None = None,
    ) -> BaselineResult | None:
        """Cheapest ``origin -> preferred destination -> origin`` round trip.

        Returns ``None`` when the user named no preferred destination, when the
        destination is unknown, or when no feasible round trip exists inside the
        budget, window and duration.
        """
        if not request.preferred_destinations:
            return None
        destination = self.destinations.get(request.preferred_destinations[0])
        if destination is None:
            return None

        allowed_types = set(request.transport_preferences)
        minimum_elapsed = (
            self.config.min_duration_utilization * request.max_trip_minutes
        )
        best: _Candidate | None = None

        for airport in sorted(set(origin_airports)):
            transfer_option = self._transfer_option(request, airport)
            transfer = (
                round(transfer_option.total_price(request.travelers) * 2, 2)
                if transfer_option is not None
                else 0.0
            )
            for start_date in start_dates:
                outbound_options = [
                    option
                    for option in self.transport.search(airport, destination.id, start_date)
                    if option.transport_type in allowed_types
                    and request.date_from <= option.arrival.date() <= request.date_to
                ]
                if not outbound_options:
                    continue
                for outbound in outbound_options:
                    for nights in range(1, request.duration_days + 1):
                        return_date = outbound.arrival.date() + timedelta(days=nights)
                        if return_date > request.date_to:
                            break
                        for inbound in self.transport.search(
                            destination.id, airport, return_date
                        ):
                            if inbound.transport_type not in allowed_types:
                                continue
                            if inbound.arrival.date() > request.date_to:
                                continue
                            elapsed = (
                                inbound.arrival - outbound.departure
                            ).total_seconds() / 60
                            if elapsed > request.max_trip_minutes:
                                continue
                            # The baseline must be the trip the user asked for,
                            # not the cheapest way to touch the destination:
                            # once accommodation is priced in, a one-night
                            # "five-day trip" would win on cost and make every
                            # comparison against it meaningless.
                            if elapsed < minimum_elapsed:
                                continue
                            transport = outbound.total_price(
                                request.travelers
                            ) + inbound.total_price(request.travelers)
                            # A baseline the user could actually book: the
                            # hotel and the ride to the airport are part of it,
                            # or the comparison against V2 itineraries is unfair.
                            rooms = self.accommodation.search(
                                destination.id,
                                outbound.arrival.date(),
                                inbound.departure.date(),
                                request.travelers,
                            )
                            room = rooms[0] if rooms else None
                            stay_cost = (
                                room.total_price(request.travelers) if room else 0.0
                            )
                            cost = transport + stay_cost + transfer
                            if cost > request.budget:
                                continue
                            if best is None or cost < best.cost:
                                best = _Candidate(
                                    cost=cost,
                                    airport=airport,
                                    outbound=outbound,
                                    inbound=inbound,
                                    transport=transport,
                                    stay_cost=stay_cost,
                                    transfer=transfer,
                                    room=room,
                                    transfer_option=transfer_option,
                                )

        if best is None:
            return None

        outbound, inbound = best.outbound, best.inbound
        elapsed_minutes = int((inbound.arrival - outbound.departure).total_seconds() // 60)
        scores = self._scores(best, request, scorer, profile)
        baggage = (
            quote_trip(
                [best.outbound, best.inbound],
                request.baggage,
                travelers=request.travelers,
            )
            if request.baggage is not BaggageRequirement.NONE
            else None
        )
        # Recorded whether or not a scorer was supplied: the time spent on the
        # airport run is a fact about the trip, not a score of it.
        transfer_minutes = (
            best.transfer_option.duration_minutes * 2
            if best.transfer_option is not None
            else 0
        )
        return BaselineResult(
            destination=destination.id,
            total_cost=round(best.cost, 2),
            currency=request.currency,
            duration_days=round(elapsed_minutes / (24 * 60), 2),
            legs=[outbound, inbound],
            total_travel_minutes=outbound.duration_minutes + inbound.duration_minutes,
            cost_breakdown=CostBreakdown(
                transport=round(best.transport, 2),
                accommodation=round(best.stay_cost, 2),
                ground_transfer=round(best.transfer, 2),
            ),
            nights=(inbound.departure.date() - outbound.arrival.date()).days,
            ground_transfer_minutes=transfer_minutes,
            usable_destination_minutes=usable_minutes(
                outbound.arrival,
                inbound.departure,
                day_start=self.config.usable_day_start,
                day_end=self.config.usable_day_end,
            ),
            baggage=baggage,
            **scores,
        )

    def _transfer_option(
        self, request: TripRequest, airport: str
    ) -> GroundTransferOption | None:
        """Cheapest way to reach ``airport`` from home, or ``None`` if free."""
        options = self.ground_transfer.search(request.origin, airport)
        return options[0] if options else None

    def state_for(self, candidate: "_Candidate", request: TripRequest) -> SearchState:
        """The baseline expressed as a real :class:`SearchState`.

        This is the crux of the V7 comparison. The original idea is scored by
        walking it through the *same* state transitions a searched itinerary
        goes through, so the two are measured by identical code. Recomputing
        the baseline's metrics independently would let the two paths drift, and
        an honest comparison cannot be built on two definitions of "usable
        hours".
        """
        outbound, inbound = candidate.outbound, candidate.inbound
        moment = datetime.combine(outbound.departure.date(), time.min)
        state = SearchState(
            origin_airport=candidate.airport,
            current_location=candidate.airport,
            start_datetime=moment,
            current_datetime=moment,
        ).with_outbound_transfer(candidate.transfer_option, travelers=request.travelers)
        state = state.extend(outbound, travelers=request.travelers)
        return state.extend(
            inbound,
            travelers=request.travelers,
            is_return=True,
            accommodation=candidate.room,
            usable_minutes=usable_minutes(
                outbound.arrival,
                inbound.departure,
                day_start=self.config.usable_day_start,
                day_end=self.config.usable_day_end,
            ),
            return_transfer=candidate.transfer_option,
            cheapest_alternative=candidate.room,
        )

    def _scores(
        self,
        candidate: "_Candidate",
        request: TripRequest,
        scorer: BaselineScorer | None,
        profile: RecommendationProfile | None,
    ) -> dict:
        """Score the baseline, or report honestly that it was not scored."""
        if scorer is None or profile is None:
            return {"scored": False}
        state = self.state_for(candidate, request)
        return {
            "scored": True,
            "experience_score": round(scorer.experience_score(state, request), 6),
            "preference_match": round(scorer.preference_score(state, request), 6),
            "accommodation_score": round(scorer.accommodation_score(state, request), 6),
            "convenience_score": round(scorer.convenience_score(state), 6),
            "travel_value": round(scorer.score(state, request, profile).total, 6),
        }


def compare_to_baseline(
    itinerary: Itinerary, baseline: BaselineResult | None
) -> BaselineComparison | None:
    """How much money, cities and transit time an itinerary trades vs. the baseline."""
    if baseline is None:
        return None
    return BaselineComparison(
        baseline_destination=baseline.destination,
        baseline_cost=baseline.total_cost,
        money_saved=round(baseline.total_cost - itinerary.total_cost, 2),
        additional_cities=len(itinerary.cities) - 1,
        additional_travel_minutes=(
            itinerary.total_travel_minutes - baseline.total_travel_minutes
        ),
    )
