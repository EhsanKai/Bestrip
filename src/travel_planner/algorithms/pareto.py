"""Pareto filtering.

Reduces a set of completed itineraries to the non-dominated ones. A candidate
is *dominated* when another candidate is no worse on every objective and
strictly better on at least one - such a candidate is inferior under any
weighting, so dropping it never removes a legitimate travel style.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Sequence, TypeVar

from .scoring import Objectives

T = TypeVar("T")

#: Comparison direction per objective: ``-1`` = lower is better.
OBJECTIVE_DIRECTIONS: dict[str, int] = {
    "cost": -1,
    "travel_minutes": -1,
    "city_count": +1,
    "preference_score": +1,
}

_EPSILON = 1e-9


def _values(objectives: Objectives) -> dict[str, float]:
    return {
        "cost": objectives.cost,
        "travel_minutes": float(objectives.travel_minutes),
        "city_count": float(objectives.city_count),
        "preference_score": objectives.preference_score,
    }


def dominates(a: Objectives, b: Objectives) -> bool:
    """True when ``a`` dominates ``b``."""
    left, right = _values(a), _values(b)
    strictly_better = False
    for name, direction in OBJECTIVE_DIRECTIONS.items():
        delta = (left[name] - right[name]) * direction
        if delta < -_EPSILON:
            return False
        if delta > _EPSILON:
            strictly_better = True
    return strictly_better


@dataclass(frozen=True, slots=True)
class ParetoResult(Generic[T]):
    """The frontier plus, for each removed item, what dominated it."""

    frontier: list[T]
    dominated: list[tuple[T, T]]
    """``(removed, dominator)`` pairs, in input order."""


def pareto_filter(
    items: Sequence[T],
    objectives_of: Callable[[T], Objectives],
) -> ParetoResult[T]:
    """Split ``items`` into the Pareto frontier and the dominated remainder.

    Input order is preserved, so the result is deterministic for a
    deterministic input.
    """
    vectors = [objectives_of(item) for item in items]
    frontier: list[T] = []
    dominated: list[tuple[T, T]] = []
    for index, item in enumerate(items):
        dominator_index: int | None = None
        for other_index in range(len(items)):
            if other_index == index:
                continue
            if dominates(vectors[other_index], vectors[index]):
                dominator_index = other_index
                break
        if dominator_index is None:
            frontier.append(item)
        else:
            dominated.append((item, items[dominator_index]))
    return ParetoResult(frontier=frontier, dominated=dominated)
