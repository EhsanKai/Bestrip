"""Ops-authenticated read access to Destination Attractiveness (V9 Phase 3 §B, Ops).

Backend/API only — no consumer-facing route, no new UI. Every route is
behind ``require_ops``, matching the Search Intelligence Ops pattern
(``ops_search_intel.py``).

Exposes exactly what §Ops/Observability asks for: profiles, the current
model version, which destinations are missing/low-confidence, and a
catalog-vs-profile coverage summary — nothing consumer-facing (no ranking
badges, no scores in a search response).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..data.destinations import acquisition_catalog
from ..models.attractiveness import CURRENT_MODEL_VERSION
from ..persistence import attractiveness as store
from ..persistence import get_db
from ..search_intel_config import search_intel_config
from ..services.attractiveness_import import seed_attractiveness
from .ops_auth import require_ops

router = APIRouter(prefix="/api/v1/ops/attractiveness", tags=["ops", "attractiveness"])


def _profile_dto(p) -> dict:
    return {
        "destination_id": p.destination_id,
        "model_version": p.model_version,
        "sightseeing_score": p.sightseeing_score,
        "culture_score": p.culture_score,
        "food_score": p.food_score,
        "nightlife_score": p.nightlife_score,
        "nature_score": p.nature_score,
        "uniqueness_score": p.uniqueness_score,
        "short_trip_score": p.short_trip_score,
        "experience_density_score": p.experience_density_score,
        "aggregate_score": p.aggregate_score,
        "confidence": p.confidence.value,
        "provenance": p.provenance.value,
        "source": p.source,
        "updated_at": p.updated_at.isoformat(),
        "is_known": p.is_known,
    }


@router.get("/overview")
def attractiveness_overview(actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    cfg = search_intel_config()
    version = cfg.attractiveness_model_version
    catalog = list(acquisition_catalog())
    catalog_ids = {d.id for d in catalog}
    profiled_count = store.count_profiles(db, model_version=version)
    versions_present = store.model_versions_present(db)
    profiles = store.list_profiles(db, model_version=version, limit=100_000)
    profiled_ids = {p.destination_id for p in profiles}
    missing = sorted(catalog_ids - profiled_ids)
    orphaned = sorted(profiled_ids - catalog_ids)
    low_confidence = sorted(
        p.destination_id for p in profiles if p.confidence.value in ("NONE", "LOW")
    )
    by_provenance: dict[str, int] = {}
    for p in profiles:
        by_provenance[p.provenance.value] = by_provenance.get(p.provenance.value, 0) + 1
    return {
        "current_model_version": version,
        "code_default_model_version": CURRENT_MODEL_VERSION,
        "model_versions_present": versions_present,
        "catalog_total": len(catalog),
        "profiled_total": profiled_count,
        "missing_count": len(missing),
        "missing_destination_ids": missing,
        "orphaned_profile_count": len(orphaned),
        "orphaned_destination_ids": orphaned,
        "low_confidence_count": len(low_confidence),
        "low_confidence_destination_ids": low_confidence,
        "by_provenance": by_provenance,
    }


@router.get("/profiles")
def list_attractiveness_profiles(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    model_version: int | None = Query(default=None),
    actor: str = Depends(require_ops),
) -> dict:
    db = get_db()
    version = model_version or search_intel_config().attractiveness_model_version
    profiles = store.list_profiles(db, model_version=version, limit=limit, offset=offset)
    return {
        "model_version": version,
        "count": len(profiles),
        "profiles": [_profile_dto(p) for p in profiles],
    }


@router.get("/profiles/{destination_id}")
def get_attractiveness_profile(
    destination_id: str,
    model_version: int | None = Query(default=None),
    actor: str = Depends(require_ops),
) -> dict:
    db = get_db()
    version = model_version or search_intel_config().attractiveness_model_version
    profile = store.get_profile(db, destination_id, model_version=version)
    if profile is None:
        raise HTTPException(status_code=404, detail={"message": "No profile at this model version."})
    return _profile_dto(profile)


@router.post("/reseed")
def reseed_attractiveness(actor: str = Depends(require_ops)) -> dict:
    """Recompute every destination's profile from the current catalog at the
    current model version (§B2 import/update mechanism). Deterministic and
    offline — safe to call repeatedly; unchanged destinations are reported,
    never rewritten."""
    db = get_db()
    cfg = search_intel_config()
    catalog = list(acquisition_catalog())
    summary = seed_attractiveness(db, catalog, model_version=cfg.attractiveness_model_version)
    return summary.as_dict()
