"""Persistence for the communication domain (V9 Phase 5).

Every write that changes a communication's state goes through a
compare-and-swap on ``version`` (never a read-then-write race) and, for
irreversible steps, an accompanying insert into the append-only
``communication_events`` table in the SAME transaction - so the ledger
and the current-state row can never disagree about whether a step happened.

Idempotent creation (communications) relies on the UNIQUE constraint on
``(booking_id, communication_type)``: a retried create is a single INSERT
attempt that either succeeds once or fails with ``IntegrityError``, at which
point the existing row - not a fresh one - is returned. This is the same
pattern ``persistence/payments.py`` uses for duplicate payment creation.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone

from ..models.communication import (
    CommunicationAttempt,
    CommunicationChannel,
    CommunicationEvent,
    CommunicationStatus,
    CommunicationType,
    CustomerCommunication,
)
from .db import Database


# ======================================================================
# Schema addition for Agent 6 to integrate centrally
# ======================================================================
"""
# --- Phase 5 schema addition (for Agent 6 to add to persistence/db.py's central migration - do NOT edit db.py yourself) ---

CREATE TABLE IF NOT EXISTS customer_communications (
    communication_id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL,
    journey_reference TEXT NOT NULL,
    user_id TEXT,
    channel TEXT NOT NULL,  -- EMAIL, SMS (future), PUSH (future)
    communication_type TEXT NOT NULL,  -- BOOKING_CONFIRMATION, etc.
    status TEXT NOT NULL,  -- PENDING, SENDING, SENT, FAILED, UNKNOWN
    recipient_address TEXT NOT NULL,  -- Email, phone, or device token
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(booking_id, communication_type)
);
CREATE INDEX IF NOT EXISTS idx_communications_booking
    ON customer_communications(booking_id);
CREATE INDEX IF NOT EXISTS idx_communications_user
    ON customer_communications(user_id);

CREATE TABLE IF NOT EXISTS communication_attempts (
    attempt_id TEXT PRIMARY KEY,
    communication_id TEXT NOT NULL REFERENCES customer_communications(communication_id),
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,  -- PENDING, SENDING, SENT, FAILED, UNKNOWN
    provider_name TEXT NOT NULL,
    provider_message_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(communication_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_attempts_communication
    ON communication_attempts(communication_id);

CREATE TABLE IF NOT EXISTS communication_events (
    event_id TEXT PRIMARY KEY,
    communication_id TEXT NOT NULL REFERENCES customer_communications(communication_id),
    attempt_id TEXT REFERENCES communication_attempts(attempt_id),
    event_type TEXT NOT NULL,  -- COMMUNICATION_CREATED, EMAIL_SEND_REQUESTED, etc.
    occurred_at TEXT NOT NULL,
    detail TEXT DEFAULT '',
    data_json TEXT DEFAULT '{}',
    FOREIGN KEY(communication_id) REFERENCES customer_communications(communication_id)
);
CREATE INDEX IF NOT EXISTS idx_events_communication
    ON communication_events(communication_id);
CREATE INDEX IF NOT EXISTS idx_events_occurred
    ON communication_events(occurred_at);
"""


class DuplicateIdempotencyKey(Exception):
    """Raised internally, caught by the creator, never propagated - a
    duplicate create is not an error to the caller, it is the same row."""


class StaleVersion(Exception):
    """A compare-and-swap write lost the race: the row has moved since the
    caller last read it. The caller must re-read, never blindly retry the
    same write."""


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(16)}"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


# ======================================================================
# Customer Communications
# ======================================================================
def _row_to_communication(row: sqlite3.Row) -> CustomerCommunication:
    return CustomerCommunication(
        communication_id=row["communication_id"],
        booking_id=row["booking_id"],
        journey_reference=row["journey_reference"],
        user_id=row["user_id"],
        channel=CommunicationChannel(row["channel"]),
        communication_type=CommunicationType(row["communication_type"]),
        status=CommunicationStatus(row["status"]),
        recipient_address=row["recipient_address"],
        idempotency_key=row["idempotency_key"],
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
        version=row["version"],
    )


def create_communication(
    db: Database, *, communication: CustomerCommunication,
) -> tuple[CustomerCommunication, bool]:
    """Insert a new communication, or return the existing row for the same
    (booking_id, communication_type) pair. Returns ``(communication, created)`` -
    ``created`` is ``False`` when an earlier attempt already made this row
    (a safe retry, never a duplicate send)."""
    with db.write() as conn:
        try:
            conn.execute(
                "INSERT INTO customer_communications ("
                " communication_id, booking_id, journey_reference, user_id,"
                " channel, communication_type, status, recipient_address,"
                " idempotency_key, created_at, updated_at, version"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    communication.communication_id,
                    communication.booking_id,
                    communication.journey_reference,
                    communication.user_id,
                    communication.channel.value,
                    communication.communication_type.value,
                    communication.status.value,
                    communication.recipient_address,
                    communication.idempotency_key,
                    communication.created_at.isoformat(),
                    communication.updated_at.isoformat(),
                    communication.version,
                ),
            )
        except sqlite3.IntegrityError:
            # Duplicate - either idempotency_key or (booking_id, communication_type).
            # Try to retrieve the existing row.
            existing = conn.execute(
                "SELECT * FROM customer_communications"
                " WHERE booking_id=? AND communication_type=?",
                (communication.booking_id, communication.communication_type.value),
            ).fetchone()
            if existing is None:  # pragma: no cover - defensive
                raise
            return _row_to_communication(existing), False
    return communication, True


def get_communication(db: Database, communication_id: str) -> CustomerCommunication | None:
    row = db.query_one(
        "SELECT * FROM customer_communications WHERE communication_id=?",
        (communication_id,),
    )
    return _row_to_communication(row) if row else None


def get_communication_for_booking(
    db: Database, booking_id: str, communication_type: str,
) -> CustomerCommunication | None:
    """Get the one communication for this (booking, type) pair."""
    row = db.query_one(
        "SELECT * FROM customer_communications"
        " WHERE booking_id=? AND communication_type=?",
        (booking_id, communication_type),
    )
    return _row_to_communication(row) if row else None


def compare_and_swap_communication(
    db: Database, *, communication: CustomerCommunication, expected_version: int,
) -> CustomerCommunication:
    """Write ``communication`` (already advanced via ``.with_status``) only
    if the stored row is still at ``expected_version``. Raises
    :class:`StaleVersion` otherwise - the caller must re-read and decide
    fresh, never blindly overwrite."""
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE customer_communications SET"
            " status=?, updated_at=?, version=?"
            " WHERE communication_id=? AND version=?",
            (
                communication.status.value,
                communication.updated_at.isoformat(),
                communication.version,
                communication.communication_id,
                expected_version,
            ),
        )
        if cur.rowcount == 0:
            raise StaleVersion(
                f"{communication.communication_id}:"
                f" expected version {expected_version}, row has moved"
            )
    return communication


# ======================================================================
# Communication Attempts (retry tracking)
# ======================================================================
def _row_to_attempt(row: sqlite3.Row) -> CommunicationAttempt:
    return CommunicationAttempt(
        attempt_id=row["attempt_id"],
        communication_id=row["communication_id"],
        attempt_number=row["attempt_number"],
        status=row["status"],
        provider_name=row["provider_name"],
        provider_message_id=row["provider_message_id"],
        created_at=_dt(row["created_at"]),
        completed_at=_dt(row["completed_at"]),
    )


def record_attempt(db: Database, attempt: CommunicationAttempt) -> None:
    """Record a new send attempt."""
    with db.write() as conn:
        conn.execute(
            "INSERT INTO communication_attempts ("
            " attempt_id, communication_id, attempt_number, status,"
            " provider_name, provider_message_id, created_at, completed_at"
            ") VALUES (?,?,?,?,?,?,?,?)",
            (
                attempt.attempt_id,
                attempt.communication_id,
                attempt.attempt_number,
                attempt.status,
                attempt.provider_name,
                attempt.provider_message_id,
                attempt.created_at.isoformat(),
                _iso(attempt.completed_at),
            ),
        )


def list_attempts_for_communication(
    db: Database, communication_id: str,
) -> list[CommunicationAttempt]:
    """Get all attempts for a communication, in order."""
    rows = db.query(
        "SELECT * FROM communication_attempts"
        " WHERE communication_id=? ORDER BY attempt_number",
        (communication_id,),
    )
    return [_row_to_attempt(r) for r in rows]


def update_attempt_completion(
    db: Database, *, attempt_id: str, completed_at: datetime, status: str,
) -> None:
    """Mark an attempt as completed with final status."""
    with db.write() as conn:
        conn.execute(
            "UPDATE communication_attempts SET completed_at=?, status=?"
            " WHERE attempt_id=?",
            (completed_at.isoformat(), status, attempt_id),
        )


# ======================================================================
# Communication Events (append-only ledger)
# ======================================================================
def record_event(
    db: Database, event: CommunicationEvent, *, conn: sqlite3.Connection | None = None,
) -> None:
    """Append one ledger row. Pass ``conn`` to fold this into an
    already-open ``db.write()`` transaction (the common case: a state
    change and its ledger row must commit together or not at all)."""
    sql = (
        "INSERT INTO communication_events ("
        " event_id, communication_id, attempt_id, event_type, occurred_at,"
        " detail, data_json"
        ") VALUES (?,?,?,?,?,?,?)"
    )
    params = (
        event.event_id,
        event.communication_id,
        event.attempt_id,
        event.event_type,
        event.occurred_at.isoformat(),
        event.detail,
        json.dumps(event.data),
    )
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.write() as c:
            c.execute(sql, params)


def list_events(db: Database, communication_id: str) -> list[CommunicationEvent]:
    """Get all events for a communication, ordered by insertion (occurred_at, rowid)."""
    rows = db.query(
        # Sort by (occurred_at, rowid) - not just event_id: two events
        # written microseconds apart can share identical occurred_at
        # timestamps, and sorting by random event_id makes the ledger's
        # displayed order not chronological. rowid is exact insertion order.
        "SELECT *, rowid FROM communication_events"
        " WHERE communication_id=? ORDER BY occurred_at, rowid",
        (communication_id,),
    )
    return [
        CommunicationEvent(
            event_id=r["event_id"],
            communication_id=r["communication_id"],
            attempt_id=r["attempt_id"],
            event_type=r["event_type"],
            occurred_at=_dt(r["occurred_at"]),
            detail=r["detail"],
            data=json.loads(r["data_json"] or "{}"),
        )
        for r in rows
    ]
