"""Admissible lower bounds on accommodation, for search pruning.

The protocol these satisfy is
:class:`detoura.constraints.validator.AccommodationEstimator`.
"""

from __future__ import annotations

from ..providers.accommodation import AccommodationDataProvider


class CachedAccommodationEstimator:
    """Wraps a provider's per-night floor into a whole-stay bound.

    The bound is admissible: it uses the cheapest tier at the un-surcharged
    base rate, so no bookable stay can come in under it and pruning on it never
    discards a feasible itinerary.
    """

    def __init__(self, provider: AccommodationDataProvider) -> None:
        self._provider = provider
        self._cache: dict[tuple[str, int], float] = {}

    def min_price_per_night(self, city: str, travelers: int) -> float:
        key = (city, travelers)
        cached = self._cache.get(key)
        if cached is None:
            price = self._provider.min_price_per_night(city, travelers)
            cached = 0.0 if price is None else price
            self._cache[key] = cached
        return cached

    def min_stay_cost(self, city: str, nights: int, travelers: int) -> float:
        if nights <= 0:
            return 0.0
        return round(self.min_price_per_night(city, travelers) * nights, 2)


class ZeroAccommodationEstimator:
    """Bound for the V1 "stays are free" model."""

    def min_stay_cost(self, city: str, nights: int, travelers: int) -> float:
        return 0.0
