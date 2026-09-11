"""The sparse Bootstrap Acquisition planner (V9 Phase 2.5 §12, §13, §33).

Explicitly rejects the dense ``origin x destination x every calendar date``
matrix, exactly as Phase 2's Bootstrap Market Prior itself does. Planning
reasons over origins x destinations x a handful of horizon buckets — cheap
enough to reason about at ``30 origins x 203 destinations x 6 horizons ~=
36,540`` potential cells without ever implying they should all be executed
(§13). Horizon buckets here are the same +14/+30/+45/+60/+90/+120-day
planning intelligence Phase 2 already defines; they never become live Duffel
query dates on their own (§12, §26 - unchanged from Phase 2).

Two things this module produces:

* :func:`plan_cells` — the raw candidate cells for a scope (pure, no I/O).
* :func:`dry_run` — what a job *would* do: potential cells, how many are
  already freshly covered (deduplicated), how many tasks that leaves, how the
  hard request budget clamps them, and a cost/duration projection. Makes
  **zero** network requests and, notably, writes nothing either — a dry run
  is read-only by construction (§33).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from ..models.market_prior_acquisition import SourceRegistration, TaskCell
from ..persistence import market_prior_acquisition as store
from ..persistence.db import Database

#: How many days of source_date freshness counts as "already covered" for
#: dedup purposes - deliberately the same order of magnitude as
#: ``prior_decay_half_life_days`` (Phase 2 default 120d), so the planner does
#: not re-spend a request on a market whose prior is still meaningfully fresh.
DEFAULT_DEDUP_FRESHNESS_DAYS = 90


def plan_cells(
    origins: Sequence[str], destinations: Sequence[str], horizon_days: Sequence[int],
) -> list[TaskCell]:
    """Every ``(origin, destination, horizon)`` cell for a scope, skipping the
    degenerate origin==destination pair. A generator would also do here, but
    callers (dedup, persistence) all need the count and the sequence more
    than once, so a list is the honest cost."""
    origins_u = sorted({o.upper() for o in origins if o})
    dests_u = sorted({d.upper() for d in destinations if d})
    horizons = sorted({int(h) for h in horizon_days if h is not None and h >= 0})
    return [
        TaskCell(origin=o, destination=d, horizon_days=h)
        for o in origins_u for d in dests_u for h in horizons
        if o != d
    ]


@dataclass(frozen=True, slots=True)
class DryRunReport:
    source_id: str
    potential_cells: int
    already_covered: int
    eligible_tasks: int
    request_budget: int
    planned_requests: int
    """``min(eligible_tasks, request_budget)`` — what would actually run."""
    truncated_by_budget: int
    estimated_duration_seconds: float | None
    estimated_cost_minor: int | None
    """``None`` = UNKNOWN (the source has no configured per-request cost) —
    never reported as free (§34)."""
    currency: str | None

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "potential_cells": self.potential_cells,
            "already_covered": self.already_covered,
            "eligible_tasks": self.eligible_tasks,
            "request_budget": self.request_budget,
            "planned_requests": self.planned_requests,
            "truncated_by_budget": self.truncated_by_budget,
            "estimated_duration_seconds": self.estimated_duration_seconds,
            "estimated_cost_minor": self.estimated_cost_minor,
            "currency": self.currency,
            "network_requests_made": 0,
        }


def dry_run(
    db: Database, *, registration: SourceRegistration, origins: Sequence[str],
    destinations: Sequence[str], horizon_days: Sequence[int], request_budget: int,
    dedup_freshness_days: int = DEFAULT_DEDUP_FRESHNESS_DAYS, now: datetime | None = None,
) -> DryRunReport:
    """Zero network requests, zero writes — a pure read-and-compute answer to
    "what would this bootstrap do?" (§33)."""
    cells = plan_cells(origins, destinations, horizon_days)
    covered = store.already_fresh_cells(
        db, origin_airports=origins, destination_airports=destinations,
        horizon_days=horizon_days, freshness_days=dedup_freshness_days, now=now,
    )
    eligible = [c for c in cells if c.key not in covered]
    planned = min(len(eligible), max(request_budget, 0))
    truncated = len(eligible) - planned

    pol = registration.rate_limit_policy
    duration = planned * pol.effective_min_interval_seconds if planned else 0.0

    cost = None
    if registration.request_cost_minor is not None:
        cost = registration.request_cost_minor * planned

    return DryRunReport(
        source_id=registration.source_id,
        potential_cells=len(cells),
        already_covered=len(cells) - len(eligible),
        eligible_tasks=len(eligible),
        request_budget=request_budget,
        planned_requests=planned,
        truncated_by_budget=max(truncated, 0),
        estimated_duration_seconds=round(duration, 2) if planned else 0.0,
        estimated_cost_minor=cost,
        currency="EUR" if cost is not None else None,
    )
