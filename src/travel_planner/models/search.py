"""The search state explored by beam search."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from .accommodation import AccommodationOption
from .transfer import GroundTransferOption
from .transport import TransportOption


@dataclass(frozen=True, slots=True)
class CityStay:
    """One completed stay in one city.

    Carries everything needed to price and evaluate the stay without going back
    to a provider: when the traveler arrived and left, how many nights that is,
    what the room cost, and how much of it was actually usable for sightseeing.
    """

    city: str
    arrival: datetime
    departure: datetime
    nights: int
    accommodation: AccommodationOption | None = None
    accommodation_cost: float = 0.0
    usable_minutes: int = 0


@dataclass(frozen=True, slots=True)
class SearchState:
    """An immutable partial (or completed) itinerary.

    A state carries everything needed to reconstruct the full itinerary and to
    decide whether it may be extended, so the search itself stays stateless.

    Costs are decomposed into transport, accommodation and ground transfer;
    :attr:`total_cost` is their sum and is always a *party* total.
    """

    origin_airport: str
    """The departure node the trip started from (e.g. ``DUS``)."""

    current_location: str
    """Where the traveler currently is."""

    start_datetime: datetime
    current_datetime: datetime

    route: tuple[TransportOption, ...] = ()
    cities: tuple[str, ...] = ()
    """Destination cities in visiting order (origin airports excluded)."""

    stays: tuple[CityStay, ...] = ()
    """Completed stays, one per city the traveler has already left."""

    transport_cost: float = 0.0
    accommodation_cost: float = 0.0
    ground_transfer_cost: float = 0.0

    total_travel_minutes: int = 0
    """Time on intercity transport only (excludes ground transfers)."""
    ground_transfer_minutes: int = 0

    outbound_transfer: GroundTransferOption | None = None
    return_transfer: GroundTransferOption | None = None

    completed: bool = False

    score: float = 0.0
    """Score of the completed itinerary, or the optimistic estimate while partial."""

    visited_cities: frozenset[str] = field(default_factory=frozenset)

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------
    @property
    def total_cost(self) -> float:
        """Everything the party pays: transport + accommodation + transfers."""
        return round(
            self.transport_cost + self.accommodation_cost + self.ground_transfer_cost, 2
        )

    @property
    def stay_days(self) -> tuple[int, ...]:
        """Nights spent in each city already left, in visiting order."""
        return tuple(stay.nights for stay in self.stays)

    @property
    def usable_destination_minutes(self) -> int:
        """Sightseeing minutes accumulated across completed stays."""
        return sum(stay.usable_minutes for stay in self.stays)

    @property
    def total_transport_minutes(self) -> int:
        """All time in transit, ground transfers included."""
        return self.total_travel_minutes + self.ground_transfer_minutes

    @property
    def elapsed_minutes(self) -> int:
        """Minutes since midnight of the trip's start date."""
        return int((self.current_datetime - self.start_datetime).total_seconds() // 60)

    @property
    def trip_span_minutes(self) -> int:
        """Minutes from the first departure to wherever the traveler is now.

        This - not :attr:`elapsed_minutes` - is the trip's real length, and it
        is what the duration constraints and the time score measure. Counting
        from midnight would silently spend several hours of a five-day
        allowance before the traveler had left the house.
        """
        if not self.route:
            return 0
        return int(
            (self.current_datetime - self.route[0].departure).total_seconds() // 60
        )

    @property
    def city_count(self) -> int:
        return len(self.cities)

    @property
    def leg_count(self) -> int:
        return len(self.route)

    @property
    def is_at_start(self) -> bool:
        return not self.route

    def signature(self) -> tuple[str, ...]:
        """Stable identity used for deterministic tie-breaking."""
        return (self.origin_airport, *(leg.id for leg in self.route))

    def city_signature(self) -> frozenset[str]:
        """The set of cities visited - the key used by the diversity filter."""
        return self.visited_cities

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------
    def with_outbound_transfer(
        self, transfer: GroundTransferOption | None, *, travelers: int
    ) -> "SearchState":
        """Charge the journey from the user's origin to the departure airport."""
        if transfer is None:
            return self
        return replace(
            self,
            outbound_transfer=transfer,
            ground_transfer_cost=round(
                self.ground_transfer_cost + transfer.total_price(travelers), 2
            ),
            ground_transfer_minutes=self.ground_transfer_minutes
            + transfer.duration_minutes,
        )

    def extend(
        self,
        leg: TransportOption,
        *,
        travelers: int,
        is_return: bool = False,
        accommodation: AccommodationOption | None = None,
        usable_minutes: int = 0,
        return_transfer: GroundTransferOption | None = None,
    ) -> "SearchState":
        """Return a new state with ``leg`` appended.

        When the traveler is leaving a city (rather than setting off from home),
        the stay they are ending is recorded as a :class:`CityStay`, priced with
        ``accommodation`` if one was booked. ``return_transfer`` charges the ride
        home from the arrival airport.
        """
        stays = self.stays
        accommodation_cost = self.accommodation_cost
        if self.cities:
            stay_cost = (
                accommodation.total_price(travelers) if accommodation is not None else 0.0
            )
            stays = self.stays + (
                CityStay(
                    city=self.current_location,
                    arrival=self.current_datetime,
                    departure=leg.departure,
                    nights=(leg.departure.date() - self.current_datetime.date()).days,
                    accommodation=accommodation,
                    accommodation_cost=stay_cost,
                    usable_minutes=usable_minutes,
                ),
            )
            accommodation_cost = round(accommodation_cost + stay_cost, 2)

        cities = self.cities
        visited = self.visited_cities
        if not is_return:
            cities = self.cities + (leg.destination,)
            visited = self.visited_cities | {leg.destination}

        transfer_cost = self.ground_transfer_cost
        transfer_minutes = self.ground_transfer_minutes
        if return_transfer is not None:
            transfer_cost = round(
                transfer_cost + return_transfer.total_price(travelers), 2
            )
            transfer_minutes += return_transfer.duration_minutes

        return replace(
            self,
            current_location=leg.destination,
            current_datetime=leg.arrival,
            route=self.route + (leg,),
            cities=cities,
            stays=stays,
            transport_cost=round(self.transport_cost + leg.total_price(travelers), 2),
            accommodation_cost=accommodation_cost,
            ground_transfer_cost=transfer_cost,
            ground_transfer_minutes=transfer_minutes,
            total_travel_minutes=self.total_travel_minutes + leg.duration_minutes,
            completed=is_return,
            visited_cities=visited,
            return_transfer=return_transfer if is_return else self.return_transfer,
        )

    def with_score(self, score: float) -> "SearchState":
        return replace(self, score=score)
