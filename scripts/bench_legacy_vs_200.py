"""V9 Phase 2 — Benchmark 3: legacy 16-city catalog vs the ~200-city catalog (§39).

The legacy catalog (the synthetic 16 :data:`CORE_DESTINATIONS`) structurally
cannot discover an opportunity that lives outside it - the destination is not
even a candidate. The expanded ~200-city catalog can discover the same
opportunity, given a Bootstrap Market Prior pointing at it, while the
:class:`ProviderCallBudget` stays identical in both runs: catalog expansion
buys *reach*, never a bigger provider-call ceiling (V9 §24).

Run::

    python3 scripts/bench_legacy_vs_200.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.data.destinations import CORE_DESTINATIONS, DESTINATIONS  # noqa: E402
from detoura.models.trip import TravelPreferences, TripRequest  # noqa: E402
from detoura.persistence.db import Database  # noqa: E402
from detoura.services.acquisition import ProviderCallBudget, build_plan  # noqa: E402
from detoura.services.candidate_funnel import run_funnel  # noqa: E402
from detoura.services.market_prior_import import run_import  # noqa: E402
from detoura.services.market_prior_source import FixtureMarketPriorSource  # noqa: E402

ORIGIN = "CGN"
SLOTS = 6
BUDGET = ProviderCallBudget(max_offer_requests=100, max_destinations=SLOTS,
                            max_date_variants=1, max_airport_variants=1)

# A destination present only in the expanded catalog, never in the legacy 16.
_CORE_IDS = {d.id for d in CORE_DESTINATIONS}
_OPPORTUNITY = next(
    d for d in DESTINATIONS if d.id not in _CORE_IDS and d.primary_airport
)


def _req() -> TripRequest:
    return TripRequest(
        origin="Köln", budget=3000.0, travelers=2, duration_days=4,
        date_from=date(2026, 10, 12), date_to=date(2026, 10, 26),
        preferences=TravelPreferences(shopping=0.9, museums=0.9),
    )


def _seed_priors(db: Database, catalog: list) -> None:
    # A representative baseline prior across the catalog (so the opportunity's
    # relative-attractiveness has something real to compare against), then a
    # deliberately cheap/confident record for the opportunity itself.
    baseline = [
        (ORIGIN, d.primary_airport) for d in catalog
        if d.primary_airport and d.id != _OPPORTUNITY.id
    ]
    if baseline:
        run_import(db, FixtureMarketPriorSource(markets=baseline, source_date=date(2026, 5, 1)))

    class _Opportunity:
        def meta(self):
            from detoura.services.market_prior_source import SourceMeta
            return SourceMeta(source="fixture", source_version="opportunity")

        def records(self):
            for hd in (14, 30):
                yield {
                    "origin_airport": ORIGIN,
                    "destination_airport": _OPPORTUNITY.primary_airport,
                    "horizon_days": hd, "currency": "EUR",
                    "sample_count": 40, "median_minor": 4200,
                    "observed_low_minor": 3600, "observed_high_minor": 5600,
                    "direct_possible": True, "weekly_frequency": 21,
                    "carrier_count": 3, "confidence": "HIGH",
                    "source_date": "2026-05-01",
                }

    run_import(db, _Opportunity())


def _run_funnel_over(catalog: list) -> dict:
    db = Database(":memory:")
    if any(d.id == _OPPORTUNITY.id for d in catalog):
        _seed_priors(db, catalog)
    result = run_funnel(
        db, _req(), catalog, origin_airports=[ORIGIN],
        departure_date=date(2026, 10, 14), slots=SLOTS,
    )
    chosen_ids = {d.id for d in result.chosen}
    return {
        "catalog_total": result.trace.catalog_total,
        "opportunity_in_catalog": any(d.id == _OPPORTUNITY.id for d in catalog),
        "opportunity_discovered": _OPPORTUNITY.id in chosen_ids,
        "shortlisted_total": result.trace.shortlisted_total,
        "chosen": sorted(chosen_ids),
    }


def _plan_edges_over(catalog: list) -> int:
    req = _req()
    plan = build_plan(req, destinations=catalog, airports=[ORIGIN],
                      days=[date(2026, 10, 14)], budget=BUDGET)
    return plan.planned_request_count


def main() -> None:
    legacy = _run_funnel_over(list(CORE_DESTINATIONS))
    expanded = _run_funnel_over(list(DESTINATIONS))

    report = {
        "opportunity_destination": _OPPORTUNITY.id,
        "budget": {
            "max_offer_requests": BUDGET.max_offer_requests,
            "max_destinations": BUDGET.max_destinations,
        },
        "legacy_16": legacy,
        "expanded_catalog": expanded,
        "provider_call_edges": {
            "legacy_16": _plan_edges_over(list(CORE_DESTINATIONS)),
            "expanded_catalog": _plan_edges_over(list(DESTINATIONS)),
        },
        "conclusion": (
            "legacy cannot discover it (not a candidate at all); expanded "
            "discovers it via its Market Prior"
            if (not legacy["opportunity_discovered"] and expanded["opportunity_discovered"])
            else "see raw fields - the opportunity did not need Phase 2 to surface "
                 "this run, or Phase 2 also missed it (re-check the prior seed)"
        ),
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
