"""Deterministic Destination Attractiveness derivation (V9 Phase 3 §B2).

No new personality data is invented here. Every score is computed from the
catalog's own already-truthful ``Destination`` fields — the 12 hand-curated
or tag-derived experience attributes (V1/V3), ``recommended_min/max_days``
and ``richness`` (V9 Phase 2 §17) — reorganised into the 8 interpretable
attractiveness dimensions this phase asks for (§B1), with honest provenance
attached (§B2). This is the "deterministic curated/fixture baseline" the
spec allows when no authorized external data source exists — reporting that
limitation honestly rather than fabricating tourism statistics.

**Dimensions are not mathematically orthogonal by design.** A history-rich
city is *expected* to score well on both ``sightseeing`` and ``culture`` —
that overlap matches how a traveler actually experiences a place, and forcing
artificial independence would just relocate real correlation into a hidden
weighting instead of removing it.

## The 12 attributes -> 8 dimensions

    sightseeing_score          mean(history, architecture, museums)
    culture_score              culture
    food_score                 food
    nightlife_score            nightlife
    nature_score               mean(nature, beaches)
    short_trip_score           blend of "well-suited to 1-2 days"
                               (low recommended_min_days) and richness — a
                               destination that is merely quick, not also
                               good, does not score high here (§B4)
    experience_density_score   Destination.richness (already "how much is
                               there to do")
    uniqueness_score           this destination's Euclidean distance from
                               the *catalog's own* attribute centroid,
                               scaled — an honestly-computed distinctiveness
                               signal, never a popularity/fame proxy (§B3)

``uniqueness_score`` is the one dimension that is catalog-relative rather
than per-destination: it needs the population's centroid, computed once over
the whole catalog (O(N), not O(N²) — see the Performance requirement) via
:class:`CatalogAttractivenessContext`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..data.destinations import CORE_DESTINATIONS
from ..models.attractiveness import (
    CURRENT_MODEL_VERSION,
    AttractivenessConfidence,
    AttractivenessProvenance,
    DestinationAttractivenessProfile,
)
from ..models.destination import EXPERIENCE_ATTRIBUTES, Destination

_CORE_IDS: frozenset[str] = frozenset(d.id for d in CORE_DESTINATIONS)

#: Attribute weights per dimension. Kept as an explicit table (not scattered
#: arithmetic) so the mapping above is directly auditable against the code.
_DIMENSION_ATTRS: dict[str, tuple[str, ...]] = {
    "sightseeing_score": ("history", "architecture", "museums"),
    "culture_score": ("culture",),
    "food_score": ("food",),
    "nightlife_score": ("nightlife",),
    "nature_score": ("nature", "beaches"),
}


def _mean100(destination: Destination, attrs: tuple[str, ...]) -> float:
    values = [getattr(destination, a) for a in attrs]
    return round(100.0 * sum(values) / len(values), 2)


def _short_trip_score(destination: Destination) -> float:
    """Rewards a genuinely good SHORT trip, not merely a quick one (§B4).

    ``suitability`` alone would let a shallow but fast city (min_days=1,
    nothing to do) outscore an excellent one that rewards 2-3 days — the
    ``richness`` term is what stops that. Both terms in [0, 1] (min_days is
    already validated > 0; 4 days is treated as "no longer a short trip").
    """
    suitability = max(0.0, min(1.0, (4.0 - destination.recommended_min_days) / 3.0))
    return round(100.0 * (0.5 * suitability + 0.5 * destination.richness), 2)


@dataclass(frozen=True, slots=True)
class CatalogAttractivenessContext:
    """Precomputed catalog-wide statistics ``uniqueness_score`` needs.
    Built once per batch (O(N) over the catalog), never recomputed per
    destination — see the module docstring on avoiding O(N^2) over the full
    catalog."""

    centroid: dict[str, float]
    max_distance: float

    @classmethod
    def build(cls, catalog: list[Destination]) -> "CatalogAttractivenessContext":
        if not catalog:
            return cls(centroid={a: 0.5 for a in EXPERIENCE_ATTRIBUTES}, max_distance=1.0)
        centroid = {
            a: sum(getattr(d, a) for d in catalog) / len(catalog)
            for a in EXPERIENCE_ATTRIBUTES
        }
        distances = [_distance(d, centroid) for d in catalog]
        max_distance = max(distances) if distances else 1.0
        return cls(centroid=centroid, max_distance=max_distance or 1.0)


def _distance(destination: Destination, centroid: dict[str, float]) -> float:
    return sum(
        (getattr(destination, a) - centroid[a]) ** 2 for a in EXPERIENCE_ATTRIBUTES
    ) ** 0.5


def _uniqueness_score(destination: Destination, ctx: CatalogAttractivenessContext) -> float:
    d = _distance(destination, ctx.centroid)
    return round(100.0 * max(0.0, min(1.0, d / ctx.max_distance)), 2)


def derive_profile(
    destination: Destination,
    *,
    ctx: CatalogAttractivenessContext,
    model_version: int = CURRENT_MODEL_VERSION,
) -> DestinationAttractivenessProfile:
    """Deterministic — the same ``Destination`` + the same catalog context
    always produce the exact same profile (§ Persistence / Determinism)."""
    dims = {
        name: _mean100(destination, attrs)
        for name, attrs in _DIMENSION_ATTRS.items()
    }
    dims["short_trip_score"] = _short_trip_score(destination)
    dims["experience_density_score"] = round(100.0 * destination.richness, 2)
    dims["uniqueness_score"] = _uniqueness_score(destination, ctx)

    aggregate = round(sum(dims.values()) / len(dims), 2)

    is_core = destination.id in _CORE_IDS
    provenance = (
        AttractivenessProvenance.CURATED if is_core
        else AttractivenessProvenance.DERIVED
    )
    # A hand-authored 12-attribute profile is a stronger basis than a coarse
    # tag-derived one, so confidence tracks provenance rather than being
    # uniformly HIGH just because *a* number exists (§B2 — not presented as
    # more certain than it is).
    confidence = (
        AttractivenessConfidence.HIGH if is_core else AttractivenessConfidence.MEDIUM
    )
    source = (
        "catalog:CORE_DESTINATIONS (hand-authored attribute profile)"
        if is_core else "catalog:profile_from_tags (tag-derived attribute profile)"
    )

    return DestinationAttractivenessProfile(
        destination_id=destination.id, model_version=model_version,
        confidence=confidence, provenance=provenance, source=source,
        aggregate_score=aggregate, **dims,
    )
