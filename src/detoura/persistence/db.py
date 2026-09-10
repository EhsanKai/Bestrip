"""SQLite persistence for Detoura's commercial + ops data (V8.5).

The rest of Detoura is deliberately in-memory: a search, a selection and a
booking run are all short-lived. Commercial records are not - a promo code, a
markup policy version, an audit entry and the economics ledger for a completed
booking must survive a restart and must not be silently recomputed. That is
what this database is for, and it holds nothing else.

stdlib ``sqlite3`` only. One connection, WAL mode, every access serialised
through a lock - correct and more than fast enough for sandbox volumes. Money
is stored as integer minor units so a stored row never drifts.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 3

_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS markup_policies (
    policy_id       TEXT NOT NULL,
    version         INTEGER NOT NULL,
    label           TEXT NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 0,
    definition_json TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (policy_id, version)
);

CREATE TABLE IF NOT EXISTS promo_codes (
    code            TEXT PRIMARY KEY,
    definition_json TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_redemptions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT NOT NULL,
    booking_id     TEXT NOT NULL,
    user_key       TEXT NOT NULL DEFAULT 'anonymous',
    discount_minor INTEGER NOT NULL,
    currency       TEXT NOT NULL,
    redeemed_at    TEXT NOT NULL,
    UNIQUE (code, booking_id)
);
CREATE INDEX IF NOT EXISTS ix_redemptions_code ON promo_redemptions (code);
CREATE INDEX IF NOT EXISTS ix_redemptions_user ON promo_redemptions (code, user_key);

CREATE TABLE IF NOT EXISTS booking_economics (
    booking_id             TEXT PRIMARY KEY,
    journey_reference      TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    currency               TEXT NOT NULL,
    service_tier           TEXT NOT NULL,
    markup_policy_id       TEXT NOT NULL,
    markup_policy_version  INTEGER NOT NULL,
    promo_code             TEXT,
    supplier_transport_minor INTEGER NOT NULL,
    supplier_baggage_minor   INTEGER NOT NULL,
    supplier_fees_minor      INTEGER NOT NULL,
    service_fee_minor        INTEGER NOT NULL,
    markup_minor             INTEGER NOT NULL,
    discount_minor           INTEGER NOT NULL,
    tax_minor                INTEGER NOT NULL,
    customer_price_minor     INTEGER NOT NULL,
    -- costs that Detoura may not know at booking time. NULL means UNKNOWN,
    -- never zero. Filled in later by ops; the priced components above never
    -- change once written.
    provider_cost_estimate_minor INTEGER,
    payment_cost_minor           INTEGER,
    refund_minor                 INTEGER,
    recovery_cost_minor          INTEGER,
    breakdown_json         TEXT NOT NULL,
    snapshot_json          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT '',
    target_id   TEXT NOT NULL DEFAULT '',
    before_json TEXT,
    after_json  TEXT,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_events (ts);
CREATE INDEX IF NOT EXISTS ix_audit_target ON audit_events (target_type, target_id);

-- The operational record of every Detoura journey, for the ops console. This
-- is mutable (states change, recovery notes get added); the immutable
-- commercial record is booking_economics. PII is deliberately minimal: lead
-- name + email for customer identification, nothing else (no DOB, phone,
-- nationality or document numbers).
CREATE TABLE IF NOT EXISTS bookings (
    booking_id            TEXT PRIMARY KEY,
    session_ref           TEXT NOT NULL DEFAULT '',
    journey_reference     TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    mode                  TEXT NOT NULL,
    phase                 TEXT NOT NULL,
    trip_label            TEXT NOT NULL DEFAULT '',
    route_json            TEXT NOT NULL DEFAULT '[]',
    party_size            INTEGER NOT NULL DEFAULT 1,
    lead_name             TEXT NOT NULL DEFAULT '',
    lead_email            TEXT NOT NULL DEFAULT '',
    currency              TEXT NOT NULL DEFAULT 'EUR',
    service_tier          TEXT NOT NULL DEFAULT 'BASIC',
    discovered_total_minor INTEGER NOT NULL DEFAULT 0,
    current_total_minor   INTEGER,
    customer_total_minor  INTEGER,
    recovery_state        TEXT NOT NULL DEFAULT '',
    reconfirm_note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_bookings_phase ON bookings (phase);
CREATE INDEX IF NOT EXISTS ix_bookings_recovery ON bookings (recovery_state);
CREATE INDEX IF NOT EXISTS ix_bookings_updated ON bookings (updated_at);

-- Product-funnel events (V8.5 Phase C2). Anonymous by construction: a random
-- per-tab session key and a random per-browser visitor key, and a small
-- props blob that the ingest endpoint strips of anything PII-shaped. No name,
-- email, phone, DOB or free text ever lands here.
CREATE TABLE IF NOT EXISTS analytics_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    event        TEXT NOT NULL,
    session_key  TEXT NOT NULL DEFAULT '',
    visitor_key  TEXT NOT NULL DEFAULT '',
    tier         TEXT NOT NULL DEFAULT '',
    props_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON analytics_events (ts);
CREATE INDEX IF NOT EXISTS ix_events_event ON analytics_events (event);
CREATE INDEX IF NOT EXISTS ix_events_visitor ON analytics_events (visitor_key);

CREATE TABLE IF NOT EXISTS booking_items (
    booking_id          TEXT NOT NULL,
    sequence            INTEGER NOT NULL,
    origin_city         TEXT NOT NULL DEFAULT '',
    origin_airport      TEXT NOT NULL DEFAULT '',
    destination_city    TEXT NOT NULL DEFAULT '',
    destination_airport TEXT NOT NULL DEFAULT '',
    departure           TEXT,
    arrival             TEXT,
    carrier             TEXT NOT NULL DEFAULT '',
    flight_number       TEXT NOT NULL DEFAULT '',
    offer_id            TEXT NOT NULL DEFAULT '',
    provider            TEXT NOT NULL DEFAULT '',
    quoted_price_minor  INTEGER NOT NULL DEFAULT 0,
    current_price_minor INTEGER,
    booked_price_minor  INTEGER,
    currency            TEXT NOT NULL DEFAULT 'EUR',
    cabin_baggage       TEXT NOT NULL DEFAULT 'unknown',
    checked_baggage     TEXT NOT NULL DEFAULT 'unknown',
    required            INTEGER NOT NULL DEFAULT 1,
    state               TEXT NOT NULL,
    detail              TEXT NOT NULL DEFAULT '',
    provider_order_id   TEXT,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (booking_id, sequence)
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path not in (":memory:", "") and not path.startswith("file:"):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path or ":memory:", check_same_thread=False, isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=4000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_DDL)
            row = self._conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            elif row["version"] < SCHEMA_VERSION:
                # The IF NOT EXISTS DDL above already added the new tables; just
                # record that we are current.
                self._conn.execute(
                    "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
                )

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A serialised write transaction. Commits on success, rolls back on
        error."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_DB: Database | None = None
_DB_LOCK = threading.Lock()


def db_path_from_env() -> str:
    """``DETOURA_DB_PATH`` or a local default. In Docker this points at a
    mounted volume; see the Dockerfile."""
    return os.getenv("DETOURA_DB_PATH", "").strip() or str(
        Path.cwd() / "detoura.db"
    )


def configure_db(database: Database | None) -> Database:
    """Install the process-wide database. Tests pass an explicit in-memory
    one; the app calls :func:`init_db` at startup."""
    global _DB
    with _DB_LOCK:
        _DB = database or Database(db_path_from_env())
        return _DB


def init_db() -> Database:
    global _DB
    with _DB_LOCK:
        if _DB is None:
            _DB = Database(db_path_from_env())
        return _DB


def get_db() -> Database:
    if _DB is None:
        return init_db()
    return _DB
