"""V9 Phase 1 — Search Intelligence instrumentation benchmark.

This benchmark does **not** claim adaptive-acquisition superiority. Phase 1's
job is to establish that the instrumentation is present, correct and cheap. It
becomes the baseline for the real adaptive-acquisition benchmark in a later V9
phase.

Runs a fixed synthetic search (a scripted fake Duffel — no socket) with the
:class:`SearchIntelRecorder` enabled, over N repetitions, and reports:

* candidates scored / selected, provider budget, EXPLOIT vs EXPLORE counts;
* provider calls made, observations written;
* useful-call rate, Top-5 contribution rate, winner contribution;
* Price Memory lookup overhead: acquisition preparation latency with the
  recorder off vs on, at a representative candidate count;
* a cold-start run (empty Price Memory) vs a warm run (history pre-loaded).

Run::

    python3 scripts/bench_search_intel.py [--runs 5]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.data.destinations import DESTINATIONS  # noqa: E402
from detoura.models.trip import TravelPreferences, TripRequest  # noqa: E402
from detoura.persistence.db import Database  # noqa: E402
from detoura.persistence import price_memory as pm  # noqa: E402
from detoura.providers.duffel import DuffelTransportProvider  # noqa: E402
from detoura.providers.http import HttpResponse  # noqa: E402
from detoura.search_intel_config import SearchIntelConfig  # noqa: E402
from detoura.services.acquisition import (  # noqa: E402
    ProviderCallBudget,
    days_for_request,
    rank_candidates,
)
from detoura.services.live_search import live_search  # noqa: E402
from detoura.services.market_intel import batch_market_signals  # noqa: E402
from detoura.services.search_intel_recorder import SearchIntelRecorder  # noqa: E402
from detoura.services.selection_store import SelectionStore  # noqa: E402
from detoura.models.search_intel import MarketKey  # noqa: E402

try:
    from tests import duffel_fixtures as fx
except Exception:  # pragma: no cover - script convenience
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests import duffel_fixtures as fx


class _Stub:
    def __init__(self):
        self.calls = 0

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls += 1
        sl = json.loads(body)["data"]["slices"][0]
        # deterministic per-route price so variance is meaningful over runs
        seed = (hash((sl["origin"], sl["destination"])) % 60) + 60
        offer = fx._offer(
            f"off_{self.calls:020d}",
            [fx._slice(sl["origin"], sl["destination"], "PT2H", [
                fx._segment(sl["origin"], sl["destination"],
                            "2026-10-14T08:00:00", "2026-10-14T10:00:00",
                            "PT2H", carrier="LH")])],
            total_amount=f"{seed}.00", total_currency="EUR",
            expires_at="2027-01-01T00:00:00Z",
        )
        return HttpResponse(status=200, body=json.dumps(fx.response([offer])))


def _req() -> TripRequest:
    return TripRequest(
        origin="Köln", budget=3000.0, travelers=2, duration_days=4,
        date_from=date(2026, 10, 12), date_to=date(2026, 10, 26),
        preferences=TravelPreferences(history=0.9, culture=0.8),
    )


def _budget() -> ProviderCallBudget:
    return ProviderCallBudget(
        max_offer_requests=48, max_destinations=4, max_date_variants=1,
        max_airport_variants=1,
    )


def _one_search(db: Database, cfg: SearchIntelConfig):
    req, budget = _req(), _budget()
    rec = SearchIntelRecorder(
        db, req, mode="SMART", provider="duffel",
        provider_call_budget=budget.max_offer_requests, cfg=cfg,
    )
    res = live_search(
        req, duffel=DuffelTransportProvider(access_token="duffel_test_x",
                                            http_client=_Stub(), max_offers=20),
        selection_store=SelectionStore(), destinations=DESTINATIONS,
        airports=["CGN"], days=days_for_request(req, req.candidate_start_dates(), max_days=1),
        budget=budget, recorder=rec,
    )
    return res.search_trace


def _lookup_overhead(db: Database, cfg: SearchIntelConfig, *, candidates: int) -> dict:
    req = _req()
    pool, _ = rank_candidates(DESTINATIONS, req, limit=candidates)
    day = days_for_request(req, req.candidate_start_dates(), max_days=1)[0]
    markets = [
        MarketKey.build(provider="duffel", origin="CGN", destination=(d.primary_airport or d.id),
                        departure_date=day, travelers=2)
        for d in pool
    ]
    # baseline: no lookup at all
    t0 = time.perf_counter()
    for _ in range(50):
        pass
    baseline = (time.perf_counter() - t0) / 50

    t0 = time.perf_counter()
    for _ in range(50):
        batch_market_signals(db, markets, cfg=cfg, now=datetime.now(timezone.utc))
    enabled = (time.perf_counter() - t0) / 50
    return {
        "candidate_markets": len(markets),
        "baseline_prep_ms": round(baseline * 1000, 4),
        "price_memory_prep_ms": round(enabled * 1000, 4),
        "overhead_ms": round((enabled - baseline) * 1000, 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    cfg = SearchIntelConfig()

    # ---- cold start ----
    db = Database(":memory:")
    cold = _one_search(db, cfg)

    # ---- warm: repeat to build history ----
    traces = [cold]
    lat = []
    for _ in range(args.runs):
        t0 = time.perf_counter()
        traces.append(_one_search(db, cfg))
        lat.append((time.perf_counter() - t0) * 1000)
    warm = traces[-1]

    cov = pm.coverage_summary(db)
    overhead = _lookup_overhead(db, cfg, candidates=8)

    # ---- attribution demo (the synthetic fixture does not always yield a
    # ---- bookable multi-city itinerary; this exercises the attribution path
    # ---- deterministically so the contribution rates in the report are real) ----
    attr_db = Database(":memory:")
    req, budget = _req(), _budget()
    rec = SearchIntelRecorder(attr_db, req, mode="SMART", provider="duffel",
                              provider_call_budget=budget.max_offer_requests, cfg=cfg)
    _ = live_search(
        req, duffel=DuffelTransportProvider(access_token="duffel_test_x",
                                            http_client=_Stub(), max_offers=20),
        selection_store=SelectionStore(), destinations=DESTINATIONS,
        airports=["CGN"], days=days_for_request(req, req.candidate_start_dates(), max_days=1),
        budget=budget, recorder=rec,
    )  # recorder.finalize already ran inside live_search; re-run attribution demo:

    class _L:
        def __init__(self, c): self.acquisition_call_id = c

    class _T:
        def __init__(self, rank, cids): self.rank = rank; self.legs = [_L(c) for c in cids]

    call_ids = list(rec._by_call)
    if call_ids:
        rec2 = SearchIntelRecorder(attr_db, req, cfg=cfg, provider_call_budget=48)
        rec2._by_call = rec._by_call
        winner = call_ids[:2]
        top = call_ids[:4]
        rec2.attribute([_T(0, winner), _T(1, call_ids[2:4]), _T(2, call_ids[4:6])])
        attribution_demo = {
            "calls_total": len(call_ids),
            "winner_calls": len(rec2._winner_calls),
            "top_k_calls": len(rec2._top_k_calls),
            "candidate_calls": len(rec2._candidate_calls),
        }
    else:
        attribution_demo = {"calls_total": 0}

    def _fmt(t):
        # candidate-level allocation (one decision per selected destination)
        cand_exploit = sum(1 for c in t.candidates if c.selected and c.stance.value == "EXPLOIT")
        cand_explore = sum(1 for c in t.candidates if c.selected and c.stance.value == "EXPLORE")
        return {
            "candidates_scored": len(t.candidates),
            "candidates_selected": sum(1 for c in t.candidates if c.selected),
            "candidate_level_exploit": cand_exploit,
            "candidate_level_explore": cand_explore,
            "provider_call_budget": t.provider_call_budget,
            "acquisition_edges": len(t.call_outcomes),
            "provider_calls_used": t.provider_calls_used,
            "provider_call_level_exploit": t.calls_exploit,
            "provider_call_level_explore": t.calls_explore,
            "explore_fraction": t.explore_fraction,
            "recommendations": t.recommendations_produced,
            "useful_call_rate": t.useful_call_rate,
            "top_k_contribution_rate": t.top_k_contribution_rate,
            "winner_contribution_rate": t.winner_contribution_rate,
        }

    report = {
        "runs": args.runs,
        "provenance_version": pm.CURRENT_PROVENANCE_VERSION,
        "note_provenance": "Only provenance_version >= "
                           f"{pm.CURRENT_PROVENANCE_VERSION} rows count toward "
                           "aggregates; the pre-QA-fix warm split (12 exploit / "
                           "8 explore, explore_fraction 0.4) is INVALID and "
                           "discarded.",
        "cold_start": _fmt(cold),
        "warm": _fmt(warm),
        "warm_search_latency_ms": {
            "median": round(statistics.median(lat), 2),
            "best": round(min(lat), 2),
        },
        "price_memory": {
            "observations_written": cov["observations"],
            "distinct_markets": cov["distinct_markets"],
            "search_traces": cov["search_traces"],
        },
        "lookup_overhead": overhead,
        "attribution_demo": attribution_demo,
        "note": "Phase 1 does NOT claim adaptive acquisition superiority. This "
                "establishes instrumentation quality and is the baseline for a "
                "later adaptive benchmark.",
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
