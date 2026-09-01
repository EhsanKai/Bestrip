"""The search state explored by beam search."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from .transport import TransportOption


@dataclass(frozen=True, slots=True)
class SearchState:
    """An immutable partial (or completed) itinerary.

    A state carries everything needed to reconstruct the full itinerary and to
    decide whether it may be extended, so the search itself stays stateless.
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

    stay_days: tuple[int, ...] = ()
    """Planned stay length, in days, for each entry of :attr:`cities`."""

    total_cost: float = 0.0
    """Total cost for the *whole party*, not per person."""

    total_travel_minutes: int = 0
    completed: bool = False

    score: float = 0.0
    """Score of the completed itinerary, or the optimistic estimate while partial."""

    visited_cities: frozenset[str] = field(default_factory=frozenset)

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------
    @property
    def elapsed_minutes(self) -> int:
        return int((self.current_datetime - self.start_datetime).total_seconds() // 60)

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
    def extend(
        self,
        leg: TransportOption,
        *,
        travelers: int,
        stay_days: int | None = None,
        is_return: bool = False,
    ) -> "SearchState":
        """Return a new state with ``leg`` appended.

        ``stay_days`` records how long the traveler stayed in the city they are
        *departing from*; it is ``None`` for the very first leg (the traveler is
        still at home) and for the return leg it belongs to the last city.
        """
        stays = self.stay_days
        if stay_days is not None and self.cities:
            stays = self.stay_days + (stay_days,)
        cities = self.cities
        visited = self.visited_cities
        if not is_return:
            cities = self.cities + (leg.destination,)
            visited = self.visited_cities | {leg.destination}
        return replace(
            self,
            current_location=leg.destination,
            current_datetime=leg.arrival,
            route=self.route + (leg,),
            cities=cities,
            stay_days=stays,
            total_cost=round(self.total_cost + leg.total_price(travelers), 2),
            total_travel_minutes=self.total_travel_minutes + leg.duration_minutes,
            completed=is_return,
            visited_cities=visited,
        )

    def with_score(self, score: float) -> "SearchState":
        return replace(self, score=score)
