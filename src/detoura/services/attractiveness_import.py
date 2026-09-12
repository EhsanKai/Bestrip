"""Batch (re)computation of Destination Attractiveness profiles (V9 Phase 3 §B2).

The "import/update mechanism" the spec asks for. There is no external feed
this phase — this *is* the mechanism, run against the catalog itself, and it
is exactly how a future real data source would plug in: replace
``derive_profile`` (or blend its output with imported rows) behind the same
``seed_attractiveness`` entry point, bump ``CURRENT_MODEL_VERSION``, and old
rows stay put, untouched, at their old version.

Deterministic and offline — no network, no randomness — so re-running it
against an unchanged catalog is a no-op at the database level (every row's
values are byte-identical; only ``updated_at`` would differ, and unchanged
runs are skipped entirely below to keep that field meaningful).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models.attractiveness import CURRENT_MODEL_VERSION, DestinationAttractivenessProfile
from ..models.destination import Destination
from ..persistence import attractiveness as store
from ..persistence.db import Database
from .attractiveness_model import CatalogAttractivenessContext, derive_profile


@dataclass(slots=True)
class AttractivenessImportSummary:
    model_version: int
    catalog_total: int = 0
    computed: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    destination_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "model_version": self.model_version,
            "catalog_total": self.catalog_total,
            "computed": self.computed,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
        }


def _unchanged(new: DestinationAttractivenessProfile, old: DestinationAttractivenessProfile) -> bool:
    return (
        new.aggregate_score == old.aggregate_score
        and new.dimension_vector() == old.dimension_vector()
        and new.confidence == old.confidence
        and new.provenance == old.provenance
        and new.source == old.source
    )


def seed_attractiveness(
    db: Database,
    catalog: list[Destination],
    *,
    model_version: int = CURRENT_MODEL_VERSION,
    now: datetime | None = None,
) -> AttractivenessImportSummary:
    """Compute and upsert a :class:`~detoura.models.attractiveness.DestinationAttractivenessProfile`
    for every destination in ``catalog``, at ``model_version``.

    Idempotent: re-running against an unchanged catalog leaves every existing
    row's ``updated_at`` untouched (skipped, not rewritten) and reports
    ``unchanged`` rather than a false ``updated`` count."""
    now = now or datetime.now(timezone.utc)
    summary = AttractivenessImportSummary(model_version=model_version, catalog_total=len(catalog))
    if not catalog:
        return summary

    ctx = CatalogAttractivenessContext.build(catalog)
    existing = store.batch_get_profiles(
        db, [d.id for d in catalog], model_version=model_version,
    )

    to_write: list[DestinationAttractivenessProfile] = []
    for d in catalog:
        profile = derive_profile(d, ctx=ctx, model_version=model_version)
        summary.computed += 1
        old = existing.get(d.id)
        if old is not None and _unchanged(profile, old):
            summary.unchanged += 1
            continue
        to_write.append(profile.model_copy(update={"updated_at": now}))
        summary.destination_ids.append(d.id)

    inserted, updated = store.upsert_profiles(db, to_write)
    summary.inserted, summary.updated = inserted, updated
    return summary
