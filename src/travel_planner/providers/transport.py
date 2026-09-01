"""Transport data providers.

The optimizer only ever talks to :class:`TransportDataProvider`. Swapping the
synthetic network for a real API (Amadeus, Kiwi, Deutsche Bahn, ...) means
adding a new implementation of this protocol - no algorithm changes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable, Protocol, runtime_checkable

from ..data.synthetic_transport import (
    CONNECTIONS,
    NETWORK_END,
    NETWORK_START,
    Connection,
    build_options,
)
from ..models.transport import TransportOption


@runtime_checkable
class TransportDataProvider(Protocol):
    """Source of bookable transport legs."""

    def search(
        self,
        origin: str,
        destination: str,
        departure_date: date,
    ) -> list[TransportOption]:
        """Legs departing ``origin`` for ``destination`` on ``departure_date``.

        Implementations must return an empty list (never raise) when no
        connection exists, and must be deterministic for a given argument
        triple so that repeated planning runs agree.
        """
        ...


class SyntheticTransportDataProvider:
    """Serves the built-in synthetic European network.

    The whole timetable is materialized lazily per ``(origin, destination,
    date)`` key and cached, which keeps beam search fast without leaking the
    dataset's shape into the algorithms.
    """

    def __init__(
        self,
        connections: Iterable[Connection] | None = None,
        *,
        start_date: date = NETWORK_START,
        end_date: date = NETWORK_END,
        price_variation: bool = True,
    ) -> None:
        self._connections: dict[tuple[str, str], list[Connection]] = defaultdict(list)
        for connection in connections if connections is not None else CONNECTIONS:
            self._connections[(connection.origin, connection.destination)].append(connection)
        self._start_date = start_date
        self._end_date = end_date
        self._price_variation = price_variation
        self._cache: dict[tuple[str, str, date], list[TransportOption]] = {}
        self.search_calls = 0

    # -- TransportDataProvider ----------------------------------------
    def search(
        self,
        origin: str,
        destination: str,
        departure_date: date,
    ) -> list[TransportOption]:
        self.search_calls += 1
        key = (origin, destination, departure_date)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if not (self._start_date <= departure_date <= self._end_date):
            self._cache[key] = []
            return []
        options: list[TransportOption] = []
        for connection in self._connections.get((origin, destination), ()):
            options.extend(
                build_options(
                    connection, departure_date, price_variation=self._price_variation
                )
            )
        # Deterministic ordering: cheapest first, then earliest, then by id.
        options.sort(key=lambda o: (o.price_per_person, o.departure, o.id))
        self._cache[key] = options
        return options

    # -- Introspection -------------------------------------------------
    def destinations_from(self, origin: str) -> list[str]:
        """Nodes directly reachable from ``origin`` (synthetic network only)."""
        return sorted({dest for (src, dest) in self._connections if src == origin})

    @property
    def coverage(self) -> tuple[date, date]:
        return (self._start_date, self._end_date)


class RealTransportDataProvider:
    """Placeholder for a live provider.

    Intentionally unimplemented: the MVP must not make external calls and must
    not present invented numbers as real availability. The class exists so the
    seam between synthetic and real data is visible in the codebase.
    """

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError(
            "RealTransportDataProvider is a placeholder. The MVP runs entirely on "
            "SyntheticTransportDataProvider; wire a real API here to go live."
        )

    def search(
        self,
        origin: str,
        destination: str,
        departure_date: date,
    ) -> list[TransportOption]:  # pragma: no cover - never constructed
        raise NotImplementedError
