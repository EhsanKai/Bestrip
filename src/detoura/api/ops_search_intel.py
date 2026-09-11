"""Ops-authenticated read access to Search Intelligence (V9 Phase 1).

Backend/API support for a future Search Intelligence Ops view — the view's
UI/UX is designed separately. Every route is behind ``require_ops``.

Nothing here exposes historical Price Memory as a current fare: every market
signal is returned with ``not_a_quote: true`` and amounts are labelled
"observed", never "price".
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from ..data.destinations import CORE_DESTINATIONS, DESTINATIONS, acquisition_catalog
from ..models.money import from_minor_units
from ..models.search_intel import MarketKey
from ..persistence import get_db
from ..persistence import market_priors as mpp
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
    stale = pm.prune_stale_provenance(db)
    return {
        "retention_days": cfg.retention_days,
        "observations_pruned": obs,
        "traces_pruned": tr,
        "stale_provenance_pruned": stale,
    }


# ======================================================================
# V9 Phase 2 — catalog + Bootstrap Market Prior observability (§41)
# ======================================================================
@router.get("/catalog")
def si_catalog(actor: str = Depends(require_ops)) -> dict:
    """Catalog stats only — no UI, no per-city personality scores. Confirms at
    a glance that acquisition is not still limited to the legacy 16 cities."""
    acq = acquisition_catalog()
    countries = {d.country_code for d in DESTINATIONS if d.country_code}
    subregions = {d.subregion for d in DESTINATIONS if d.subregion}
    by_subregion: dict[str, int] = {}
    for d in DESTINATIONS:
        key = d.subregion or "UNKNOWN"
        by_subregion[key] = by_subregion.get(key, 0) + 1
    return {
        "catalog_total": len(DESTINATIONS),
        "core_network_total": len(CORE_DESTINATIONS),
        "enabled_total": sum(1 for d in DESTINATIONS if d.enabled),
        "acquisition_eligible_total": len(acq),
        "countries_covered": len(countries),
        "subregions_covered": len(subregions),
        "by_subregion": by_subregion,
        "test_data": True,
    }


@router.get("/market-prior")
def si_market_prior(actor: str = Depends(require_ops)) -> dict:
    """Bootstrap Market Prior coverage and freshness — never a fare. Distinct
    from ``/traces`` and ``/markets``, which are live Price Memory."""
    db = get_db()
    cfg = search_intel_config()
    cov = mpp.coverage_summary(db)
    stale = mpp.stale_row_count(db, retention_days=cfg.prior_retention_days)
    return {
        "prior_enabled": cfg.prior_enabled,
        "retention_days": cfg.prior_retention_days,
        "decay_half_life_days": cfg.prior_decay_half_life_days,
        "coverage": cov,
        "stale_row_count": stale,
        "not_a_quote": True,
        "note": "Bootstrap Market Prior is approximate historical intelligence "
                "for acquisition scoring only — never a current fare or booking input.",
        "test_data": True,
    }


@router.get("/market-prior/imports")
def si_market_prior_imports(
    actor: str = Depends(require_ops),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    rows = mpp.recent_imports(get_db(), limit=limit)
    return {"imports": rows, "test_data": True}


@router.post("/market-prior/prune")
def si_market_prior_prune(actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    cfg = search_intel_config()
    deleted = mpp.prune(db, retention_days=cfg.prior_retention_days)
    return {"retention_days": cfg.prior_retention_days, "priors_pruned": deleted}


@router.get("/knowledge")
def si_knowledge(
    actor: str = Depends(require_ops),
    sample: int = Query(default=50, ge=1, le=500),
) -> dict:
    """LIVE / PRIOR / UNKNOWN candidate counts, aggregated from the funnel
    trace of the last ``sample`` searches (§20, §21). A search before Phase 2
    has no ``funnel`` field and is simply skipped, never reported as zero."""
    rows = pm.recent_traces(get_db(), limit=sample)
    totals = {"live": 0, "prior": 0, "unknown": 0, "shortlisted": 0,
              "exploit": 0, "explore": 0, "catalog_total": None}
    searches_with_funnel = 0
    for r in rows:
        try:
            body = json.loads(r.get("trace_json") or "{}")
        except (TypeError, ValueError):
            continue
        funnel = body.get("funnel") or {}
        if not funnel:
            continue
        searches_with_funnel += 1
        totals["live"] += funnel.get("live_history_known_count", 0) or 0
        totals["prior"] += funnel.get("prior_known_count", 0) or 0
        totals["unknown"] += funnel.get("fully_unknown_count", 0) or 0
        totals["shortlisted"] += funnel.get("shortlisted_total", 0) or 0
        totals["exploit"] += funnel.get("exploit_candidates", 0) or 0
        totals["explore"] += funnel.get("explore_candidates", 0) or 0
        totals["catalog_total"] = funnel.get("catalog_total", totals["catalog_total"])
    return {
        "searches_sampled": len(rows),
        "searches_with_funnel_trace": searches_with_funnel,
        "totals": totals,
        "test_data": True,
    }
