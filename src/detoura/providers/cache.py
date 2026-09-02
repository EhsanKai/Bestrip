"""Provider caching and call metrics (V3).

The beam issues thousands of provider lookups per plan - measured at ~15,000
transport and ~15,000 accommodation calls for a five-day two-traveler search.
Against the synthetic dataset that is free. Against a real API it is a rate
limit, a bill, and several minutes of latency.

These decorators put a deterministic in-memory cache in front of any provider
implementing the protocol, and count what happens, so the cost of a real
integration is visible before anyone pays it. Deliberately no Redis, no disk,
no TTL: an in-process cache for the life of one planning run is what the MVP
needs, and anything more is infrastructure the project has not earned yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Generic, TypeVar

from ..models.accommodation import AccommodationOption
from ..models.transfer import GroundTransferOption
from ..models.transport import TransportOption
from .accommodation import AccommodationDataProvider
from .ground_transfer import GroundTransferProvider
from .transport import TransportDataProvider

K = TypeVar("K")
V = TypeVar("V")


@dataclass
class CacheStats:
    """How a cache performed during a run."""

    hits: int = 0
    misses: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    def merged(self, other: "CacheStats") -> "CacheStats":
        return CacheStats(hits=self.hits + other.hits, misses=self.misses + other.misses)


class ProviderCache(Generic[K, V]):
    """A plain memo table with hit/miss accounting."""

    def __init__(self) -> None:
        self._entries: dict[K, V] = {}
        self.stats = CacheStats()

    def get_or_compute(self, key: K, compute) -> V:
        try:
            value = self._entries[key]
        except KeyError:
            self.stats.misses += 1
            value = compute()
            self._entries[key] = value
            return value
        self.stats.hits += 1
        return value

    def clear(self) -> None:
        self._entries.clear()

    @property
    def entries(self) -> int:
        return len(self._entries)


@dataclass
class ProviderMetrics:
    """Aggregate provider behaviour for one planning run."""

    transport: CacheStats = field(default_factory=CacheStats)
    accommodation: CacheStats = field(default_factory=CacheStats)
    ground_transfer: CacheStats = field(default_factory=CacheStats)

    @property
    def total(self) -> CacheStats:
        return self.transport.merged(self.accommodation).merged(self.ground_transfer)

    @property
    def upstream_calls(self) -> int:
        """Requests that would actually have reached a real API."""
        return self.total.misses

    def as_dict(self) -> dict[str, float | int]:
        return {
            "lookups": self.total.lookups,
            "hits": self.total.hits,
            "misses": self.total.misses,
            "hit_rate": round(self.total.hit_rate, 4),
            "transport_lookups": self.transport.lookups,
            "transport_upstream": self.transport.misses,
            "accommodation_lookups": self.accommodation.lookups,
            "accommodation_upstream": self.accommodation.misses,
            "ground_transfer_lookups": self.ground_transfer.lookups,
            "ground_transfer_upstream": self.ground_transfer.misses,
        }


class CachingTransportProvider:
    """Memoizes ``search`` per ``(origin, destination, date)``."""

    def __init__(self, inner: TransportDataProvider) -> None:
        self.inner = inner
        self._cache: ProviderCache[tuple[str, str, date], list[TransportOption]] = (
            ProviderCache()
        )

    @property
    def stats(self) -> CacheStats:
        return self._cache.stats

    def search(
        self, origin: str, destination: str, departure_date: date
    ) -> list[TransportOption]:
        return self._cache.get_or_compute(
            (origin, destination, departure_date),
            lambda: self.inner.search(origin, destination, departure_date),
        )

    def __getattr__(self, name: str):
        # Forward anything the wrapped provider offers beyond the protocol,
        # so wrapping never removes capability.
        return getattr(self.inner, name)


class CachingAccommodationProvider:
    """Memoizes ``search`` per ``(city, check-in, check-out, travelers)``."""

    def __init__(self, inner: AccommodationDataProvider) -> None:
        self.inner = inner
        self._cache: ProviderCache[
            tuple[str, date, date, int], list[AccommodationOption]
        ] = ProviderCache()
        self._min_cache: ProviderCache[tuple[str, int], float | None] = ProviderCache()

    @property
    def stats(self) -> CacheStats:
        return self._cache.stats.merged(self._min_cache.stats)

    def search(
        self, city: str, check_in: date, check_out: date, travelers: int
    ) -> list[AccommodationOption]:
        return self._cache.get_or_compute(
            (city, check_in, check_out, travelers),
            lambda: self.inner.search(city, check_in, check_out, travelers),
        )

    def min_price_per_night(self, city: str, travelers: int) -> float | None:
        return self._min_cache.get_or_compute(
            (city, travelers), lambda: self.inner.min_price_per_night(city, travelers)
        )

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


class CachingGroundTransferProvider:
    """Memoizes ``search`` per ``(origin, airport)``."""

    def __init__(self, inner: GroundTransferProvider) -> None:
        self.inner = inner
        self._cache: ProviderCache[tuple[str, str], list[GroundTransferOption]] = (
            ProviderCache()
        )

    @property
    def stats(self) -> CacheStats:
        return self._cache.stats

    def search(self, origin: str, airport: str) -> list[GroundTransferOption]:
        return self._cache.get_or_compute(
            (origin, airport), lambda: self.inner.search(origin, airport)
        )

    def __getattr__(self, name: str):
        return getattr(self.inner, name)
