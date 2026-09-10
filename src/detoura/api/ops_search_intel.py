"""Ops-authenticated read access to Search Intelligence (V9 Phase 1).

Backend/API support for a future Search Intelligence Ops view — the view's
UI/UX is designed separately. Every route is behind ``require_ops``.

Nothing here exposes historical Price Memory as a current fare: every market
signal is returned with ``not_a_quote: true`` and amounts are labelled
"observed", never "price".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from ..models.money import from_minor_units
from ..models.search_intel import MarketKey
from ..persistence import get_db
from ..persistence import price_memory as pm
from ..search_intel_config import search_intel_config
from ..services import provider_economics
from ..services.market_intel import market_signals
from .ops_auth import require_ops

router = APIRouter(prefix="/api/v1/ops/search-intel", tags=["ops", "search-intel"])


def _signal_dto(sig) -> dict:
    def _m(v):
        return None if v is None else round(from_minor_units(v), 2)
    return {
        "currency": sig.currency,
        "not_a_quote": True,
        "sample_count": sig.sample_count,
        "freshest_observation_at": sig.freshest_observation_at,
        "oldest_observation_at": sig.oldest_observation_at,
        "median_observed": _m(sig.median_observed_minor),
        "cheap_reference_observed": _m(sig.cheap_reference_minor),
        "expensive_reference_observed": _m(sig.expensive_reference_minor),
        "min_observed": _m(sig.min_observed_minor),
        "max_observed": _m(sig.max_observed_minor),
        "price_cv": sig.price_cv,
        "direct_sample_count": sig.direct_sample_count,
        "connecting_sample_count": sig.connecting_sample_count,
        "useful_offer_rate": sig.useful_offer_rate,
        "top_k_contribution_rate": sig.top_k_contribution_rate,
        "winner_contribution_rate": sig.winner_contribution_rate,
        "confidence": {
            "verdict": sig.confidence.verdict.value,
            "score": sig.confidence.score,
            "sample_component": sig.confidence.sample_component,
            "recency_component": sig.confidence.recency_component,
            "consistency_component": sig.confidence.consistency_component,
        },
    }


@router.get("/overview")
def si_overview(actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    cfg = search_intel_config()
    cov = pm.coverage_summary(db)
    roll = pm.contribution_rollup(db)
    econ = provider_economics.rolling_economics(db)
    return {
        "enabled": cfg.enabled,
        "retention_days": cfg.retention_days,
        "top_k": cfg.top_k,
        "coverage": cov,
        "contribution": roll,
        "exploration_rate": roll.get("explore_rate"),
        "economics": econ.as_dict(),
        "test_data": True,
    }


@router.get("/markets")
def si_markets(
    actor: str = Depends(require_ops),
    order: str = Query(default="samples", pattern="^(samples|weak)$"),
    limit: int = Query(default=25, ge=1, le=200),
) -> dict:
    db = get_db()
    rows = pm.top_markets(db, limit=200)
    for r in rows:
        r["freshest"] = r.get("freshest")
    if order == "weak":
        rows = sorted(rows, key=lambda r: (r["samples"], r.get("freshest") or ""))
    return {"markets": rows[:limit], "order": order, "test_data": True}


@router.get("/market")
def si_market(
    actor: str = Depends(require_ops),
    origin: str = Query(min_length=3, max_length=3),
    destination: str = Query(min_length=3, max_length=3),
    departure_date: str = Query(...),
    travelers: int = Query(default=1, ge=1, le=9),
    provider: str = Query(default="duffel"),
) -> dict:
    try:
        dep = date.fromisoformat(departure_date)
    except ValueError:
        raise HTTPException(status_code=422, detail={"message": "bad departure_date"})
    mk = MarketKey.build(
        provider=provider, origin=origin, destination=destination,
        departure_date=dep, travelers=travelers,
    )
    sigs = market_signals(get_db(), mk)
    return {
        "market": {
            "provider": mk.provider, "origin": mk.origin,
            "destination": mk.destination,
            "departure_date": mk.departure_date.isoformat(),
            "travelers_bucket": mk.travelers_bucket,
        },
        "not_a_quote": True,
        "note": "Historical market observation only — never a current fare and "
                "never usable for booking.",
        "signals_by_currency": {c: _signal_dto(s) for c, s in sigs.items()},
        "test_data": True,
    }


@router.get("/traces")
def si_traces(
    actor: str = Depends(require_ops),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    rows = pm.recent_traces(get_db(), limit=limit)
    for r in rows:
        r.pop("trace_json", None)
        if r.get("excess_search_cost_minor") is not None:
            r["excess_search_cost"] = round(
                from_minor_units(r["excess_search_cost_minor"]), 2
            )
    return {"traces": rows, "test_data": True}


@router.get("/traces/{search_id}")
def si_trace(search_id: str, actor: str = Depends(require_ops)) -> dict:
    import json

    row = pm.get_trace(get_db(), search_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"message": "No such search trace."})
    body = json.loads(row["trace_json"] or "{}")
    return {"search_id": search_id, "trace": body, "test_data": True}


@router.post("/prune")
def si_prune(actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    cfg = search_intel_config()
    obs = pm.prune(db, retention_days=cfg.retention_days)
    tr = pm.prune_traces(db, retention_days=cfg.retention_days)
    return {
        "retention_days": cfg.retention_days,
        "observations_pruned": obs,
        "traces_pruned": tr,
    }
