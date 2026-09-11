"""Ops-authenticated Authorized Market-Prior Acquisition control (V9 Phase 2.5 §35).

Backend/API support only — no UI is designed here. Every route is behind
``require_ops``. Nothing here can be reached from the consumer search/booking
path (§28): this is exclusively an Ops job-control surface over the sources,
jobs and tasks introduced in Phase 2.5.

A caller can never specify a URL or fetcher — only an already-registered
``source_id`` (see :mod:`detoura.services.bootstrap_registry`). Starting or
resuming a job against a source with no registered fetcher, or one that is
not ``network_allowed`` for a network-capable type, fails closed with a 409,
never a best-effort guess.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..acquisition_config import ORIGIN_TIERS, acquisition_config
from ..models.market_prior_acquisition import (
    AuthorizationStatus,
    RateLimitPolicy,
    SourceHealth,
    SourceRegistration,
    SourceType,
)
from ..persistence import get_db
from ..persistence import market_prior_acquisition as store
from ..services.bootstrap_executor import run_job_slice
from ..services.bootstrap_planner import dry_run, plan_cells
from ..services.bootstrap_registry import ensure_default_sources, get_fetcher, has_fetcher
from .ops_auth import require_ops

router = APIRouter(prefix="/api/v1/ops/acquisition", tags=["ops", "market-prior-acquisition"])


def _source_dto(reg: SourceRegistration, db) -> dict:
    return {
        "source_id": reg.source_id, "source_name": reg.source_name,
        "source_type": reg.source_type.value,
        "authorization_status": reg.authorization_status.value,
        "authorization_basis": reg.authorization_basis,
        "allowed_scope": reg.allowed_scope,
        "commercial_reuse_status": reg.commercial_reuse_status,
        "persistence_allowed": reg.persistence_allowed,
        "base_domain": reg.base_domain,
        "rate_limit_policy": reg.rate_limit_policy.model_dump(),
        "adapter_version": reg.adapter_version,
        "reviewed_at": reg.reviewed_at, "reviewed_by": reg.reviewed_by,
        "robots_checked_at": reg.robots_checked_at, "robots_allowed": reg.robots_allowed,
        "request_cost_minor": reg.request_cost_minor,
        "network_allowed": reg.network_allowed,
        "has_fetcher_configured": has_fetcher(reg.source_id),
        "health": _source_health(reg, db).value,
        "created_at": reg.created_at, "updated_at": reg.updated_at,
    }


def _source_health(reg: SourceRegistration, db) -> SourceHealth:
    floor = reg.health_if_disabled_only
    if floor is not None:
        return floor
    recent = store.list_jobs(db, source_id=reg.source_id, limit=5)
    if not recent:
        return SourceHealth.HEALTHY
    reasons = {j["stopped_reason"] for j in recent if j.get("stopped_reason")}
    if "CAPTCHA_DETECTED" in reasons or "ACCESS_DENIED" in reasons or "DOMAIN_NOT_ALLOWED" in reasons:
        return SourceHealth.BLOCKED
    if "RATE_LIMITED" in reasons:
        return SourceHealth.RATE_LIMITED
    if "SOURCE_CHANGED" in reasons:
        return SourceHealth.SOURCE_CHANGED
    counters = [store.job_counters(db, j["job_id"]) for j in recent]
    total_attempted = sum(c["total_tasks"] - c["pending"] for c in counters)
    total_bad = sum(c["failed"] + c["blocked"] for c in counters)
    if total_attempted and total_bad / total_attempted > 0.5:
        return SourceHealth.DEGRADED
    return SourceHealth.HEALTHY


def _job_dto(job: dict, db) -> dict:
    return {**job, "counters": store.job_counters(db, job["job_id"])}


# ======================================================================
# Sources
# ======================================================================
@router.get("/sources")
def list_sources(actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    ensure_default_sources(db)
    return {"sources": [_source_dto(s, db) for s in store.list_sources(db)],
            "origin_tiers": {k: list(v) for k, v in ORIGIN_TIERS.items()}}


@router.get("/sources/{source_id}")
def get_source(source_id: str, actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    ensure_default_sources(db)
    reg = store.get_source(db, source_id)
    if reg is None:
        raise HTTPException(status_code=404, detail={"message": "No such source."})
    return _source_dto(reg, db)


class RegisterSourceRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=64)
    source_name: str = Field(min_length=1, max_length=200)
    source_type: SourceType
    authorization_basis: str = Field(default="", max_length=2000)
    allowed_scope: str = Field(default="", max_length=2000)
    commercial_reuse_status: str = Field(default="UNKNOWN", max_length=64)
    base_domain: str | None = None
    rate_limit_policy: RateLimitPolicy = Field(default_factory=RateLimitPolicy)
    adapter_version: str = Field(default="v1", max_length=32)
    request_cost_minor: int | None = None
    notes: str = Field(default="", max_length=4000)


@router.post("/sources")
def register_source(body: RegisterSourceRequest, actor: str = Depends(require_ops)) -> dict:
    """Registers or updates a source. **Fail closed**: authorization always
    starts/stays at ``REVIEW_REQUIRED`` here — only :func:`set_authorization`
    below can move it to ``APPROVED``, and that is a deliberate, separate,
    audited action (§4)."""
    db = get_db()
    now = datetime.now(timezone.utc)
    existing = store.get_source(db, body.source_id)
    status = existing.authorization_status if existing else AuthorizationStatus.REVIEW_REQUIRED
    reg = SourceRegistration(
        source_id=body.source_id, source_name=body.source_name, source_type=body.source_type,
        authorization_status=status,
        authorization_basis=body.authorization_basis, allowed_scope=body.allowed_scope,
        commercial_reuse_status=body.commercial_reuse_status, base_domain=body.base_domain,
        rate_limit_policy=body.rate_limit_policy, adapter_version=body.adapter_version,
        request_cost_minor=body.request_cost_minor, notes=body.notes,
        reviewed_at=existing.reviewed_at if existing else None,
        reviewed_by=existing.reviewed_by if existing else "",
        robots_checked_at=existing.robots_checked_at if existing else None,
        robots_allowed=existing.robots_allowed if existing else None,
        created_at=existing.created_at if existing else now, updated_at=now,
    )
    store.upsert_source(db, reg)
    return _source_dto(reg, db)


class SetAuthorizationRequest(BaseModel):
    status: AuthorizationStatus
    basis: str = Field(default="", max_length=2000)


@router.post("/sources/{source_id}/authorization")
def set_authorization(source_id: str, body: SetAuthorizationRequest,
                      actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    if store.get_source(db, source_id) is None:
        raise HTTPException(status_code=404, detail={"message": "No such source."})
    store.set_authorization(db, source_id, status=body.status, basis=body.basis, reviewed_by=actor)
    return _source_dto(store.get_source(db, source_id), db)


@router.get("/sources/{source_id}/health")
def source_health(source_id: str, actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    reg = store.get_source(db, source_id)
    if reg is None:
        raise HTTPException(status_code=404, detail={"message": "No such source."})
    return {"source_id": source_id, "health": _source_health(reg, db).value,
            "network_allowed": reg.network_allowed}


# ======================================================================
# Jobs — plan / dry-run / start / pause / resume / cancel / inspect
# ======================================================================
class PlanScopeRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=64)
    origins: list[str] = Field(min_length=1, max_length=200)
    destinations: list[str] = Field(min_length=1, max_length=500)
    horizon_days: list[int] = Field(min_length=1, max_length=12)
    request_budget: int | None = Field(default=None, ge=1)


@router.post("/jobs/dry-run")
def jobs_dry_run(body: PlanScopeRequest, actor: str = Depends(require_ops)) -> dict:
    """§33 — zero network requests, zero writes. Ops can ask "what would
    this bootstrap do?" before anything is created."""
    db = get_db()
    ensure_default_sources(db)
    reg = store.get_source(db, body.source_id)
    if reg is None:
        raise HTTPException(status_code=404, detail={"message": "No such source."})
    cfg = acquisition_config()
    budget = body.request_budget or cfg.max_requests_per_run
    report = dry_run(
        db, registration=reg, origins=body.origins, destinations=body.destinations,
        horizon_days=body.horizon_days, request_budget=budget,
        dedup_freshness_days=cfg.dedup_freshness_days,
    )
    return report.as_dict()


@router.post("/jobs")
def create_job(body: PlanScopeRequest, actor: str = Depends(require_ops)) -> dict:
    """Plans a bounded job: creates it and inserts its (deduplicated) task
    set. Does not start execution — call ``/jobs/{id}/start`` for that."""
    db = get_db()
    ensure_default_sources(db)
    reg = store.get_source(db, body.source_id)
    if reg is None:
        raise HTTPException(status_code=404, detail={"message": "No such source."})
    cfg = acquisition_config()
    budget = min(body.request_budget or cfg.max_requests_per_run, cfg.max_requests_per_run)
    job_id = store.create_job(
        db, source_id=body.source_id, origins=body.origins, destinations=body.destinations,
        horizon_days=body.horizon_days, request_budget=budget, dry_run=False,
    )
    cells = plan_cells(body.origins, body.destinations, body.horizon_days)
    plan_result = store.plan_tasks(db, job_id=job_id, source_id=body.source_id, cells=cells)
    return {"job": _job_dto(store.get_job(db, job_id), db), "plan": plan_result}


@router.get("/jobs")
def list_jobs(actor: str = Depends(require_ops), source_id: str | None = Query(default=None),
             limit: int = Query(default=50, ge=1, le=500)) -> dict:
    db = get_db()
    jobs = store.list_jobs(db, source_id=source_id, limit=limit)
    return {"jobs": [_job_dto(j, db) for j in jobs]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    job = store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"message": "No such job."})
    return _job_dto(job, db)


@router.get("/jobs/{job_id}/tasks")
def list_job_tasks(job_id: str, actor: str = Depends(require_ops),
                   status: str | None = Query(default=None),
                   limit: int = Query(default=200, ge=1, le=2000)) -> dict:
    db = get_db()
    if store.get_job(db, job_id) is None:
        raise HTTPException(status_code=404, detail={"message": "No such job."})
    return {"tasks": store.list_tasks(db, job_id, status=status, limit=limit)}


class RunSliceRequest(BaseModel):
    max_tasks: int = Field(default=25, ge=1, le=500)


def _start_or_resume(job_id: str, body: RunSliceRequest, actor: str) -> dict:
    db = get_db()
    job = store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"message": "No such job."})
    reg = store.get_source(db, job["source_id"])
    if reg is None:
        raise HTTPException(status_code=404, detail={"message": "Job's source no longer exists."})
    fetcher = get_fetcher(reg, db=db, job_id=job_id)
    if fetcher is None:
        # Fail closed - never guess a fetch strategy for an unwired source.
        raise HTTPException(status_code=409, detail={
            "message": f"No fetcher is configured for source {reg.source_id!r}. "
                       "See docs/V9_MARKET_PRIOR_SOURCE_OPTIONS.md.",
        })
    cfg = acquisition_config()
    result = run_job_slice(
        db, job_id, fetcher=fetcher, source=reg, max_tasks=body.max_tasks,
        lease_seconds=cfg.task_lease_seconds,
    )
    return {"job": _job_dto(store.get_job(db, job_id), db), "result": result.as_dict()}


@router.post("/jobs/{job_id}/start")
def start_job(job_id: str, body: RunSliceRequest = RunSliceRequest(),
             actor: str = Depends(require_ops)) -> dict:
    return _start_or_resume(job_id, body, actor)


@router.post("/jobs/{job_id}/resume")
def resume_job(job_id: str, body: RunSliceRequest = RunSliceRequest(),
               actor: str = Depends(require_ops)) -> dict:
    return _start_or_resume(job_id, body, actor)


@router.post("/jobs/{job_id}/pause")
def pause_job(job_id: str, actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    job = store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"message": "No such job."})
    store.set_job_status(db, job_id, "PAUSED", stopped_reason="paused by operator")
    return _job_dto(store.get_job(db, job_id), db)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    job = store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"message": "No such job."})
    cancelled = store.cancel_pending(db, job_id)
    store.set_job_status(db, job_id, "CANCELLED", stopped_reason="cancelled by operator")
    return {"job": _job_dto(store.get_job(db, job_id), db), "pending_tasks_cancelled": cancelled}
