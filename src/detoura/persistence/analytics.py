"""Product-funnel analytics (V8.5 Phase C2).

Anonymous by construction. The ingest path accepts only a whitelisted set of
event names, a random per-tab session key, a random per-browser visitor key,
and a tiny props map that is scrubbed of anything PII-shaped. No traveller
name, email, phone, date of birth or free-form text is ever stored.

This exists to answer product questions - where the funnel leaks, whether
promos convert, Basic vs All-in-One selection - not to profile people.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .db import Database

#: The only event names accepted. Anything else is dropped at ingest.
EVENTS = (
    "SEARCH",
    "RESULT_VIEW",
    "TRIP_OPEN",
    "TIER_SELECTED",
    "TIER_SWITCHED",
    "REVIEW",
    "PROMO_APPLIED",
    "CONFIRM",
    "BOOKED",
    "FAILED",
    "CHECKOUT_ABANDONED",
    "PASS_VIEW",
)

#: The ordered funnel the dashboard renders.
FUNNEL = ("SEARCH", "RESULT_VIEW", "TRIP_OPEN", "TIER_SELECTED", "REVIEW",
          "CONFIRM", "BOOKED")

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
#: props values may only be short scalars from this safe set of keys.
_ALLOWED_PROP_KEYS = {
    "tier", "promo_code", "result_count", "rank", "search_mode", "profile",
    "budget_band", "duration_band", "ticket_count", "over_budget",
    "outcome", "reason", "repeat",
}
_PII_HINT = re.compile(
    r"(@|\+?\d{7,}|\bname\b|email|phone|dob|born|passport|address)", re.I
)


def _clean_props(props: Any) -> dict:
    if not isinstance(props, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in props.items():
        if k not in _ALLOWED_PROP_KEYS:
            continue
        if isinstance(v, bool) or isinstance(v, int):
            out[k] = v
        elif isinstance(v, (float,)):
            out[k] = round(v, 2)
        elif isinstance(v, str):
            s = v.strip()[:40]
            if s and not _PII_HINT.search(s):
                out[k] = s
    return out


def _safe_key(value: str) -> str:
    value = (value or "").strip()
    return value if _KEY_RE.match(value) else ""


def record(
    db: Database,
    *,
    event: str,
    session_key: str = "",
    visitor_key: str = "",
    tier: str = "",
    props: Any = None,
) -> bool:
    """Write one event if it is a known type. Returns whether it was kept."""
    if event not in EVENTS:
        return False
    tier = tier if tier in ("BASIC", "ALL_IN_ONE") else ""
    ts = datetime.now(timezone.utc).isoformat()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO analytics_events "
            "(ts, event, session_key, visitor_key, tier, props_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, event, _safe_key(session_key), _safe_key(visitor_key), tier,
             json.dumps(_clean_props(props), separators=(",", ":"))),
        )
    return True


def record_many(db: Database, batch: list[dict], *, session_key: str,
                visitor_key: str) -> int:
    kept = 0
    for e in batch[:50]:
        if record(
            db,
            event=str(e.get("event", "")),
            session_key=session_key,
            visitor_key=visitor_key,
            tier=str(e.get("tier", "")),
            props=e.get("props"),
        ):
            kept += 1
    return kept


# --- reporting -------------------------------------------------------------
def _window(since: datetime | None, until: datetime | None) -> tuple[str, list]:
    clauses, params = [], []
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since.isoformat())
    if until is not None:
        clauses.append("ts <= ?")
        params.append(until.isoformat())
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def event_counts(
    db: Database, *, since: datetime | None = None, until: datetime | None = None
) -> dict[str, int]:
    where, params = _window(since, until)
    rows = db.query(
        f"SELECT event, COUNT(*) AS n FROM analytics_events{where} GROUP BY event",
        tuple(params),
    )
    return {r["event"]: r["n"] for r in rows}


def funnel(
    db: Database, *, since: datetime | None = None, until: datetime | None = None
) -> list[dict]:
    """One row per funnel stage: distinct sessions that reached it, and the
    drop-off from the previous stage."""
    where, params = _window(since, until)
    rows = db.query(
        f"SELECT event, COUNT(DISTINCT session_key) AS s FROM analytics_events"
        f"{where} GROUP BY event",
        tuple(params),
    )
    by = {r["event"]: r["s"] for r in rows}
    out: list[dict] = []
    prev: int | None = None
    for stage in FUNNEL:
        reached = int(by.get(stage, 0))
        drop = None if prev is None else max(0, prev - reached)
        rate = None if not prev else round(reached / prev, 3)
        out.append({
            "stage": stage, "sessions": reached,
            "drop_from_prev": drop, "rate_from_prev": rate,
        })
        prev = reached
    return out


def tier_selection(
    db: Database, *, since: datetime | None = None, until: datetime | None = None
) -> dict[str, dict]:
    """Per tier: how often it was selected, and how many of those sessions
    went on to BOOKED (a rough conversion)."""
    where, params = _window(since, until)
    sel = db.query(
        f"SELECT tier, COUNT(DISTINCT session_key) AS n FROM analytics_events"
        f"{where}{' AND' if where else ' WHERE'} event = 'TIER_SELECTED'"
        f" AND tier != '' GROUP BY tier",
        tuple(params),
    )
    booked = db.query(
        f"SELECT tier, COUNT(DISTINCT session_key) AS n FROM analytics_events"
        f"{where}{' AND' if where else ' WHERE'} event = 'BOOKED'"
        f" AND tier != '' GROUP BY tier",
        tuple(params),
    )
    b = {r["tier"]: r["n"] for r in booked}
    out: dict[str, dict] = {}
    for r in sel:
        n = int(r["n"])
        conv = round(b.get(r["tier"], 0) / n, 3) if n else None
        out[r["tier"]] = {"selected": n, "booked": b.get(r["tier"], 0),
                          "conversion": conv}
    return out


def promo_impact(
    db: Database, *, since: datetime | None = None, until: datetime | None = None
) -> dict:
    where, params = _window(since, until)
    applied = db.query_one(
        f"SELECT COUNT(DISTINCT session_key) AS n FROM analytics_events"
        f"{where}{' AND' if where else ' WHERE'} event = 'PROMO_APPLIED'",
        tuple(params),
    )
    # sessions that applied a promo AND booked
    both = db.query_one(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT session_key FROM analytics_events WHERE event='PROMO_APPLIED'"
        "  INTERSECT"
        "  SELECT session_key FROM analytics_events WHERE event='BOOKED'"
        ")"
    )
    a = int(applied["n"]) if applied else 0
    return {
        "sessions_applied_promo": a,
        "of_those_booked": int(both["n"]) if both else 0,
        "conversion": round((both["n"] / a), 3) if a and both else None,
    }


def repeat_search_rate(
    db: Database, *, since: datetime | None = None, until: datetime | None = None
) -> dict:
    where, params = _window(since, until)
    rows = db.query(
        f"SELECT visitor_key, COUNT(*) AS n FROM analytics_events"
        f"{where}{' AND' if where else ' WHERE'} event = 'SEARCH'"
        f" AND visitor_key != '' GROUP BY visitor_key",
        tuple(params),
    )
    visitors = len(rows)
    repeat = sum(1 for r in rows if r["n"] > 1)
    avg = round(sum(r["n"] for r in rows) / visitors, 2) if visitors else 0.0
    return {
        "visitors_who_searched": visitors,
        "repeat_searchers": repeat,
        "repeat_rate": round(repeat / visitors, 3) if visitors else None,
        "avg_searches_per_visitor": avg,
    }


def recent_events(db: Database, *, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    rows = db.query(
        "SELECT ts, event, tier, props_json FROM analytics_events "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [
        {"ts": r["ts"], "event": r["event"], "tier": r["tier"],
         "props": json.loads(r["props_json"])}
        for r in rows
    ]
