"""Destination metadata providers."""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from ..data.destinations import DESTINATIONS, canonical_key
from ..models.destination import Destination


@runtime_checkable
class DestinationProvider(Protocol):
    """Source of destination metadata."""

    def all(self) -> list[Destination]:
        """Every destination the planner may consider, in a stable order."""
        ...

    def get(self, destination_id: str) -> Destination | None:
        """Look up one destination, tolerating alternative spellings."""
        ...


class StaticDestinationProvider:
    """Serves the built-in synthetic destination catalog."""

    def __init__(self, destinations: Iterable[Destination] | None = None) -> None:
        source = list(destinations) if destinations is not None else list(DESTINATIONS)
        self._destinations = sorted(source, key=lambda d: d.id)
        self._index = {canonical_key(d.id): d for d in self._destinations}
        # Names may differ from ids in a richer catalog; index both.
        self._index.update({canonical_key(d.name): d for d in self._destinations})

    def all(self) -> list[Destination]:
        return list(self._destinations)

    def get(self, destination_id: str) -> Destination | None:
        return self._index.get(canonical_key(destination_id))

    def resolve(self, name: str) -> str | None:
        """Return the canonical destination id for a user-supplied name."""
        destination = self.get(name)
        return destination.id if destination else None

    def ids(self) -> list[str]:
        return [d.id for d in self._destinations]
