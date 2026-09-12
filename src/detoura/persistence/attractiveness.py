"""Persistence for Destination Attractiveness profiles (V9 Phase 3 §B).

One row per ``(destination_id, model_version)``. Writing a new model version
never touches an old version's rows — ``get``/``batch_get`` always resolve a
specific version (the config's current one unless a caller asks otherwise),
so a running search never straddles two model versions and a stored profile's
meaning never silently changes underneath a caller that cached it.

Batch-oriented by design (§ Performance): a candidate funnel over the whole
~203-destination catalog does **one** query, never one row-lookup per
candidate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Sequence

from ..models.attractiveness import (
    DIMENSION_FIELDS,
    AttractivenessConfidence,
    AttractivenessProvenance,
    DestinationAttractivenessProfile,
)
from .db import Database

_COLUMNS = (
    "destination_id", "model_version", *DIMENSION_FIELDS,
    "aggregate_score", "confidence", "provenance", "source", "updated_at",
)


def _row_to_profile(row) -> DestinationAttractivenessProfile:
    kwargs = {name: row[name] for name in DIMENSION_FIELDS}
    return DestinationAttractivenessProfile(
        destination_id=row["destination_id"], model_version=row["model_version"],
        aggregate_score=row["aggregate_score"],
        confidence=AttractivenessConfidence(row["confidence"]),
        provenance=AttractivenessProvenance(row["provenance"]),
        source=row["source"] or "",
        updated_at=datetime.fromisoformat(row["updated_at"]),
        **kwargs,
    )


def upsert_profiles(
    db: Database, profiles: Sequence[DestinationAttractivenessProfile],
) -> tuple[int, int]:
    """Idempotent batch upsert, keyed on ``(destination_id, model_version)``.
    Returns ``(inserted, updated)``."""
    if not profiles:
        return 0, 0
    inserted = updated = 0
    with db.write() as conn:
        for p in profiles:
            existing = conn.execute(
                "SELECT 1 FROM destination_attractiveness"
                " WHERE destination_id=? AND model_version=?",
                (p.destination_id, p.model_version),
            ).fetchone()
            dims = tuple(getattr(p, name) for name in DIMENSION_FIELDS)
            if existing:
                conn.execute(
                    "UPDATE destination_attractiveness SET"
                    " sightseeing_score=?, culture_score=?, food_score=?,"
                    " nightlife_score=?, nature_score=?, uniqueness_score=?,"
                    " short_trip_score=?, experience_density_score=?,"
                    " aggregate_score=?, confidence=?, provenance=?, source=?,"
                    " updated_at=?"
                    " WHERE destination_id=? AND model_version=?",
                    (*dims, p.aggregate_score, p.confidence.value,
                     p.provenance.value, p.source, p.updated_at.isoformat(),
                     p.destination_id, p.model_version),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO destination_attractiveness ("
                    " destination_id, model_version, sightseeing_score,"
                    " culture_score, food_score, nightlife_score, nature_score,"
                    " uniqueness_score, short_trip_score,"
                    " experience_density_score, aggregate_score, confidence,"
                    " provenance, source, updated_at"
                    " ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p.destination_id, p.model_version, *dims, p.aggregate_score,
                     p.confidence.value, p.provenance.value, p.source,
                     p.updated_at.isoformat()),
                )
                inserted += 1
    return inserted, updated


def get_profile(
    db: Database, destination_id: str, *, model_version: int,
) -> DestinationAttractivenessProfile | None:
    row = db.query_one(
        "SELECT * FROM destination_attractiveness"
        " WHERE destination_id=? AND model_version=?",
        (destination_id, model_version),
    )
    return _row_to_profile(row) if row else None


def batch_get_profiles(
    db: Database, destination_ids: Iterable[str], *, model_version: int,
) -> dict[str, DestinationAttractivenessProfile]:
    """``{destination_id: profile}`` for whichever ids actually have a row at
    ``model_version`` — one query regardless of how many ids are asked for. A
    missing id is simply absent from the returned dict; the caller decides
    what UNKNOWN means (never silently a neutral 50 here)."""
    ids = sorted({d for d in destination_ids if d})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.query(
        f"SELECT * FROM destination_attractiveness"
        f" WHERE model_version=? AND destination_id IN ({marks})",
        (model_version, *ids),
    )
    return {r["destination_id"]: _row_to_profile(r) for r in rows}


def list_profiles(
    db: Database, *, model_version: int, limit: int = 500, offset: int = 0,
) -> list[DestinationAttractivenessProfile]:
    """Ops listing — paginated, deterministic order."""
    rows = db.query(
        "SELECT * FROM destination_attractiveness WHERE model_version=?"
        " ORDER BY destination_id LIMIT ? OFFSET ?",
        (model_version, limit, offset),
    )
    return [_row_to_profile(r) for r in rows]


def count_profiles(db: Database, *, model_version: int) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM destination_attractiveness WHERE model_version=?",
        (model_version,),
    )
    return int(row["n"]) if row else 0


def model_versions_present(db: Database) -> list[int]:
    rows = db.query(
        "SELECT DISTINCT model_version FROM destination_attractiveness"
        " ORDER BY model_version",
    )
    return [int(r["model_version"]) for r in rows]
