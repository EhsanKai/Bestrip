"""Origin <-> airport ground-transfer providers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..data.ground_transfers import build_options
from ..models.transfer import GroundTransferOption


@runtime_checkable
class GroundTransferProvider(Protocol):
    """Source of transfers between the user's origin and a departure airport."""

    def search(self, origin: str, airport: str) -> list[GroundTransferOption]:
        """Transfers for the pair, cheapest first; empty when none exist."""
        ...


class SyntheticGroundTransferProvider:
    """Serves the built-in synthetic transfer table."""

    def __init__(
        self,
        transfers: dict[str, dict[str, tuple]] | None = None,
    ) -> None:
        self._transfers = transfers
        self._cache: dict[tuple[str, str], list[GroundTransferOption]] = {}
        self.search_calls = 0

    def search(self, origin: str, airport: str) -> list[GroundTransferOption]:
        self.search_calls += 1
        key = (origin, airport)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        options = build_options(origin, airport, table=self._transfers)
        options.sort(key=lambda o: (o.price_per_person, o.duration_minutes, o.id))
        self._cache[key] = options
        return options


class FreeGroundTransferProvider:
    """Explicit "the airport is free to reach" provider - the V1 behaviour."""

    def search(self, origin: str, airport: str) -> list[GroundTransferOption]:
        return []


class RealGroundTransferProvider:
    """Placeholder for live public-transport routing. Deliberately unimplemented."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError(
            "RealGroundTransferProvider is a placeholder. The MVP runs entirely on "
            "SyntheticGroundTransferProvider; wire a routing API here to go live."
        )

    def search(  # pragma: no cover - never constructed
        self, origin: str, airport: str
    ) -> list[GroundTransferOption]:
        raise NotImplementedError
