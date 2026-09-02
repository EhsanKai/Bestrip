"""Budget sensitivity analysis (V3).

Answers "what would another fifty euros buy me?" - which is often the most
useful thing a planner can tell someone, and which no single plan can express.

Deliberately built *around* the planner rather than inside it: one plan per
budget step, sharing the planner's warm provider caches. No second optimizer,
no special-cased search.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.itinerary import Itinerary
from ..models.trip import TripRequest
from ..profiles import ProfileName


@dataclass(frozen=True, slots=True)
class BudgetStep:
    """What one budget level makes possible."""

    budget: float
    feasible: bool
    best_score: float
    best_cost: float
    best_route: str
    city_sets: tuple[frozenset[str], ...]
    """The distinct city combinations available at this budget."""
    unlocked: tuple[frozenset[str], ...] = ()
    """City combinations that were not reachable at the previous step."""

    @property
    def is_threshold(self) -> bool:
        """True when this step made a genuinely new kind of trip possible."""
        return bool(self.unlocked)


@dataclass(frozen=True, slots=True)
class BudgetSensitivity:
    """The result of sweeping a request across several budgets."""

    profile: ProfileName
    currency: str
    steps: tuple[BudgetStep, ...]

    @property
    def thresholds(self) -> tuple[BudgetStep, ...]:
        """Only the steps where something new appeared."""
        return tuple(step for step in self.steps if step.is_threshold)

    @property
    def minimum_feasible_budget(self) -> float | None:
        for step in self.steps:
            if step.feasible:
                return step.budget
        return None

    def render(self) -> str:  # pragma: no cover - presentation only
        lines = [f"Budget sensitivity ({self.profile.value})", "-" * 34]
        for step in self.steps:
            if not step.feasible:
                lines.append(f"  {step.budget:>7.0f}  (nothing fits)")
                continue
            marker = "*" if step.is_threshold else " "
            lines.append(
                f" {marker}{step.budget:>7.0f}  {step.best_cost:>7.2f}  "
                f"score {step.best_score:.4f}  {step.best_route}"
            )
            for unlocked in step.unlocked:
                lines.append(f"            + unlocks {' + '.join(sorted(unlocked))}")
        return "\n".join(lines)


def analyze_budget_sensitivity(
    planner,
    request: TripRequest,
    *,
    budgets: list[float] | None = None,
    profile: ProfileName | None = None,
    steps: int = 5,
    span: float = 0.6,
) -> BudgetSensitivity:
    """Plan ``request`` at several budgets and report what each one unlocks.

    By default the sweep runs from ``budget * (1 - span/2)`` to
    ``budget * (1 + span/2)`` in ``steps`` even increments, which brackets what
    the traveler asked for. Pass ``budgets`` to control the levels exactly.

    Cost is ``steps`` planning runs. They share the planner's provider caches,
    so the marginal cost of each extra step is search, not I/O.
    """
    if budgets is None:
        if steps < 1:
            raise ValueError("steps must be >= 1")
        low = request.budget * (1.0 - span / 2.0)
        high = request.budget * (1.0 + span / 2.0)
        if steps == 1:
            budgets = [request.budget]
        else:
            increment = (high - low) / (steps - 1)
            budgets = [round(low + increment * index, 2) for index in range(steps)]
    budgets = sorted(set(budgets))

    active = profile or request.profile or planner.config.profile
    results: list[BudgetStep] = []
    seen: set[frozenset[str]] = set()

    for budget in budgets:
        plan = planner.plan(
            request.model_copy(update={"budget": budget}), profile=active
        )
        recommendations: list[Itinerary] = plan.recommendations
        if not recommendations:
            results.append(
                BudgetStep(
                    budget=budget, feasible=False, best_score=0.0, best_cost=0.0,
                    best_route="", city_sets=(),
                )
            )
            continue
        city_sets = tuple(
            dict.fromkeys(frozenset(i.cities) for i in recommendations)
        )
        unlocked = tuple(cities for cities in city_sets if cities not in seen)
        seen.update(city_sets)
        best = recommendations[0]
        results.append(
            BudgetStep(
                budget=budget,
                feasible=True,
                best_score=best.score,
                best_cost=best.total_cost,
                best_route=best.route_label(),
                city_sets=city_sets,
                unlocked=unlocked,
            )
        )

    return BudgetSensitivity(
        profile=ProfileName(active), currency=request.currency, steps=tuple(results)
    )
