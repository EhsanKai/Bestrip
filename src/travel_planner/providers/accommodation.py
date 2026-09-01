"""Accommodation providers.

Same seam as transport: the optimizer only ever sees the protocol, so swapping
the synthetic dataset for a real API (Booking, Expedia, ...) touches nothing in
``algorithms/`` or ``constraints/``.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from ..data.synthetic_accommodation import TIERS, build_options, nightly_rate
from ..models.accommodation import AccommodationOption


@runtime_checkable
class AccommodationDataProvider(Protocol):
    """Source of bookable stays."""

    def search(
        self,
        city: str,
        check_in: date,
        check_out: date,
        travelers: int,
    ) -> list[AccommodationOption]:
        """Stays in ``city`` for the given nights, cheapest total first.

        Implementations must return an empty list (never raise) when nothing is
        available, and must be deterministic for a given argument tuple.
        """
        ...

    def min_price_per_night(self, city: str, travelers: int) -> float | None:
        """Cheapest conceivable per-night cost for the party in ``city``.

        Used as an admissible lower bound for search pruning; returning ``None``
        disables accommodation pruning for that city.
        """
        ...


class SyntheticAccommodationDataProvider:
    """Serves the built-in synthetic accommodation dataset."""

    def __init__(
        self,
        rates: dict[str, float] | None = None,
        *,
        date_variation: bool = True,
    ) -> None:
        self._rates = rates
        self._date_variation = date_variation
        self._cache: dict[tuple[str, date, date, int], list[AccommodationOption]] = {}
        self._min_cache: dict[tuple[str, int], float] = {}
        self.search_calls = 0

    # -- helpers -------------------------------------------------------
    def _base_rate(self, city: str) -> float | None:
        """The standard-tier nightly rate, or ``None`` if the city is unknown.

        An injected rate table is authoritative: a city missing from it has no
        accommodation at all, which is what test fixtures rely on to keep a
        scenario's economics exact.
        """
        if self._rates is not None:
            return self._rates.get(city)
        return nightly_rate(city)

    # -- AccommodationDataProvider ------------------------------------
    def search(
        self,
        city: str,
        check_in: date,
        check_out: date,
        travelers: int,
    ) -> list[AccommodationOption]:
        self.search_calls += 1
        key = (city, check_in, check_out, travelers)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        rate = self._base_rate(city)
        if check_out <= check_in or rate is None:
            self._cache[key] = []
            return []
        options = build_options(
            city,
            check_in,
            check_out,
            base_rate=rate,
            date_variation=self._date_variation,
        )
        # Deterministic: cheapest party total first, then tier name, then id.
        options.sort(key=lambda o: (o.total_price(travelers), o.tier.value, o.id))
        self._cache[key] = options
        return options

    def min_price_per_night(self, city: str, travelers: int) -> float | None:
        """The cheapest tier's per-night party cost, ignoring date surcharges.

        Admissible: no real option can be cheaper, because every surcharge in
        the dataset is >= 1.0.
        """
        key = (city, travelers)
        cached = self._min_cache.get(key)
        if cached is not None:
            return cached
        base = self._base_rate(city)
        if base is None:
            return None
        best = min(
            base * multiplier * -(-travelers // capacity)  # ceil division
            for _tier, multiplier, capacity, _rating in TIERS
        )
        self._min_cache[key] = round(best, 2)
        return self._min_cache[key]


class NoAccommodationProvider:
    """Explicit "stays are free" provider - the V1 behaviour.

    Kept so the V1 economics stay reproducible and testable, and so
    ``enable_accommodation=False`` has a real object behind it rather than a
    pile of ``None`` checks.
    """

    def search(
        self,
        city: str,
        check_in: date,
        check_out: date,
        travelers: int,
    ) -> list[AccommodationOption]:
        return []

    def min_price_per_night(self, city: str, travelers: int) -> float | None:
        return 0.0


class RealAccommodationDataProvider:
    """Placeholder for a live hotel API. Deliberately unimplemented."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError(
            "RealAccommodationDataProvider is a placeholder. The MVP runs entirely "
            "on SyntheticAccommodationDataProvider; wire a real API here to go live."
        )

    def search(  # pragma: no cover - never constructed
        self, city: str, check_in: date, check_out: date, travelers: int
    ) -> list[AccommodationOption]:
        raise NotImplementedError

    def min_price_per_night(  # pragma: no cover - never constructed
        self, city: str, travelers: int
    ) -> float | None:
        raise NotImplementedError
