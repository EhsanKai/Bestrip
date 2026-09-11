"""V9 Phase 2 — Benchmark 1: candidate-funnel latency at catalog scale (§37).

Runs the full candidate funnel (eligibility -> feasibility -> Market Prior +
Price Memory batch lookup -> opportunity scoring -> diversity -> shortlist)
over increasing catalog sizes (16 / 50 / 100 / ~200) and reports:

* wall-clock latency per stage-equivalent (one ``run_funnel`` call is the whole
  pre-network pipeline; we also expose the DB roundtrip count directly);
* the number of SQL queries issued — the invariant under test is that this
  stays **flat** as the catalog grows, because Price Memory and Market Prior
  lookups are single batched queries (``IN (...)``), never one query per
  destination (V9 §10 "no N+1 across ~200 destinations").

Run::

    python3 scripts/bench_catalog_scale.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.data.destinations import DESTINATIONS  # noqa: E402
from detoura.models.trip import TravelPreferences, TripRequest  # noqa: E402
from detoura.persistence.db import Database  # noqa: E402
from detoura.services.candidate_funnel import run_funnel  # noqa: E402
from detoura.services.market_prior_import import run_import  # noqa: E402
from detoura.services.market_prior_source import FixtureMarketPriorSource  # noqa: E402

CATALOG_SIZES = (16, 50, 100, len(DESTINATIONS))


def _req() -> TripRequest:
    return TripRequest(
        origin="Köln", budget=3000.0, travelers=2, duration_days=4,
        date_from=date(2026, 10, 12), date_to=date(2026, 10, 26),
        preferences=TravelPreferences(history=0.8, culture=0.7, nature=0.5),
    )


def _catalog_of_size(n: int) -> list:
    # Deterministic slice, not a random sample - reproducible benchmark.
    return list(DESTINATIONS[: max(1, n)])


def _seed_prior(db: Database, catalog, origin: str) -> None:
    markets = tuple(
        (origin, d.primary_airport) for d in catalog
        if d.primary_airport and d.primary_airport != origin
    )
    run_import(db, FixtureMarketPriorSource(markets=markets, source_date=date(2026, 5, 1)))


def _counting_db(path: str = ":memory:") -> tuple[Database, dict]:
    db = Database(path)
    counters = {"queries": 0}
    original = db.query

    def _wrapped(sql, params=()):
        counters["queries"] += 1
        return original(sql, params)

    db.query = _wrapped  # type: ignore[method-assign]
    return db, counters


def _run_once(size: int) -> dict:
    catalog = _catalog_of_size(size)
    db, counters = _counting_db()
    _seed_prior(db, catalog, "CGN")
    req = _req()

    t0 = time.perf_counter()
    result = run_funnel(
        db, req, catalog, origin_airports=["CGN"],
        departure_date=date(2026, 10, 14), slots=8,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    return {
        "catalog_size": size,
        "eligible_total": result.trace.eligible_total,
        "feasible_total": result.trace.feasible_total,
        "scored_total": result.trace.scored_total,
        "shortlisted_total": result.trace.shortlisted_total,
        "sql_queries_issued": counters["queries"],
        "total_latency_ms": round(elapsed_ms, 3),
    }


def main() -> None:
    rows = [_run_once(n) for n in CATALOG_SIZES]
    # The invariant: query count must not scale with catalog size — the
    # largest catalog run must not issue meaningfully more SQL round-trips
    # than the smallest. A handful of fixed queries (eligibility is in-memory;
    # only Market Prior + Price Memory + contribution history hit the DB).
    query_counts = [r["sql_queries_issued"] for r in rows]
    n_plus_one = max(query_counts) > min(query_counts) + 3
    report = {
        "runs": rows,
        "no_n_plus_one": not n_plus_one,
        "note": "sql_queries_issued must stay flat (batched IN(...) lookups) "
                "as catalog_size grows from 16 to ~200 - this is the N+1 check.",
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
