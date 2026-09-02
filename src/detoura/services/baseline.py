"""Baseline planner.

The baseline is what a conventional search would return: the cheapest simple
round trip from one of the origin airports to the user's preferred destination
and back, inside the requested window and duration.

It exists purely as a reference point. When the user names no preferred
destination there is no baseline - the planner does not invent one.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Sequence

from ..config import PlannerConfig
from ..models.itinerary import (
    BaselineComparison,
    BaselineResult,
    CostBreakdown,
    Itinerary,
)
from ..models.transport import TransportOption
from ..models.trip import TripRequest
from ..providers.accommodation import AccommodationDataProvider, NoAccommodationProvider
from ..providers.destinations import DestinationProvider
from ..providers.ground_transfer import FreeGroundTransferProvider, GroundTransferProvider
from ..providers.transport import TransportDataProvider
from ..usable_time import usable_minutes


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
        # ``(total, outbound, inbound, transport, accommodation, transfer)``
        best: tuple[float, TransportOption, TransportOption, float, float, float] | None = (
            None
        )

        for airport in sorted(set(origin_airports)):
            transfer = self._transfer_cost(request, airport)
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
                            stay_cost = (
                                rooms[0].total_price(request.travelers) if rooms else 0.0
                            )
                            cost = transport + stay_cost + transfer
                            if cost > request.budget:
                                continue
                            if best is None or cost < best[0]:
                                best = (
                                    cost,
                                    outbound,
                                    inbound,
                                    transport,
                                    stay_cost,
                                    transfer,
                                )

        if best is None:
            return None

        cost, outbound, inbound, transport, stay_cost, transfer = best
        elapsed_minutes = int((inbound.arrival - outbound.departure).total_seconds() // 60)
        return BaselineResult(
            destination=destination.id,
            total_cost=round(cost, 2),
            currency=request.currency,
            duration_days=round(elapsed_minutes / (24 * 60), 2),
            legs=[outbound, inbound],
            total_travel_minutes=outbound.duration_minutes + inbound.duration_minutes,
            cost_breakdown=CostBreakdown(
                transport=round(transport, 2),
                accommodation=round(stay_cost, 2),
                ground_transfer=round(transfer, 2),
            ),
            nights=(inbound.departure.date() - outbound.arrival.date()).days,
            usable_destination_minutes=usable_minutes(
                outbound.arrival,
                inbound.departure,
                day_start=self.config.usable_day_start,
                day_end=self.config.usable_day_end,
            ),
        )

    def _transfer_cost(self, request: TripRequest, airport: str) -> float:
        """Getting to the airport and home again, for the whole party."""
        options = self.ground_transfer.search(request.origin, airport)
        if not options:
            return 0.0
        return round(options[0].total_price(request.travelers) * 2, 2)


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
