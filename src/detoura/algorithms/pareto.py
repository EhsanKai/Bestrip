"""Pareto filtering.

Reduces a set of completed itineraries to the non-dominated ones. A candidate
is *dominated* when another candidate is no worse on every objective and
strictly better on at least one - such a candidate is inferior under any
weighting, so dropping it never removes a legitimate travel style.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Generic, Sequence, TypeVar

from .scoring import Objectives

T = TypeVar("T")

#: Comparison direction per objective: ``-1`` = lower is better.
#:
#: V3 widened the frontier: reducing a trip to cost and time alone collapses
#: exactly the trade-offs the product exists to surface, so usable time,
#: experience, accommodation quality and convenience are compared too.
OBJECTIVE_DIRECTIONS: dict[str, int] = {
    "cost": -1,
    "travel_minutes": -1,
    "city_count": +1,
    "preference_score": +1,
    "usable_minutes": +1,
    "experience": +1,
    "accommodation": +1,
    "convenience": +1,
}

#: Resolution below which two itineraries are "the same" on an objective.
#:
#: With eight objectives, strict domination almost never fires - any candidate
#: that is 0.001 better on one axis survives, and the frontier degenerates into
#: "everything". Quantizing each objective into boxes of a size a traveler could
#: actually notice restores the filter's job: 5 euros, half an hour of travel,
#: an hour of usable time. This is standard grid (epsilon) dominance, and unlike
#: naive epsilon-comparison it is transitive, so the frontier is well defined.
OBJECTIVE_RESOLUTION: dict[str, float] = {
    "cost": 5.0,
    "travel_minutes": 30.0,
    "city_count": 1.0,
    "preference_score": 0.02,
    "usable_minutes": 60.0,
    "experience": 0.02,
    "accommodation": 0.03,
    "convenience": 0.03,
}

_EPSILON = 1e-9


def _values(objectives: Objectives) -> dict[str, float]:
    return {
        "cost": objectives.cost,
        "travel_minutes": float(objectives.travel_minutes),
        "city_count": float(objectives.city_count),
        "preference_score": objectives.preference_score,
        "usable_minutes": float(objectives.usable_minutes),
        "experience": objectives.experience,
        "accommodation": objectives.accommodation,
        "convenience": objectives.convenience,
    }


def minimization_vector(
    objectives: Objectives, *, quantize: bool = True
) -> tuple[float, ...]:
    """The objective vector rewritten so that *lower is better* everywhere.

    Precomputing this once per candidate turns domination into an elementwise
    tuple comparison - rebuilding a dict inside the O(n^2) loop was, measurably,
    a fifth of a whole planning run. When ``quantize`` is set each objective is
    also snapped to its :data:`OBJECTIVE_RESOLUTION` box.
    """
    values = _values(objectives)
    vector = []
    for name, direction in OBJECTIVE_DIRECTIONS.items():
        value = values[name] * -direction
        if quantize:
            step = OBJECTIVE_RESOLUTION[name]
            value = math.floor(value / step) if step > 0 else value
        vector.append(float(value))
    return tuple(vector)


def _dominates_vector(left: tuple[float, ...], right: tuple[float, ...]) -> bool:
    strictly_better = False
    for a, b in zip(left, right):
        delta = a - b
        if delta > _EPSILON:
            return False
        if delta < -_EPSILON:
            strictly_better = True
    return strictly_better


def dominates(a: Objectives, b: Objectives) -> bool:
    """True when ``a`` dominates ``b``."""
    return _dominates_vector(minimization_vector(a), minimization_vector(b))


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
    vectors = [minimization_vector(objectives_of(item)) for item in items]
    frontier: list[T] = []
    dominated: list[tuple[T, T]] = []
    for index, item in enumerate(items):
        dominator_index: int | None = None
        for other_index in range(len(items)):
            if other_index == index:
                continue
            if _dominates_vector(vectors[other_index], vectors[index]):
                dominator_index = other_index
                break
        if dominator_index is None:
            frontier.append(item)
        else:
            dominated.append((item, items[dominator_index]))
    return ParetoResult(frontier=frontier, dominated=dominated)
