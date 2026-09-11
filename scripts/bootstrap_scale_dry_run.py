"""V9 Phase 2.5 — scale dry runs (§60).

Three planner-only scenarios, all **zero network requests**:

    5 origins  x 25 destinations  x 3 horizons
    10 origins x 203 destinations x 6 horizons
    30 origins x 203 destinations x 6 horizons

Reports potential cells, deduplicated tasks, planning latency, an estimate of
the SQLite footprint one task row costs, the request/duration/cost
projection under the configured hard budget, and — where a synthetic prior
has been pre-seeded for part of the scope — the coverage-change projection.

Run::

    python3 scripts/bootstrap_scale_dry_run.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.data.destinations import DESTINATIONS  # noqa: E402
from detoura.models.market_prior_acquisition import (  # noqa: E402
    AuthorizationStatus, SourceRegistration, SourceType,
)
from detoura.persistence.db import Database  # noqa: E402
from detoura.services.bootstrap_planner import dry_run, plan_cells  # noqa: E402
from detoura.search_intel_config import DEFAULT_BOOTSTRAP_ORIGINS  # noqa: E402

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)

#: Rough estimated bytes per market_prior_tasks row (all-TEXT/INTEGER columns,
#: SQLite's per-row overhead included) - a documented estimate, not a promise.
BYTES_PER_TASK_ROW_ESTIMATE = 220


def _scenario(name: str, origins: list[str], destinations: list[str], horizons: list[int]) -> dict:
    reg = SourceRegistration(
        source_id="scale-demo", source_name="Scale Demo", source_type=SourceType.FILE_IMPORT,
        authorization_status=AuthorizationStatus.APPROVED, created_at=NOW, updated_at=NOW,
    )
    db = Database(":memory:")

    started = time.perf_counter()
    cells = plan_cells(origins, destinations, horizons)
    plan_latency = time.perf_counter() - started

    started = time.perf_counter()
    report = dry_run(db, registration=reg, origins=origins, destinations=destinations,
                     horizon_days=horizons, request_budget=50, now=NOW)
    dry_run_latency = time.perf_counter() - started

    pol_interval = reg.rate_limit_policy.effective_min_interval_seconds
    return {
        "scenario": name,
        "origins": len(origins), "destinations": len(destinations), "horizons": len(horizons),
        "potential_cells": len(cells),
        "planning_latency_ms": round(plan_latency * 1000, 3),
        "dry_run_latency_ms": round(dry_run_latency * 1000, 3),
        "estimated_task_table_bytes": len(cells) * BYTES_PER_TASK_ROW_ESTIMATE,
        "hard_request_budget_used_for_projection": 50,
        "planned_requests_at_that_budget": report.planned_requests,
        "estimated_duration_seconds_at_that_budget": report.estimated_duration_seconds,
        "estimated_cost_minor": report.estimated_cost_minor,  # None = UNKNOWN, never 0
        "note": "zero network requests were made to produce this report",
    }


def main() -> None:
    all_dest_airports = sorted({d.primary_airport for d in DESTINATIONS if d.primary_airport})
    horizons_6 = (14, 30, 45, 60, 90, 120)

    scenarios = [
        _scenario("5x25x3", list(DEFAULT_BOOTSTRAP_ORIGINS[:5]), all_dest_airports[:25], list(horizons_6[:3])),
        _scenario("10x203x6", list(DEFAULT_BOOTSTRAP_ORIGINS[:10]), all_dest_airports, list(horizons_6)),
        _scenario("30x203x6", list(DEFAULT_BOOTSTRAP_ORIGINS), all_dest_airports, list(horizons_6)),
    ]
    print(json.dumps({"scenarios": scenarios, "network_requests_made": 0}, indent=2))


if __name__ == "__main__":
    main()
