"""Origin airport discovery.

"Köln" is a place, not an airport. The planner treats the origin as a *set* of
candidate departure nodes within a configurable radius, so a cheaper trip out of
Düsseldorf is discoverable without the user having to think of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..config import PlannerConfig
from ..data.destinations import (
    AIRPORT_INDEX,
    ORIGIN_AIRPORTS,
    ORIGIN_DISTANCES_KM,
    OriginAirport,
    canonical_key,
)
from .origin_intelligence import (
    NearbyAirportPolicy,
    OriginPlace,
    nearby_airports,
    resolve_origin_exact,
)


@dataclass(frozen=True, slots=True)
class OriginCandidate:
    """A departure node together with why it was offered."""

    code: str
    name: str
    city: str
    distance_km: float


@runtime_checkable
class OriginResolver(Protocol):
    """Turns a free-text origin into candidate departure nodes."""

    def resolve(self, origin: str, config: PlannerConfig) -> list[OriginCandidate]: ...


class StaticOriginResolver:
    """Resolves origins from the built-in distance table.

    A production implementation would geocode the origin and query airports
    within the radius; the interface would not change.
    """

    def __init__(
        self,
        airports: tuple[OriginAirport, ...] = ORIGIN_AIRPORTS,
        distances: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self._airports = {airport.code: airport for airport in airports}
        self._distances = distances if distances is not None else ORIGIN_DISTANCES_KM

    def resolve(self, origin: str, config: PlannerConfig) -> list[OriginCandidate]:
        """Departure nodes within ``config.max_origin_distance_km``, nearest first.

        Falls back to the single matching airport when the origin is given as an
        airport code we have no distance table for, and raises when nothing at
        all matches - guessing a departure city would be worse than failing.
        """
        key = canonical_key(origin)
        table = self._distances.get(key)

        if table is None:
            code = AIRPORT_INDEX.get(key)
            if code is None:
                raise ValueError(
                    f"unknown origin {origin!r}; known origins: "
                    f"{sorted(self._distances)} or airport codes {sorted(self._airports)}"
                )
            table = {code: 0.0}

        candidates = [
            OriginCandidate(
                code=code,
                name=self._airports[code].name,
                city=self._airports[code].city,
                distance_km=distance,
            )
            for code, distance in table.items()
            if code in self._airports and distance <= config.max_origin_distance_km
        ]
        candidates.sort(key=lambda c: (c.distance_km, c.code))
        selected = candidates[: config.max_origin_airports]
        if not selected:
            raise ValueError(
                f"no departure airport within {config.max_origin_distance_km} km of {origin!r}"
            )
        # Stable, code-sorted output keeps downstream iteration deterministic.
        return sorted(selected, key=lambda c: c.code)


class CatalogOriginResolver:
    """Resolves an origin against the full ~203-city discovery catalog
    (V9 Post-Phase-6 Origin Intelligence slice), instead of
    :class:`StaticOriginResolver`'s closed 7-city/5-airport table.

    This is the resolution the product problem statement asked for: any
    catalog city, alias or IATA code can be an origin, with nearby airports
    computed from real coordinates
    (:func:`~detoura.services.origin_intelligence.nearby_airports`) rather
    than a hand-maintained distance table. It implements the same
    :class:`OriginResolver` protocol and returns the same
    :class:`OriginCandidate` shape as :class:`StaticOriginResolver`, so
    :class:`~detoura.services.planner.TravelPlanner` needs no change at all
    to use it - only the resolver passed to its constructor differs.

    Typo matching never happens here (only exact/normalized/alias/airport-
    code matches resolve - see
    :func:`~detoura.services.origin_intelligence.resolve_origin_exact`), so
    swapping this in changes *which* origins are accepted, never silently
    *which* origin an ambiguous or misspelled input resolves to.
    """

    def __init__(self, places: tuple[OriginPlace, ...] | None = None) -> None:
        self._places = places

    def resolve(self, origin: str, config: PlannerConfig) -> list[OriginCandidate]:
        place = resolve_origin_exact(origin, places=self._places)
        if place is None:
            raise ValueError(
                f"unknown origin {origin!r}; try a city name, a common alias, "
                "or a 3-letter airport code"
            )
        policy = NearbyAirportPolicy(
            max_radius_km=config.max_origin_distance_km,
            max_candidates=config.max_origin_airports,
        )
        nearby = nearby_airports(
            place.latitude, place.longitude, places=self._places, policy=policy,
        )
        if not nearby:
            # Cannot happen in practice - a resolved place is always within
            # 0 km of itself - but a resolver must never return an empty
            # candidate list silently (mirrors StaticOriginResolver's own
            # guard for the equivalent case).
            raise ValueError(
                f"no departure airport within {config.max_origin_distance_km} km of {origin!r}"
            )
        return [
            OriginCandidate(code=a.code, name=a.name, city=a.city, distance_km=a.distance_km)
            for a in nearby
        ]
