"""Experience similarity between destinations (V9 Phase 3 §C3).

An **interpretable feature vector**, not an opaque embedding (§C3 explicitly
asks to avoid embeddings as the sole source of truth this phase): the 12
experience attributes every ``Destination`` already carries — history,
nature, nightlife, culture, food, architecture, shopping, museums, beaches,
family_friendly, romance, adventure (V1/V3) — read directly as the
"historic city / coastal / nightlife / food / art-culture / architecture /
nature / beach / ..." dimensions §C3 suggests. No new personality data is
invented; this module only measures distance between numbers the catalog
already states.

Cosine similarity: two destinations with the same *shape* of experience
profile (both strongly nightlife+food, say) read as similar regardless of
their absolute richness, which is exactly "these are the same kind of trip",
not "these are equally good trips" (a separate concept — attractiveness).
"""

from __future__ import annotations

import math
from typing import Sequence

from ..models.destination import EXPERIENCE_ATTRIBUTES, Destination


def _vector(destination: Destination) -> tuple[float, ...]:
    return tuple(getattr(destination, a) for a in EXPERIENCE_ATTRIBUTES)


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def experience_similarity(a: Destination, b: Destination) -> float:
    """``[0, 1]`` — 1.0 for an identical experience profile shape."""
    if a.id == b.id:
        return 1.0
    return cosine_similarity(_vector(a), _vector(b))


def experience_redundancy_signal(
    candidate: Destination,
    already_selected: Sequence[Destination],
    *,
    similarity_threshold: float,
) -> float:
    """How experience-redundant ``candidate`` is against a set already
    chosen, in ``[0, 1]``. Mirrors
    :func:`detoura.services.geo.geo_redundancy_signal`'s shape: sums a
    per-selection contribution (only similarity *above* the threshold counts,
    scaled by how far above), saturates, never a hard ban."""
    if not already_selected:
        return 0.0
    total = 0.0
    for other in already_selected:
        sim = experience_similarity(candidate, other)
        if sim > similarity_threshold:
            span = max(1e-9, 1.0 - similarity_threshold)
            total += (sim - similarity_threshold) / span
    return max(0.0, min(1.0, total / (total + 1.0)))


def mean_pairwise_experience_similarity(destinations: Sequence[Destination]) -> float | None:
    """Diagnostic metric (the Cologne benchmark's "experience-similarity
    concentration"). ``None`` for fewer than 2 destinations."""
    items = list(destinations)
    pairs = []
    for idx, a in enumerate(items):
        for b in items[idx + 1:]:
            pairs.append(experience_similarity(a, b))
    if not pairs:
        return None
    return round(sum(pairs) / len(pairs), 4)
