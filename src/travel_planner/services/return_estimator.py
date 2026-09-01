"""Lower bounds on getting home, used for search pruning."""

from __future__ import annotations

from datetime import date
from typing import Iterable, Sequence

from ..providers.transport import TransportDataProvider


class CachedReturnEstimator:
    """Cheapest / fastest possible return from a city to *any* origin airport.

    The bounds are computed by asking the provider the same questions the search
    would ask anyway, then cached per city. They are admissible: no real return
    leg can be cheaper or faster than the minimum over the whole window, so
    pruning on them never discards a feasible itinerary.

    It is deliberately provider-agnostic - a real API implementation plugs in
    unchanged.
    """

    def __init__(
        self,
        provider: TransportDataProvider,
        *,
        origin_airports: Iterable[str],
        dates: Sequence[date],
        allowed_transport_types: Iterable[str] | None = None,
    ) -> None:
        self._provider = provider
        self._origin_airports = tuple(sorted(set(origin_airports)))
        self._dates = tuple(sorted(set(dates)))
        self._allowed = set(allowed_transport_types) if allowed_transport_types else None
        self._cache: dict[str, tuple[float | None, int | None]] = {}

    def _bounds(self, city: str) -> tuple[float | None, int | None]:
        cached = self._cache.get(city)
        if cached is not None:
            return cached
        best_price: float | None = None
        best_minutes: int | None = None
        for airport in self._origin_airports:
            if airport == city:
                continue
            for day in self._dates:
                for option in self._provider.search(city, airport, day):
                    if self._allowed and option.transport_type.value not in self._allowed:
                        continue
                    if best_price is None or option.price_per_person < best_price:
                        best_price = option.price_per_person
                    if best_minutes is None or option.duration_minutes < best_minutes:
                        best_minutes = option.duration_minutes
        self._cache[city] = (best_price, best_minutes)
        return self._cache[city]

    # -- ReturnEstimator protocol --------------------------------------
    def min_return_price_per_person(self, city: str) -> float | None:
        if city in self._origin_airports:
            return 0.0
        return self._bounds(city)[0]

    def min_return_minutes(self, city: str) -> int | None:
        if city in self._origin_airports:
            return 0
        return self._bounds(city)[1]
