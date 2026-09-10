"""Persistence for post-booking ticket operations (V8.5 C3).

One row per cancellation / change / recovery operation. The ``operation_id`` is
server-issued and also serves as the idempotency handle: an execute against a
row already in a terminal state returns the stored result rather than calling
the provider a second time.

No document data or unnecessary PII is written here - only ids, money, state
values and a free-text operator ``reason``.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from ..models.ticket_ops import OperationKind, TicketOperation
from .db import Database


def new_operation_id() -> str:
    return "op_" + secrets.token_urlsafe(16)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(
    db: Database,
    *,
    booking_id: str,
    sequence: int,
    kind: OperationKind,
    state: str,
    provider: str = "duffel",
    provider_order_id: str | None = None,
    reason: str = "",
    actor: str = "",
    idempotency_key: str = "",
) -> TicketOperation:
    op_id = new_operation_id()
    ts = _now()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO ticket_operations ("
            " operation_id, booking_id, sequence, kind, state, provider,"
            " provider_order_id, reason, actor, idempotency_key,"
            " created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (op_id, booking_id, sequence, kind.value, state, provider,
             provider_order_id, reason, actor, idempotency_key, ts, ts),
        )
    return get(db, op_id)  # type: ignore[return-value]


def update(
    db: Database,
    operation_id: str,
    *,
    state: str | None = None,
    quote: dict | None = None,
    result: dict | None = None,
    provider_order_id: str | None = None,
    reason: str | None = None,
) -> TicketOperation:
    sets: list[str] = ["updated_at = ?"]
    params: list = [_now()]
    if state is not None:
        sets.append("state = ?")
        params.append(state)
    if quote is not None:
        sets.append("quote_json = ?")
        params.append(json.dumps(quote, default=str, separators=(",", ":")))
    if result is not None:
        sets.append("result_json = ?")
        params.append(json.dumps(result, default=str, separators=(",", ":")))
    if provider_order_id is not None:
        sets.append("provider_order_id = ?")
        params.append(provider_order_id)
    if reason is not None:
        sets.append("reason = ?")
        params.append(reason)
    params.append(operation_id)
    with db.write() as conn:
        conn.execute(
            f"UPDATE ticket_operations SET {', '.join(sets)} WHERE operation_id = ?",
            tuple(params),
        )
    return get(db, operation_id)  # type: ignore[return-value]


def claim(db: Database, operation_id: str, *, from_state: str, to_state: str) -> bool:
    """Atomically move an operation ``from_state`` → ``to_state``. Returns True
    only for the caller that actually made the transition.

    Every write goes through the single serialised ``db.write()`` lock, so this
    conditional UPDATE is the claim primitive: two concurrent executes race
    here and exactly one wins, closing the check-then-act window before the
    provider is called.
    """
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE ticket_operations SET state = ?, updated_at = ? "
            "WHERE operation_id = ? AND state = ?",
            (to_state, _now(), operation_id, from_state),
        )
        return cur.rowcount == 1


def get(db: Database, operation_id: str) -> TicketOperation | None:
    row = db.query_one(
        "SELECT * FROM ticket_operations WHERE operation_id = ?", (operation_id,)
    )
    return _model(row) if row else None


def by_idempotency_key(db: Database, key: str) -> TicketOperation | None:
    if not key:
        return None
    row = db.query_one(
        "SELECT * FROM ticket_operations WHERE idempotency_key = ?", (key,)
    )
    return _model(row) if row else None


def for_booking(db: Database, booking_id: str) -> list[TicketOperation]:
    rows = db.query(
        "SELECT * FROM ticket_operations WHERE booking_id = ? "
        "ORDER BY created_at DESC",
        (booking_id,),
    )
    return [_model(r) for r in rows]


def latest_for_ticket(
    db: Database, booking_id: str, sequence: int, kind: OperationKind
) -> TicketOperation | None:
    row = db.query_one(
        "SELECT * FROM ticket_operations "
        "WHERE booking_id = ? AND sequence = ? AND kind = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (booking_id, sequence, kind.value),
    )
    return _model(row) if row else None


def _model(row) -> TicketOperation:
    return TicketOperation(
        operation_id=row["operation_id"],
        booking_id=row["booking_id"],
        sequence=row["sequence"],
        kind=OperationKind(row["kind"]),
        state=row["state"],
        provider=row["provider"],
        provider_order_id=row["provider_order_id"],
        reason=row["reason"],
        actor=row["actor"],
        idempotency_key=row["idempotency_key"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        quote_json=json.loads(row["quote_json"]) if row["quote_json"] else None,
        result_json=json.loads(row["result_json"]) if row["result_json"] else None,
    )
