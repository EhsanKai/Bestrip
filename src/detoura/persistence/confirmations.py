"""Persistence for the journey-confirmation domain (V9 Phase 5).

Two guarantees this module exists to provide:

**Exactly one confirmation per journey, no matter how many times the
finalizer runs.** A restart, a retried request or two workers racing the
same booking must all converge on the same row. That is enforced by the
``UNIQUE`` constraint on ``booking_id``, not by a read-then-write check: a
create is a single atomic INSERT attempt that either succeeds once or fails
with ``IntegrityError``, at which point the *existing* row is returned with
``created=False``. Same pattern as ``persistence/payments.py``'s idempotent
create, for the same reason - a SELECT-then-INSERT has a window, an INSERT
against a UNIQUE index does not.

**No status change ever overwrites a concurrent one.** Every state
transition goes through :func:`compare_and_swap_confirmation`, which writes
only if the stored row is still at the version the caller read. A loser gets
:class:`StaleConfirmationVersion` and must re-read and decide fresh.

This module is intentionally self-contained: it does not import Phase 4's
payment persistence or its exceptions. Confirmation references a payment by
``payment_id`` and a booking by ``booking_id``; it never joins into their
tables to re-derive truth that was already snapshotted at eligibility time.

No money columns: amounts live in ``payment_transactions`` and
``booking_economics``. A second copy of a total here would be a second number
that can disagree with the first.
"""

# --- Phase 5 schema addition (for Agent 6 to add to persistence/db.py's central migration - do NOT edit db.py yourself) ---
#
# -- Detoura's durable statement about one journey (V9 Phase 5 §A). Exactly
# -- one row per booking: `booking_id` is UNIQUE, and that constraint IS the
# -- idempotency mechanism for a replayed/concurrent finalizer run - never a
# -- SELECT-then-INSERT race. `booking_phase` and `payment_status` are
# -- deliberately *snapshots* taken when eligibility was evaluated, not live
# -- joins: they record why this status was decided and must not rewrite
# -- themselves when the booking or payment row later moves. No amount
# -- columns - money lives in payment_transactions / booking_economics.
# -- PII is `party_size` + `lead_name` only, matching the `bookings` table.
# CREATE TABLE IF NOT EXISTS journey_confirmations (
#     confirmation_id     TEXT PRIMARY KEY,
#     booking_id          TEXT NOT NULL UNIQUE,
#     journey_reference   TEXT NOT NULL,
#     user_id             TEXT,
#     status              TEXT NOT NULL,
#     service_tier        TEXT NOT NULL DEFAULT '',
#     booking_phase       TEXT NOT NULL,
#     payment_id          TEXT,
#     payment_status      TEXT,
#     party_size          INTEGER NOT NULL DEFAULT 1,
#     lead_name           TEXT NOT NULL DEFAULT '',
#     created_at          TEXT NOT NULL,
#     finalized_at        TEXT,
#     version             INTEGER NOT NULL DEFAULT 1
# );
# CREATE INDEX IF NOT EXISTS ix_confirmation_user ON journey_confirmations (user_id);
# CREATE INDEX IF NOT EXISTS ix_confirmation_status ON journey_confirmations (status);
# CREATE INDEX IF NOT EXISTS ix_confirmation_payment ON journey_confirmations (payment_id);
#
# -- Append-only confirmation ledger, mirroring `payment_events`. A row here
# -- is never updated or deleted; the current-state row above can be audited
# -- against this history. Never traveller PII in `detail`/`data_json`.
# CREATE TABLE IF NOT EXISTS confirmation_events (
#     event_id            TEXT PRIMARY KEY,
#     confirmation_id     TEXT NOT NULL,
#     event_type          TEXT NOT NULL,
#     occurred_at         TEXT NOT NULL,
#     detail              TEXT NOT NULL DEFAULT '',
#     data_json           TEXT NOT NULL DEFAULT '{}'
# );
# CREATE INDEX IF NOT EXISTS ix_confirmation_event ON confirmation_events (confirmation_id, occurred_at);
#
# --- end Phase 5 schema addition ---

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime

from ..models.confirmation import (
    ConfirmationEvent,
    ConfirmationStatus,
    JourneyConfirmation,
)
from .db import Database

#: The exact DDL Agent 6 folds into ``persistence/db.py``'s ``_DDL``. Kept as
#: executable text (not only as the comment block above) so this module's own
#: tests can create the tables in an in-memory database without touching the
#: shared migration file that other agents are editing concurrently.
CONFIRMATION_DDL = """
CREATE TABLE IF NOT EXISTS journey_confirmations (
    confirmation_id     TEXT PRIMARY KEY,
    booking_id          TEXT NOT NULL UNIQUE,
    journey_reference   TEXT NOT NULL,
    user_id             TEXT,
    status              TEXT NOT NULL,
    service_tier        TEXT NOT NULL DEFAULT '',
    booking_phase       TEXT NOT NULL,
    payment_id          TEXT,
    payment_status      TEXT,
    party_size          INTEGER NOT NULL DEFAULT 1,
    lead_name           TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    finalized_at        TEXT,
    version             INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_confirmation_user ON journey_confirmations (user_id);
CREATE INDEX IF NOT EXISTS ix_confirmation_status ON journey_confirmations (status);
CREATE INDEX IF NOT EXISTS ix_confirmation_payment ON journey_confirmations (payment_id);

CREATE TABLE IF NOT EXISTS confirmation_events (
    event_id            TEXT PRIMARY KEY,
    confirmation_id     TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    occurred_at         TEXT NOT NULL,
    detail              TEXT NOT NULL DEFAULT '',
    data_json           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_confirmation_event ON confirmation_events (confirmation_id, occurred_at);
"""


class DuplicateConfirmation(Exception):
    """A confirmation already exists for this booking.

    Raised internally and caught by :func:`create_confirmation`, never
    propagated: a duplicate finalizer run is not an error, it is the same
    journey, and the caller gets the existing row back.
    """


class StaleConfirmationVersion(Exception):
    """A compare-and-swap write lost the race: the confirmation has moved
    since the caller read it. The caller must re-read and re-decide, never
    blindly retry the same write over someone else's change."""


def new_confirmation_id() -> str:
    return f"conf_{secrets.token_urlsafe(16)}"


def new_confirmation_event_id() -> str:
    return f"cev_{secrets.token_urlsafe(16)}"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def apply_schema(db: Database) -> None:
    """Create the Phase 5 tables if they are not present.

    Exists so this module's tests (and any pre-integration caller) can work
    against a database whose central migration has not yet been updated. Once
    Agent 6 folds :data:`CONFIRMATION_DDL` into ``persistence/db.py``, this is
    a harmless no-op - every statement is ``IF NOT EXISTS``.
    """
    # `execute` per statement, NOT `executescript`: sqlite3's executescript
    # issues an implicit COMMIT before it runs, which would end the
    # BEGIN IMMEDIATE transaction `db.write()` opened and make its own
    # closing COMMIT fail with "no transaction is active".
    # `--` comment lines are dropped BEFORE the split: a semicolon inside a
    # prose comment would otherwise cut a statement in half and hand sqlite
    # the comment's tail as SQL.
    code = "\n".join(
        line for line in CONFIRMATION_DDL.splitlines()
        if line.strip() and not line.strip().startswith("--")
    )
    with db.write() as conn:
        for statement in code.split(";"):
            if statement.strip():
                conn.execute(statement)


# ======================================================================
# Confirmations
# ======================================================================
def _row_to_confirmation(row: sqlite3.Row) -> JourneyConfirmation:
    return JourneyConfirmation(
        confirmation_id=row["confirmation_id"],
        booking_id=row["booking_id"],
        journey_reference=row["journey_reference"],
        user_id=row["user_id"],
        status=ConfirmationStatus(row["status"]),
        service_tier=row["service_tier"],
        booking_phase=row["booking_phase"],
        payment_id=row["payment_id"],
        payment_status=row["payment_status"],
        party_size=row["party_size"],
        lead_name=row["lead_name"],
        created_at=_dt(row["created_at"]),
        finalized_at=_dt(row["finalized_at"]),
        version=row["version"],
    )


def create_confirmation(
    db: Database, *, confirmation: JourneyConfirmation,
) -> tuple[JourneyConfirmation, bool]:
    """Insert the confirmation, or return the one that already exists for
    this ``booking_id``.

    Returns ``(confirmation, created)``. ``created`` is ``False`` when an
    earlier (or concurrent) finalizer run already made the row - a safe
    replay, never a second confirmation for the same journey.

    The returned object on the ``False`` path is the **stored** row, not the
    caller's candidate: the winner's status is the truth, and a loser that
    kept its own object would go on to compare-and-swap against a version and
    a status that were never written.
    """
    with db.write() as conn:
        try:
            conn.execute(
                "INSERT INTO journey_confirmations ("
                " confirmation_id, booking_id, journey_reference, user_id,"
                " status, service_tier, booking_phase, payment_id,"
                " payment_status, party_size, lead_name, created_at,"
                " finalized_at, version"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    confirmation.confirmation_id, confirmation.booking_id,
                    confirmation.journey_reference, confirmation.user_id,
                    confirmation.status.value, confirmation.service_tier,
                    confirmation.booking_phase, confirmation.payment_id,
                    confirmation.payment_status, confirmation.party_size,
                    confirmation.lead_name,
                    confirmation.created_at.isoformat(),
                    _iso(confirmation.finalized_at), confirmation.version,
                ),
            )
        except sqlite3.IntegrityError:
            existing = conn.execute(
                "SELECT * FROM journey_confirmations WHERE booking_id=?",
                (confirmation.booking_id,),
            ).fetchone()
            if existing is None:  # pragma: no cover - defensive
                # The IntegrityError was not the booking_id UNIQUE constraint
                # (a confirmation_id collision, say). Not a duplicate journey,
                # so it must not be swallowed as one.
                raise
            return _row_to_confirmation(existing), False
    return confirmation, True


def get_confirmation(db: Database, confirmation_id: str) -> JourneyConfirmation | None:
    row = db.query_one(
        "SELECT * FROM journey_confirmations WHERE confirmation_id=?",
        (confirmation_id,),
    )
    return _row_to_confirmation(row) if row else None


def get_confirmation_for_booking(db: Database, booking_id: str) -> JourneyConfirmation | None:
    row = db.query_one(
        "SELECT * FROM journey_confirmations WHERE booking_id=?", (booking_id,),
    )
    return _row_to_confirmation(row) if row else None


def get_confirmation_for_user(
    db: Database, confirmation_id: str, *, user_id: str,
) -> JourneyConfirmation | None:
    """Ownership-checked read. Returns ``None`` both when the confirmation
    does not exist and when it belongs to someone else - the caller cannot
    tell the two apart, so a confirmation id cannot be probed for existence
    (IDOR/enumeration-safe, matching ``api/me_trips.py``).

    A blank ``user_id`` matches nothing: anonymous confirmations are stored
    with ``user_id IS NULL`` and are not owned by "the empty user".
    """
    if not user_id:
        return None
    row = db.query_one(
        "SELECT * FROM journey_confirmations WHERE confirmation_id=? AND user_id=?",
        (confirmation_id, user_id),
    )
    return _row_to_confirmation(row) if row else None


def list_confirmations_for_user(
    db: Database, *, user_id: str, limit: int = 100,
) -> list[JourneyConfirmation]:
    if not user_id:
        return []
    limit = max(1, min(int(limit), 500))
    rows = db.query(
        "SELECT * FROM journey_confirmations WHERE user_id=?"
        " ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )
    return [_row_to_confirmation(r) for r in rows]


def compare_and_swap_confirmation(
    db: Database, *, confirmation: JourneyConfirmation, expected_version: int,
) -> JourneyConfirmation:
    """Write ``confirmation`` (already advanced via :meth:`with_status`) only
    if the stored row is still at ``expected_version``.

    Raises :class:`StaleConfirmationVersion` otherwise. Only the mutable
    fields are written: ``confirmation_id``, ``booking_id`` and the snapshot
    columns are identity/evidence and are never updated after creation.
    """
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE journey_confirmations SET status=?, finalized_at=?, version=?"
            " WHERE confirmation_id=? AND version=?",
            (
                confirmation.status.value, _iso(confirmation.finalized_at),
                confirmation.version, confirmation.confirmation_id,
                expected_version,
            ),
        )
        if cur.rowcount == 0:
            raise StaleConfirmationVersion(
                f"{confirmation.confirmation_id}: expected version "
                f"{expected_version}, row has moved"
            )
    return confirmation


# ======================================================================
# Confirmation events (append-only ledger)
# ======================================================================
def record_event(
    db: Database, event: ConfirmationEvent, *, conn: sqlite3.Connection | None = None,
) -> None:
    """Append one ledger row. Pass ``conn`` to fold this into an already-open
    ``db.write()`` transaction - a status change and the ledger row that
    explains it must commit together or not at all."""
    sql = (
        "INSERT INTO confirmation_events ("
        " event_id, confirmation_id, event_type, occurred_at, detail, data_json"
        ") VALUES (?,?,?,?,?,?)"
    )
    params = (
        event.event_id, event.confirmation_id, event.event_type,
        event.occurred_at.isoformat(), event.detail, json.dumps(event.data),
    )
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.write() as c:
            c.execute(sql, params)


def list_events(db: Database, confirmation_id: str) -> list[ConfirmationEvent]:
    rows = db.query(
        # Tiebreak on `rowid` (SQLite's implicit, monotonically-increasing
        # insertion order), NOT on `event_id`. Phase 4 hit this for real:
        # two events written microseconds apart share an identical
        # `occurred_at`, and breaking that tie with a random url-safe token
        # puts the ledger in an order that is not chronological. `rowid` is
        # exact insertion order, always.
        "SELECT *, rowid FROM confirmation_events WHERE confirmation_id=?"
        " ORDER BY occurred_at, rowid",
        (confirmation_id,),
    )
    return [
        ConfirmationEvent(
            event_id=r["event_id"], confirmation_id=r["confirmation_id"],
            event_type=r["event_type"], occurred_at=_dt(r["occurred_at"]),
            detail=r["detail"], data=json.loads(r["data_json"] or "{}"),
        )
        for r in rows
    ]
