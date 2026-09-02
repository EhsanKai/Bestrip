"""Synthetic origin <-> airport ground transfers.

A real implementation would query public-transport routing. Here the common
origins are tabulated explicitly and anything else falls back to a distance
model, so the planner never silently assumes an airport is free to reach.

Transfers are modelled as symmetric: the trip home costs and takes the same as
the trip out.
"""

from __future__ import annotations

from ..models.transfer import GroundTransferMode, GroundTransferOption
from .destinations import ORIGIN_DISTANCES_KM, canonical_key

TRAIN = GroundTransferMode.TRAIN
BUS = GroundTransferMode.BUS

#: ``origin key -> airport -> (mode, price per person, minutes)``.
#: Köln's row is the spec's example: CGN is on the doorstep, DUS is a longer
#: and dearer ride, FRA longer still.
GROUND_TRANSFERS: dict[str, dict[str, tuple[GroundTransferMode, float, int]]] = {
    "koln": {
        "CGN": (TRAIN, 5.0, 20),
        "DUS": (TRAIN, 15.0, 55),
        "FRA": (TRAIN, 30.0, 90),
        "EIN": (BUS, 22.0, 130),
        "AMS": (TRAIN, 42.0, 175),
    },
    "dusseldorf": {
        "DUS": (TRAIN, 4.0, 15),
        "CGN": (TRAIN, 14.0, 50),
        "EIN": (BUS, 18.0, 100),
        "FRA": (TRAIN, 34.0, 105),
        "AMS": (TRAIN, 38.0, 150),
    },
    "frankfurt": {
        "FRA": (TRAIN, 5.0, 20),
        "CGN": (TRAIN, 30.0, 95),
        "DUS": (TRAIN, 34.0, 110),
        "EIN": (BUS, 45.0, 210),
        "AMS": (TRAIN, 55.0, 245),
    },
    "eindhoven": {
        "EIN": (BUS, 4.0, 15),
        "AMS": (TRAIN, 20.0, 90),
        "DUS": (TRAIN, 18.0, 100),
        "CGN": (TRAIN, 24.0, 135),
        "FRA": (TRAIN, 46.0, 215),
    },
    "amsterdam": {
        "AMS": (TRAIN, 5.0, 20),
        "EIN": (TRAIN, 20.0, 90),
        "DUS": (TRAIN, 36.0, 150),
        "CGN": (TRAIN, 42.0, 175),
        "FRA": (TRAIN, 58.0, 250),
    },
    "bonn": {
        "CGN": (TRAIN, 8.0, 35),
        "DUS": (TRAIN, 18.0, 70),
        "FRA": (TRAIN, 28.0, 85),
        "EIN": (BUS, 26.0, 150),
        "AMS": (TRAIN, 46.0, 195),
    },
    "aachen": {
        "CGN": (TRAIN, 12.0, 60),
        "DUS": (TRAIN, 16.0, 75),
        "EIN": (BUS, 14.0, 80),
        "FRA": (TRAIN, 38.0, 140),
        "AMS": (TRAIN, 40.0, 165),
    },
}

#: Fallback pricing when an origin/airport pair is not tabulated.
FALLBACK_PRICE_PER_KM = 0.14
FALLBACK_BASE_PRICE = 4.0
FALLBACK_BASE_MINUTES = 20
FALLBACK_MINUTES_PER_KM = 0.55


def _fallback(origin: str, airport: str) -> tuple[GroundTransferMode, float, int] | None:
    distances = ORIGIN_DISTANCES_KM.get(canonical_key(origin))
    if not distances or airport not in distances:
        return None
    km = distances[airport]
    return (
        TRAIN,
        round(FALLBACK_BASE_PRICE + km * FALLBACK_PRICE_PER_KM, 2),
        int(FALLBACK_BASE_MINUTES + km * FALLBACK_MINUTES_PER_KM),
    )


def build_options(
    origin: str,
    airport: str,
    *,
    table: dict[str, dict[str, tuple]] | None = None,
) -> list[GroundTransferOption]:
    """Ways of getting from ``origin`` to ``airport`` (and back).

    An injected ``table`` is authoritative: an origin/airport pair missing from
    it has no transfer, with no distance fallback. Test fixtures use that to pin
    a scenario's ground-transfer economics exactly.
    """
    source = GROUND_TRANSFERS if table is None else table
    row = source.get(canonical_key(origin), {})
    entry = row.get(airport)
    if entry is None and table is None:
        entry = _fallback(origin, airport)
    if entry is None:
        return []
    mode, price, minutes = entry
    return [
        GroundTransferOption(
            id=f"{canonical_key(origin)}-{airport}-{mode.value}",
            origin=origin,
            airport=airport,
            mode=mode,
            price_per_person=price,
            duration_minutes=minutes,
        )
    ]
