"""Persistence for Price Memory and search traces (V9 Phase 1).

**Price Memory is not a cache.** It is the historical record of what markets
looked like. It survives cache expiry, process restarts and repository
recreation. It is written by the search-intelligence recorder and read only by
acquisition scoring, the aggregation service and Ops analytics — never by
checkout or revalidation.

Money is stored as integer minor units with an explicit currency. Retention is
by observation age; :func:`prune` removes stale observations and touches
nothing else — booking economics and audit rows are out of its reach by
construction (different tables, no cascade).
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

from ..models.search_intel import (
    MarketKey,
    PriceObservation,
    SearchModeTag,
    TripShape,
    travelers_bucket,
)
from ..models.search_trace import SearchIntelligenceTrace
from .db import Database


#: Observations written before the QA fix (return-leg stance mislabeled) carry
#: provenance_version 1. Intelligence aggregates and benchmarks read only rows
#: at or above this.
CURRENT_PROVENANCE_VERSION = 2


def new_observation_id() -> str:
    return "obs_" + secrets.token_urlsafe(16)


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _dt(v) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


# ======================================================================
# Write
# ======================================================================
def record_observations(db: Database, observations: Sequence[PriceObservation]) -> int:
    """Batch-insert observations in one transaction. Returns the count written.
    Idempotent on ``observation_id`` (INSERT OR IGNORE)."""
    if not observations:
        return 0
    rows = [
        (
            o.observation_id, o.observed_at.isoformat(), o.provider,
            o.origin.upper(), o.destination.upper(), o.departure_date.isoformat(),
            o.return_date.isoformat() if o.return_date else None,
            o.trip_shape.value, o.travelers,
            o.travelers_bucket or travelers_bucket(o.travelers),
            o.total_amount_minor, o.per_person_minor, o.currency.upper(),
            None if o.direct is None else int(o.direct), o.stops,
            o.marketing_carrier, o.operating_carrier, o.cabin,
            o.baggage_cabin, o.baggage_checked, o.offer_count_for_edge,
            o.search_id, o.acquisition_call_id, o.search_mode.value,
            o.candidate_reason[:200], int(o.exploration), o.candidate_rank,
            o.provider_call_ordinal, o.provider_call_budget,
            o.edge_kind, o.secondary_market,
            o.scoring_reference_date.isoformat() if o.scoring_reference_date else None,
            int(o.provenance_version),
            int(o.normalized_ok), int(o.retained_after_limits),
            int(o.entered_candidate_set), int(o.contributed_to_top_k),
            int(o.contributed_to_winner),
        )
        for o in observations
    ]
    with db.write() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO price_observations ("
            " observation_id, observed_at, provider, origin, destination,"
            " departure_date, return_date, trip_shape, travelers,"
            " travelers_bucket, total_amount_minor, per_person_minor, currency,"
            " direct, stops, marketing_carrier, operating_carrier, cabin,"
            " baggage_cabin, baggage_checked, offer_count_for_edge, search_id,"
            " acquisition_call_id, search_mode, candidate_reason, exploration,"
            " candidate_rank, provider_call_ordinal, provider_call_budget,"
            " edge_kind, secondary_market, scoring_reference_date,"
            " provenance_version,"
            " normalized_ok, retained_after_limits, entered_candidate_set,"
            " contributed_to_top_k, contributed_to_winner"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def update_contributions(
    db: Database,
    *,
    entered: Iterable[str] = (),
    top_k: Iterable[str] = (),
    winner: Iterable[str] = (),
) -> None:
    """Flag the outcome columns after the optimizer has run. Keyed by
    ``acquisition_call_id`` — every observation from a contributing call is
    marked. Called once per search, so at most 3 UPDATEs."""
    entered, top_k, winner = list(entered), list(top_k), list(winner)
    with db.write() as conn:
        for col, ids in (
            ("entered_candidate_set", entered),
            ("contributed_to_top_k", top_k),
            ("contributed_to_winner", winner),
        ):
            if not ids:
                continue
            marks = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE price_observations SET {col} = 1 "
                f"WHERE acquisition_call_id IN ({marks})",
                tuple(ids),
            )


def record_trace(db: Database, trace: SearchIntelligenceTrace) -> None:
    body = trace.model_dump(mode="json")
    with db.write() as conn:
        conn.execute(
            "INSERT INTO search_traces ("
            " search_id, started_at, finished_at, provider, origin, date_from,"
            " date_to, search_mode, travelers, provider_call_budget,"
            " provider_calls_used, provider_calls_failed, cache_hits,"
            " cache_misses, calls_explore, calls_exploit,"
            " recommendations_produced, useful_call_rate, top_k_contribution_rate,"
            " winner_contribution_rate, excess_search_cost_minor,"
            " economics_configured, trace_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (search_id) DO UPDATE SET "
            " finished_at=excluded.finished_at,"
            " provider_calls_used=excluded.provider_calls_used,"
            " provider_calls_failed=excluded.provider_calls_failed,"
            " cache_hits=excluded.cache_hits, cache_misses=excluded.cache_misses,"
            " calls_explore=excluded.calls_explore,"
            " calls_exploit=excluded.calls_exploit,"
            " recommendations_produced=excluded.recommendations_produced,"
            " useful_call_rate=excluded.useful_call_rate,"
            " top_k_contribution_rate=excluded.top_k_contribution_rate,"
            " winner_contribution_rate=excluded.winner_contribution_rate,"
            " excess_search_cost_minor=excluded.excess_search_cost_minor,"
            " economics_configured=excluded.economics_configured,"
            " trace_json=excluded.trace_json",
            (
                trace.search_id, trace.started_at.isoformat(),
                _iso(trace.finished_at), trace.provider, trace.origin.upper(),
                trace.date_from.isoformat(), trace.date_to.isoformat(),
                trace.search_mode.value, trace.travelers,
                trace.provider_call_budget, trace.provider_calls_used,
                trace.provider_calls_failed, trace.cache_hits, trace.cache_misses,
                trace.calls_explore, trace.calls_exploit,
                trace.recommendations_produced, trace.useful_call_rate,
                trace.top_k_contribution_rate, trace.winner_contribution_rate,
                trace.economics.estimated_excess_search_cost_minor,
                int(trace.economics.economics_configured),
                json.dumps(body, default=str, separators=(",", ":")),
            ),
        )


# ======================================================================
# Read — market observations (for aggregation)
# ======================================================================
def observations_for_markets(
    db: Database,
    markets: Sequence[MarketKey],
    *,
    since: datetime | None = None,
) -> dict[tuple, list[dict]]:
    """Batch lookup — one query for many markets, grouped by market tuple.
    Avoids N+1 across candidate markets (V9 §15)."""
    if not markets:
        return {}
    keys = {m.as_tuple() for m in markets}
    # Build an OR of (provider, origin, destination, departure_date, tb) tuples.
    where_parts, params = [], []
    for m in markets:
        where_parts.append(
            "(provider = ? AND origin = ? AND destination = ? "
            "AND departure_date = ? AND trip_shape = ? AND travelers_bucket = ?)"
        )
        params.extend([m.provider, m.origin, m.destination,
                       m.departure_date.isoformat(), m.trip_shape.value,
                       m.travelers_bucket])
    sql = ("SELECT * FROM price_observations WHERE ("
           + " OR ".join(where_parts) + ")"
           + " AND COALESCE(provenance_version, 1) >= ?")
    params.append(CURRENT_PROVENANCE_VERSION)
    if since is not None:
        sql += " AND observed_at >= ?"
        params.append(since.isoformat())
    sql += " ORDER BY observed_at DESC"
    grouped: dict[tuple, list[dict]] = {k: [] for k in keys}
    for r in db.query(sql, tuple(params)):
        k = (r["provider"], r["origin"], r["destination"], r["departure_date"],
             r["trip_shape"], r["travelers_bucket"])
        if k in grouped:
            grouped[k].append(dict(r))
    return grouped


def observations_for_market(
    db: Database, market: MarketKey, *, since: datetime | None = None,
) -> list[dict]:
    return observations_for_markets(db, [market], since=since).get(
        market.as_tuple(), []
    )


# ======================================================================
# Retention
# ======================================================================
def prune(db: Database, *, retention_days: int, now: datetime | None = None) -> int:
    """Delete observations older than ``retention_days``. Returns rows deleted.

    Only ``price_observations``. Booking economics and audit rows are in other
    tables and are never referenced here."""
    if retention_days <= 0:
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    with db.write() as conn:
        cur = conn.execute(
            "DELETE FROM price_observations WHERE observed_at < ?",
            (cutoff.isoformat(),),
        )
        n = cur.rowcount
    return n


def prune_stale_provenance(db: Database) -> int:
    """Delete observations written before the QA-fix provenance model
    (``provenance_version < CURRENT_PROVENANCE_VERSION``). Only
    ``price_observations`` — a Search-Intelligence-only cleanup. Returns rows
    deleted."""
    with db.write() as conn:
        cur = conn.execute(
            "DELETE FROM price_observations WHERE COALESCE(provenance_version, 1) < ?",
            (CURRENT_PROVENANCE_VERSION,),
        )
        return cur.rowcount


def prune_traces(db: Database, *, retention_days: int, now: datetime | None = None) -> int:
    if retention_days <= 0:
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    with db.write() as conn:
        cur = conn.execute(
            "DELETE FROM search_traces WHERE started_at < ?", (cutoff.isoformat(),)
        )
        return cur.rowcount


# ======================================================================
# Read — coverage / analytics (for Ops)
# ======================================================================
#: The provenance-version filter every intelligence read applies, as a SQL
#: fragment + the bind param, so the constant lives in exactly one place.
_FRESH_PROV = "COALESCE(provenance_version, 1) >= ?"


def coverage_summary(db: Database) -> dict:
    row = db.query_one(
        "SELECT COUNT(*) AS n, MIN(observed_at) AS oldest, MAX(observed_at) AS newest,"
        " COUNT(DISTINCT provider || origin || destination || departure_date || travelers_bucket) AS markets"
        f" FROM price_observations WHERE {_FRESH_PROV}",
        (CURRENT_PROVENANCE_VERSION,),
    )
    traces = db.query_one("SELECT COUNT(*) AS n FROM search_traces")
    return {
        "observations": int(row["n"]) if row else 0,
        "distinct_markets": int(row["markets"]) if row and row["markets"] else 0,
        "oldest_observation_at": row["oldest"] if row else None,
        "newest_observation_at": row["newest"] if row else None,
        "search_traces": int(traces["n"]) if traces else 0,
    }


def top_markets(db: Database, *, limit: int = 20) -> list[dict]:
    rows = db.query(
        "SELECT provider, origin, destination, departure_date, travelers_bucket,"
        " COUNT(*) AS samples, MAX(observed_at) AS freshest,"
        " AVG(CASE WHEN entered_candidate_set THEN 1.0 ELSE 0.0 END) AS candidate_rate,"
        " AVG(CASE WHEN contributed_to_top_k THEN 1.0 ELSE 0.0 END) AS top_k_rate,"
        " AVG(CASE WHEN contributed_to_winner THEN 1.0 ELSE 0.0 END) AS winner_rate"
        " FROM price_observations"
        f" WHERE {_FRESH_PROV}"
        " GROUP BY provider, origin, destination, departure_date, travelers_bucket"
        " ORDER BY samples DESC LIMIT ?",
        (CURRENT_PROVENANCE_VERSION, max(1, min(int(limit), 200))),
    )
    return [dict(r) for r in rows]


def recent_traces(db: Database, *, limit: int = 50) -> list[dict]:
    rows = db.query(
        "SELECT * FROM search_traces ORDER BY started_at DESC LIMIT ?",
        (max(1, min(int(limit), 500)),),
    )
    return [dict(r) for r in rows]


def get_trace(db: Database, search_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM search_traces WHERE search_id = ?", (search_id,))
    return dict(row) if row else None


def contribution_rollup(db: Database, *, since: datetime | None = None) -> dict:
    """Search-wide useful/top-K/winner rates over recent traces + observations."""
    clauses = [_FRESH_PROV]
    params: list = [CURRENT_PROVENANCE_VERSION]
    if since is not None:
        clauses.append("observed_at >= ?")
        params.append(since.isoformat())
    row = db.query_one(
        "SELECT COUNT(*) AS obs,"
        " AVG(CASE WHEN normalized_ok AND retained_after_limits THEN 1.0 ELSE 0.0 END) AS retained_rate,"
        " AVG(CASE WHEN entered_candidate_set THEN 1.0 ELSE 0.0 END) AS candidate_rate,"
        " AVG(CASE WHEN contributed_to_top_k THEN 1.0 ELSE 0.0 END) AS top_k_rate,"
        " AVG(CASE WHEN contributed_to_winner THEN 1.0 ELSE 0.0 END) AS winner_rate,"
        " AVG(CASE WHEN exploration THEN 1.0 ELSE 0.0 END) AS explore_rate"
        " FROM price_observations WHERE " + " AND ".join(clauses),
        tuple(params),
    )
    return {k: (float(v) if v is not None else None) for k, v in dict(row or {}).items()}
