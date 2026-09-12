"""Geographic distance and redundancy signals (V9 Phase 3 §C2).

Deliberately a **soft** signal only. Nothing here ever forbids a destination
for being close to the origin or to an already-selected candidate — it
produces a bounded, interpretable penalty a caller applies to a value score,
and every penalty here saturates well short of 1.0 (see
``SearchIntelConfig.geo_redundancy_penalty`` / ``portfolio_geo_penalty_scale``)
so a single exceptional nearby deal can never be reduced to worthless (§C5).

Straight-line (great-circle) distance, not road/rail/flight distance — a
deliberately simple, auditable proxy for "how far apart are these two
places", not a routing engine.
"""

from __future__ import annotations

import math
from typing import Sequence

from ..models.destination import Destination

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return EARTH_RADIUS_KM * 2 * math.asin(min(1.0, math.sqrt(a)))


def distance_km(a: Destination, b: Destination) -> float | None:
    """``None`` when either destination has no coordinates — never a fake 0
    (which would read as "identical location") or a fake large number."""
    if a.latitude is None or a.longitude is None or b.latitude is None or b.longitude is None:
        return None
    if a.id == b.id:
        return 0.0
    return haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)


def proximity_weight(distance_km_: float, *, km_scale: float) -> float:
    """``1.0`` at zero distance, smoothly decaying — ``exp(-d/scale)`` — never
    a hard cutoff (§C2: "do NOT introduce arbitrary rules such as destinations
    under 100 km are forbidden"). At ``distance == km_scale`` the weight is
    ``~0.37``; by ``3 * km_scale`` it is below ``0.05``."""
    if km_scale <= 0:
        return 0.0
    return math.exp(-max(0.0, distance_km_) / km_scale)


def geo_redundancy_signal(
    candidate: Destination,
    already_selected: Sequence[Destination],
    *,
    km_scale: float,
    free_allowance: int = 0,
) -> float:
    """How geographically redundant ``candidate`` is against a set already
    chosen, in ``[0, 1]``.

    Sums a smooth proximity weight against every already-selected
    destination (distinctive cities close to *many* selections build up more
    redundancy than one close to a single pick), subtracts a free allowance
    so a couple of close neighbours never trigger anything, and saturates at
    1.0 rather than growing unbounded. Missing coordinates contribute 0 (an
    unknown distance is never treated as "definitely far" or "definitely
    close") — this only ever reduces the signal, never inflates it, so it
    stays a safe default.
    """
    if not already_selected:
        return 0.0
    total = 0.0
    for other in already_selected:
        d = distance_km(candidate, other)
        if d is None:
            continue
        total += proximity_weight(d, km_scale=km_scale)
    total = max(0.0, total - free_allowance)
    return max(0.0, min(1.0, total / (total + 1.0)))


def country_diversity_ratio(destinations: Sequence[Destination]) -> float:
    """Distinct countries / total, in ``[0, 1]`` — a diagnostic metric (§C7,
    the Cologne benchmark's "country concentration"), never a selection rule.
    A quota would be exactly the "simplistic" mistake §C7 warns against."""
    if not destinations:
        return 0.0
    countries = {d.country_code or d.country for d in destinations}
    return round(len(countries) / len(destinations), 4)


def mean_pairwise_distance_km(destinations: Sequence[Destination]) -> float | None:
    """Diagnostic metric: average great-circle distance across every pair —
    the Cologne benchmark's "geographic concentration". ``None`` when fewer
    than 2 destinations have usable coordinates."""
    pairs = []
    items = list(destinations)
    for idx, a in enumerate(items):
        for b in items[idx + 1:]:
            d = distance_km(a, b)
            if d is not None:
                pairs.append(d)
    if not pairs:
        return None
    return round(sum(pairs) / len(pairs), 2)
