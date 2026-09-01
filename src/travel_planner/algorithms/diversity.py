"""Route diversification.

Pareto filtering removes *inferior* itineraries; it happily keeps five
near-identical ones that differ only by departure airport or time of day. The
diversity filter makes the final list read as five genuinely different trips.

The rule is a greedy, deterministic maximal-marginal-relevance pass over the
ranked candidates: an itinerary is accepted only if its set of cities is
sufficiently different from every itinerary already accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Sequence, TypeVar

T = TypeVar("T")


def jaccard_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """``|a ∩ b| / |a ∪ b|`` - 1.0 for identical city sets, 0.0 for disjoint."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


@dataclass(frozen=True, slots=True)
class DiversityRejection(Generic[T]):
    """An itinerary dropped for being too similar to a better one."""

    item: T
    similar_to: T
    similarity: float


@dataclass(frozen=True, slots=True)
class DiversityResult(Generic[T]):
    selected: list[T]
    rejected: list[DiversityRejection[T]]


def diversify(
    items: Sequence[T],
    cities_of: Callable[[T], frozenset[str]],
    *,
    limit: int,
    similarity_threshold: float = 0.5,
) -> DiversityResult[T]:
    """Pick up to ``limit`` mutually dissimilar items from ranked ``items``.

    ``items`` must already be ordered best-first; the first item is always
    selected. If the threshold is too strict to fill ``limit`` slots, the
    remaining slots are filled from the rejected pool in rank order rather than
    returning a short list.
    """
    selected: list[T] = []
    rejected: list[DiversityRejection[T]] = []

    for item in items:
        if len(selected) >= limit:
            break
        cities = cities_of(item)
        closest: T | None = None
        closest_similarity = 0.0
        for chosen in selected:
            similarity = jaccard_similarity(cities, cities_of(chosen))
            if similarity > closest_similarity:
                closest, closest_similarity = chosen, similarity
        if closest is not None and closest_similarity > similarity_threshold:
            rejected.append(
                DiversityRejection(
                    item=item, similar_to=closest, similarity=round(closest_similarity, 4)
                )
            )
            continue
        selected.append(item)

    if len(selected) < limit and rejected:
        backfill = [rejection.item for rejection in rejected][: limit - len(selected)]
        chosen_ids = {id(item) for item in backfill}
        selected.extend(backfill)
        rejected = [r for r in rejected if id(r.item) not in chosen_ids]

    return DiversityResult(selected=selected, rejected=rejected)
