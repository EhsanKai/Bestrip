"""Admin audit trail (V8.5).

Every sensitive ops action - creating or disabling a promo, changing a markup
policy, and (from Phase B) cancelling or changing a booking - writes one row
here: who, what, which target, when, and a safe before/after snapshot. Secrets
and unnecessary PII are never part of that snapshot.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from .db import Database


class AuditEvent(BaseModel):
    id: int
    ts: datetime
    actor: str
    action: str
    target_type: str = ""
    target_id: str = ""
    before: dict | None = None
    after: dict | None = None
    note: str = ""


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str, separators=(",", ":"))


def record(
    db: Database,
    *,
    actor: str,
    action: str,
    target_type: str = "",
    target_id: str = "",
    before: dict | None = None,
    after: dict | None = None,
    note: str = "",
) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO audit_events "
            "(ts, actor, action, target_type, target_id, before_json, after_json, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, actor, action, target_type, target_id,
             _dump(before), _dump(after), note),
        )


def _row_to_event(row) -> AuditEvent:
    return AuditEvent(
        id=row["id"],
        ts=datetime.fromisoformat(row["ts"]),
        actor=row["actor"],
        action=row["action"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        before=json.loads(row["before_json"]) if row["before_json"] else None,
        after=json.loads(row["after_json"]) if row["after_json"] else None,
        note=row["note"],
    )


def recent(
    db: Database,
    *,
    limit: int = 100,
    target_type: str | None = None,
    target_id: str | None = None,
) -> list[AuditEvent]:
    limit = max(1, min(int(limit), 1000))
    sql = "SELECT * FROM audit_events"
    params: list[Any] = []
    clauses = []
    if target_type is not None:
        clauses.append("target_type = ?")
        params.append(target_type)
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(target_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [_row_to_event(r) for r in db.query(sql, tuple(params))]
