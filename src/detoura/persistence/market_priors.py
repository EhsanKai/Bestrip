"""Persistence for the Bootstrap Market Prior (V9 Phase 2).

Architecturally separate from :mod:`detoura.persistence.price_memory`:

* different table (``market_priors``) — bootstrap data is never inserted into
  ``price_observations``;
* different provenance — every row carries ``source`` + ``source_version``;
* different retention — ``MARKET_PRIOR_RETENTION_DAYS`` (default 365), and a
  prior prune touches **only** ``market_priors`` / ``market_prior_imports``.

**Never a quote.** Nothing here yields a bookable amount. The monetary columns
are the source's approximate historical statistics; commercial / booking /
revalidation code does not import this module (enforced by
``tests/test_v9_market_prior.py``).
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

from ..models.market_prior import BootstrapMarketPrior, PriorConfidence
from .db import Database

CURRENT_PRIOR_PROVENANCE_VERSION = 1


def new_prior_id() -> str:
    return "prior_" + secrets.token_urlsafe(14)


def new_import_id() -> str:
    return "imp_" + secrets.token_urlsafe(12)


def _d(v) -> str | None:
    return v.isoformat() if v else None


# ======================================================================
# Write
# ======================================================================
def upsert_priors(db: Database, priors: Sequence[BootstrapMarketPrior]) -> tuple[int, int]:
    """Idempotent batch upsert. Returns ``(inserted, updated)``.

    A re-import of the same logical row (same source + market + buckets +
    currency) **replaces** it rather than accumulating duplicates."""
    if not priors:
        return 0, 0
    inserted = updated = 0
    with db.write() as conn:
        for p in priors:
            existing = conn.execute(
                "SELECT prior_id FROM market_priors WHERE source=? AND origin_airport=?"
                " AND destination_airport=? AND season=? AND horizon_bucket=?"
                " AND weekday_class=? AND duration_bucket=? AND currency=?",
                (p.source, p.origin_airport, p.destination_airport, p.season.value,
                 p.horizon_bucket.value, p.weekday_class.value,
                 p.duration_bucket.value, p.currency),
            ).fetchone()
            row = (
                p.source, p.source_version, p.imported_at.isoformat(),
                _d(p.source_date), p.origin_airport, p.destination_airport,
                p.destination_id, p.season.value, p.month, p.horizon_bucket.value,
                p.weekday_class.value, p.duration_bucket.value, p.currency,
                p.sample_count, p.observed_low_minor, p.median_minor,
                p.typical_minor, p.observed_high_minor, p.confidence.value,
                None if p.direct_possible is None else int(p.direct_possible),
                p.weekly_frequency, p.carrier_count, p.provenance_version,
            )
            if existing:
                conn.execute(
                    "UPDATE market_priors SET source_version=?, imported_at=?,"
                    " source_date=?, destination_id=?, month=?, sample_count=?,"
                    " observed_low_minor=?, median_minor=?, typical_minor=?,"
                    " observed_high_minor=?, confidence=?, direct_possible=?,"
                    " weekly_frequency=?, carrier_count=?, provenance_version=?"
                    " WHERE prior_id=?",
                    (row[1], row[2], row[3], row[6], row[8], row[13], row[14],
                     row[15], row[16], row[17], row[18], row[19], row[20],
                     row[21], row[22], existing["prior_id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO market_priors ("
                    " prior_id, source, source_version, imported_at, source_date,"
                    " origin_airport, destination_airport, destination_id, season,"
                    " month, horizon_bucket, weekday_class, duration_bucket,"
                    " currency, sample_count, observed_low_minor, median_minor,"
                    " typical_minor, observed_high_minor, confidence,"
                    " direct_possible, weekly_frequency, carrier_count,"
                    " provenance_version"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p.prior_id, *row),
                )
                inserted += 1
    return inserted, updated


def record_import(
    db: Database, *, source: str, source_version: str, dry_run: bool,
    started_at: datetime, finished_at: datetime, metrics: dict,
    rejected: list[dict], ok: bool, error: str = "",
) -> str:
    import_id = new_import_id()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO market_prior_imports ("
            " import_id, source, source_version, started_at, finished_at, dry_run,"
            " rows_seen, rows_imported, rows_updated, rows_skipped_dup,"
            " rows_rejected, markets_covered, origins_covered,"
            " destinations_covered, source_requests, source_request_cost_minor,"
            " source_rate_limit_events, rejected_json, ok, error"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                import_id, source, source_version, started_at.isoformat(),
                finished_at.isoformat(), int(dry_run),
                metrics.get("rows_seen", 0), metrics.get("rows_imported", 0),
                metrics.get("rows_updated", 0), metrics.get("rows_skipped_dup", 0),
                metrics.get("rows_rejected", 0), metrics.get("markets_covered", 0),
                metrics.get("origins_covered", 0),
                metrics.get("destinations_covered", 0),
                metrics.get("source_requests"),
                metrics.get("source_request_cost_minor"),
                metrics.get("source_rate_limit_events"),
                json.dumps(rejected[:200], default=str, separators=(",", ":")),
                int(ok), error[:500],
            ),
        )
    return import_id


# ======================================================================
# Read — batch retrieval for a search (V9 §10: no N+1 across ~200 dests)
# ======================================================================
def priors_for_destinations(
    db: Database,
    *,
    origin_airports: Sequence[str],
    destination_airports: Sequence[str],
) -> dict[str, list[dict]]:
    """One query for every candidate destination from the given origins.
    Returns ``{destination_airport -> [prior rows]}``."""
    origins = [o.upper() for o in origin_airports if o]
    dests = [d.upper() for d in destination_airports if d]
    if not origins or not dests:
        return {}
    o_marks = ",".join("?" for _ in origins)
    d_marks = ",".join("?" for _ in dests)
    rows = db.query(
        f"SELECT * FROM market_priors WHERE origin_airport IN ({o_marks}) "
        f"AND destination_airport IN ({d_marks}) "
        "AND COALESCE(provenance_version, 1) >= ?",
        (*origins, *dests, CURRENT_PRIOR_PROVENANCE_VERSION),
    )
    out: dict[str, list[dict]] = {d: [] for d in dests}
    for r in rows:
        out.setdefault(r["destination_airport"], []).append(dict(r))
    return out


# ======================================================================
# Retention
# ======================================================================
def prune(db: Database, *, retention_days: int, now: datetime | None = None) -> int:
    """Delete priors older than ``retention_days`` (by ``source_date`` when
    present, else ``imported_at``). ``market_priors`` only — never
    price_observations / economics / audit."""
    if retention_days <= 0:
        return 0
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=retention_days))
    with db.write() as conn:
        cur = conn.execute(
            "DELETE FROM market_priors "
            "WHERE COALESCE(source_date, substr(imported_at, 1, 10)) < ?",
            (cutoff.date().isoformat(),),
        )
        return cur.rowcount


# ======================================================================
# Read — coverage / analytics (Ops)
# ======================================================================
def coverage_summary(db: Database) -> dict:
    row = db.query_one(
        "SELECT COUNT(*) AS rows,"
        " COUNT(DISTINCT origin_airport || '-' || destination_airport) AS markets,"
        " COUNT(DISTINCT origin_airport) AS origins,"
        " COUNT(DISTINCT destination_airport) AS destinations,"
        " MIN(source_date) AS oldest_source, MAX(source_date) AS newest_source,"
        " MAX(imported_at) AS last_import"
        " FROM market_priors WHERE COALESCE(provenance_version, 1) >= ?",
        (CURRENT_PRIOR_PROVENANCE_VERSION,),
    )
    by_source = {
        r["source"]: {"rows": r["n"], "version": r["v"]}
        for r in db.query(
            "SELECT source, COUNT(*) AS n, MAX(source_version) AS v "
            "FROM market_priors GROUP BY source"
        )
    }
    by_conf = {
        r["confidence"]: r["n"]
        for r in db.query(
            "SELECT confidence, COUNT(*) AS n FROM market_priors GROUP BY confidence"
        )
    }
    return {
        "rows": int(row["rows"]) if row else 0,
        "markets": int(row["markets"]) if row and row["markets"] else 0,
        "origins": int(row["origins"]) if row and row["origins"] else 0,
        "destinations": int(row["destinations"]) if row and row["destinations"] else 0,
        "oldest_source_date": row["oldest_source"] if row else None,
        "newest_source_date": row["newest_source"] if row else None,
        "last_import_at": row["last_import"] if row else None,
        "by_source": by_source,
        "by_confidence": by_conf,
    }


def recent_imports(db: Database, *, limit: int = 20) -> list[dict]:
    rows = db.query(
        "SELECT import_id, source, source_version, started_at, finished_at,"
        " dry_run, rows_seen, rows_imported, rows_updated, rows_rejected,"
        " markets_covered, origins_covered, destinations_covered,"
        " source_requests, source_request_cost_minor, source_rate_limit_events,"
        " ok, error"
        " FROM market_prior_imports ORDER BY started_at DESC LIMIT ?",
        (max(1, min(int(limit), 200)),),
    )
    return [dict(r) for r in rows]


def stale_row_count(db: Database, *, retention_days: int, now: datetime | None = None) -> int:
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=retention_days)).date().isoformat()
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM market_priors "
        "WHERE COALESCE(source_date, substr(imported_at,1,10)) < ?",
        (cutoff,),
    )
    return int(row["n"]) if row else 0
