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

SCHEMA_VERSION = 7

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

-- Post-booking ticket operations (V8.5 C3): cancellation, change, recovery.
-- One row per operation, with a server-issued operation_id that doubles as an
-- idempotency key: a repeated execute against a terminal row returns the
-- stored result and never re-calls the provider. No document/PII data here -
-- only ids, money, states and a free-text operator reason.
CREATE TABLE IF NOT EXISTS ticket_operations (
    operation_id      TEXT PRIMARY KEY,
    booking_id        TEXT NOT NULL,
    sequence          INTEGER NOT NULL DEFAULT 0,
    kind              TEXT NOT NULL,
    state             TEXT NOT NULL,
    provider          TEXT NOT NULL DEFAULT 'duffel',
    provider_order_id TEXT,
    reason            TEXT NOT NULL DEFAULT '',
    actor             TEXT NOT NULL DEFAULT '',
    idempotency_key   TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    quote_json        TEXT,
    result_json       TEXT
);
CREATE INDEX IF NOT EXISTS ix_ticketops_booking ON ticket_operations (booking_id);
CREATE INDEX IF NOT EXISTS ix_ticketops_state ON ticket_operations (state);
CREATE UNIQUE INDEX IF NOT EXISTS ix_ticketops_idem
    ON ticket_operations (idempotency_key)
    WHERE idempotency_key != '';

-- ==================================================================
-- V9 Phase 1: Search Intelligence — persistent market observation.
--
-- price_observations is HISTORICAL MARKET DATA, not a price cache and never a
-- live quote. It survives cache expiry and process restarts; it is read only
-- by acquisition scoring and analytics, never by checkout/revalidation. Money
-- is integer minor units + explicit currency. Retention-pruned by age
-- (PRICE_MEMORY_RETENTION_DAYS); booking_economics and audit_events are never
-- touched by that prune.
-- ==================================================================
CREATE TABLE IF NOT EXISTS price_observations (
    observation_id        TEXT PRIMARY KEY,
    observed_at           TEXT NOT NULL,
    provider              TEXT NOT NULL,
    origin                TEXT NOT NULL,
    destination           TEXT NOT NULL,
    departure_date        TEXT NOT NULL,
    return_date           TEXT,
    trip_shape            TEXT NOT NULL DEFAULT 'ONE_WAY',
    travelers             INTEGER NOT NULL DEFAULT 1,
    travelers_bucket      TEXT NOT NULL DEFAULT '1',
    total_amount_minor    INTEGER NOT NULL,
    per_person_minor      INTEGER NOT NULL,
    currency              TEXT NOT NULL,
    direct                INTEGER,
    stops                 INTEGER,
    marketing_carrier     TEXT,
    operating_carrier     TEXT,
    cabin                 TEXT,
    baggage_cabin         TEXT,
    baggage_checked       TEXT,
    offer_count_for_edge  INTEGER,
    search_id             TEXT NOT NULL,
    acquisition_call_id   TEXT NOT NULL,
    search_mode           TEXT NOT NULL DEFAULT 'UNKNOWN',
    candidate_reason      TEXT NOT NULL DEFAULT '',
    exploration           INTEGER NOT NULL DEFAULT 0,
    candidate_rank        INTEGER,
    provider_call_ordinal INTEGER,
    provider_call_budget  INTEGER,
    edge_kind             TEXT NOT NULL DEFAULT '',
    secondary_market      TEXT,
    scoring_reference_date TEXT,
    provenance_version    INTEGER NOT NULL DEFAULT 1,
    normalized_ok         INTEGER NOT NULL DEFAULT 1,
    retained_after_limits INTEGER NOT NULL DEFAULT 1,
    entered_candidate_set INTEGER NOT NULL DEFAULT 0,
    contributed_to_top_k  INTEGER NOT NULL DEFAULT 0,
    contributed_to_winner INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_priceobs_market
    ON price_observations (provider, origin, destination, departure_date, travelers_bucket);
CREATE INDEX IF NOT EXISTS ix_priceobs_observed ON price_observations (observed_at);
CREATE INDEX IF NOT EXISTS ix_priceobs_search ON price_observations (search_id);
CREATE INDEX IF NOT EXISTS ix_priceobs_call ON price_observations (acquisition_call_id);

-- One row per real-supply search. The trace body (candidate decisions, per-call
-- outcomes, economics) is a JSON blob; the promoted columns are what the Ops
-- coverage/economics queries filter and aggregate on. No traveller PII.
CREATE TABLE IF NOT EXISTS search_traces (
    search_id             TEXT PRIMARY KEY,
    started_at            TEXT NOT NULL,
    finished_at           TEXT,
    provider              TEXT NOT NULL DEFAULT 'duffel',
    origin                TEXT NOT NULL,
    date_from             TEXT NOT NULL,
    date_to               TEXT NOT NULL,
    search_mode           TEXT NOT NULL DEFAULT 'UNKNOWN',
    travelers             INTEGER NOT NULL DEFAULT 1,
    provider_call_budget  INTEGER NOT NULL DEFAULT 0,
    provider_calls_used   INTEGER NOT NULL DEFAULT 0,
    provider_calls_failed INTEGER NOT NULL DEFAULT 0,
    cache_hits            INTEGER NOT NULL DEFAULT 0,
    cache_misses          INTEGER NOT NULL DEFAULT 0,
    calls_explore         INTEGER NOT NULL DEFAULT 0,
    calls_exploit         INTEGER NOT NULL DEFAULT 0,
    recommendations_produced INTEGER NOT NULL DEFAULT 0,
    useful_call_rate      REAL,
    top_k_contribution_rate REAL,
    winner_contribution_rate REAL,
    excess_search_cost_minor INTEGER,
    economics_configured  INTEGER NOT NULL DEFAULT 0,
    trace_json            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_searchtrace_started ON search_traces (started_at);
CREATE INDEX IF NOT EXISTS ix_searchtrace_origin ON search_traces (origin);

-- ==================================================================
-- V9 Phase 2: Bootstrap Market Prior — a SPARSE, external/pre-seeded,
-- approximate historical view of markets. Architecturally distinct from
-- price_observations (which is Detoura's own live acquisition history):
-- different table, different provenance (source + source_version), different
-- retention. NEVER a quote — no code path may treat market_priors as a
-- bookable/current supplier fare. Money is integer minor units + explicit
-- currency; anything the source did not supply is NULL (UNKNOWN, never 0).
-- ==================================================================
CREATE TABLE IF NOT EXISTS market_priors (
    prior_id            TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    source_version      TEXT NOT NULL DEFAULT '',
    imported_at         TEXT NOT NULL,
    source_date         TEXT,
    origin_airport      TEXT NOT NULL,
    destination_airport TEXT NOT NULL,
    destination_id      TEXT,
    season              TEXT NOT NULL DEFAULT 'UNKNOWN',
    month               INTEGER,
    horizon_bucket      TEXT NOT NULL DEFAULT 'UNKNOWN',
    weekday_class       TEXT NOT NULL DEFAULT 'UNKNOWN',
    duration_bucket     TEXT NOT NULL DEFAULT 'UNKNOWN',
    currency            TEXT NOT NULL,
    sample_count        INTEGER,
    observed_low_minor  INTEGER,
    median_minor        INTEGER,
    typical_minor       INTEGER,
    observed_high_minor INTEGER,
    confidence          TEXT NOT NULL DEFAULT 'LOW',
    direct_possible     INTEGER,
    weekly_frequency    INTEGER,
    carrier_count       INTEGER,
    provenance_version  INTEGER NOT NULL DEFAULT 1,
    -- one row per (market x time-context bucket x source); a re-import of the
    -- same logical row replaces it.
    UNIQUE (source, origin_airport, destination_airport, season,
            horizon_bucket, weekday_class, duration_bucket, currency)
);
CREATE INDEX IF NOT EXISTS ix_prior_market
    ON market_priors (origin_airport, destination_airport);
CREATE INDEX IF NOT EXISTS ix_prior_dest ON market_priors (destination_airport);
CREATE INDEX IF NOT EXISTS ix_prior_source ON market_priors (source, imported_at);

-- One row per bootstrap import run: provenance + metrics. Import failures are
-- recorded here and never corrupt existing market_priors or price_observations.
CREATE TABLE IF NOT EXISTS market_prior_imports (
    import_id           TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    source_version      TEXT NOT NULL DEFAULT '',
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    dry_run             INTEGER NOT NULL DEFAULT 0,
    rows_seen           INTEGER NOT NULL DEFAULT 0,
    rows_imported       INTEGER NOT NULL DEFAULT 0,
    rows_updated        INTEGER NOT NULL DEFAULT 0,
    rows_skipped_dup    INTEGER NOT NULL DEFAULT 0,
    rows_rejected       INTEGER NOT NULL DEFAULT 0,
    markets_covered     INTEGER NOT NULL DEFAULT 0,
    origins_covered     INTEGER NOT NULL DEFAULT 0,
    destinations_covered INTEGER NOT NULL DEFAULT 0,
    -- external-source cost/limits, NULL when UNKNOWN (never reported as 0)
    source_requests     INTEGER,
    source_request_cost_minor INTEGER,
    source_rate_limit_events  INTEGER,
    rejected_json       TEXT NOT NULL DEFAULT '[]',
    ok                  INTEGER NOT NULL DEFAULT 1,
    error               TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_priorimport_source ON market_prior_imports (source, started_at);

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
    operating_carrier   TEXT NOT NULL DEFAULT '',
    operating_flight_number TEXT NOT NULL DEFAULT '',
    carrier_name        TEXT NOT NULL DEFAULT '',
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

-- ==================================================================
-- V9 Phase 2.5 — Authorized Market-Prior Acquisition. These three tables sit
-- entirely upstream of market_priors: a job/task run here ends by feeding the
-- existing Phase 2 import boundary (market_prior_import.run_import), and
-- never writes market_priors directly. Fail-closed authorization lives on
-- market_prior_sources; only authorization_status='APPROVED' rows may ever
-- back a network-capable adapter (enforced in code, not just schema).
-- ==================================================================
CREATE TABLE IF NOT EXISTS market_prior_sources (
    source_id           TEXT PRIMARY KEY,
    source_name         TEXT NOT NULL,
    source_type         TEXT NOT NULL,
    authorization_status TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED',
    authorization_basis TEXT NOT NULL DEFAULT '',
    allowed_scope       TEXT NOT NULL DEFAULT '',
    commercial_reuse_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    persistence_allowed INTEGER NOT NULL DEFAULT 1,
    base_domain         TEXT,
    rate_limit_json      TEXT NOT NULL DEFAULT '{}',
    adapter_version     TEXT NOT NULL DEFAULT 'v1',
    reviewed_at         TEXT,
    reviewed_by         TEXT NOT NULL DEFAULT '',
    notes               TEXT NOT NULL DEFAULT '',
    robots_checked_at   TEXT,
    robots_allowed      INTEGER,
    request_cost_minor  INTEGER,
    -- The next instant this source may be sent a request, in persisted
    -- shared state rather than an in-process object - the choke point that
    -- makes per-source rate limiting correct across every thread, job and
    -- OS process sharing this database file (V9 Phase 2.5 §17, §59).
    next_allowed_at     TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_prior_jobs (
    job_id              TEXT PRIMARY KEY,
    source_id           TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'PLANNED',
    origin_scope_json   TEXT NOT NULL DEFAULT '[]',
    destination_scope_json TEXT NOT NULL DEFAULT '[]',
    horizon_scope_json  TEXT NOT NULL DEFAULT '[]',
    request_budget      INTEGER NOT NULL,
    dry_run             INTEGER NOT NULL DEFAULT 0,
    planned             INTEGER NOT NULL DEFAULT 0,
    deduplicated        INTEGER NOT NULL DEFAULT 0,
    requests_used        INTEGER NOT NULL DEFAULT 0,
    stopped_reason       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_priorjob_source ON market_prior_jobs (source_id, created_at);

CREATE TABLE IF NOT EXISTS market_prior_tasks (
    task_id             TEXT PRIMARY KEY,
    job_id              TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    origin              TEXT NOT NULL,
    destination         TEXT NOT NULL,
    horizon_bucket      TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    last_attempt_at     TEXT,
    next_retry_at       TEXT,
    completed_at        TEXT,
    parser_version      TEXT NOT NULL DEFAULT '',
    failure_reason      TEXT NOT NULL DEFAULT '',
    lease_owner         TEXT NOT NULL DEFAULT '',
    lease_expires_at    TEXT,
    UNIQUE (job_id, origin, destination, horizon_bucket)
);
CREATE INDEX IF NOT EXISTS ix_priortask_job ON market_prior_tasks (job_id, status);
CREATE INDEX IF NOT EXISTS ix_priortask_lease ON market_prior_tasks (status, lease_expires_at);
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

    #: Columns added to existing tables after their first release. The
    #: ``CREATE TABLE IF NOT EXISTS`` DDL only creates *new* tables; a column
    #: added to a table that already exists on an older deployment needs an
    #: explicit, idempotent ALTER. Each entry is (table, column, DDL fragment).
    _ADD_COLUMNS = (
        ("booking_items", "operating_carrier", "TEXT NOT NULL DEFAULT ''"),
        ("booking_items", "operating_flight_number", "TEXT NOT NULL DEFAULT ''"),
        ("booking_items", "carrier_name", "TEXT NOT NULL DEFAULT ''"),
        # V9 Phase 1 QA fix — provenance fields on an already-created v5 table.
        ("price_observations", "edge_kind", "TEXT NOT NULL DEFAULT ''"),
        ("price_observations", "secondary_market", "TEXT"),
        ("price_observations", "scoring_reference_date", "TEXT"),
        ("price_observations", "provenance_version", "INTEGER NOT NULL DEFAULT 1"),
    )

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_DDL)
            for table, column, ddl in self._ADD_COLUMNS:
                cols = {
                    r["name"]
                    for r in self._conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                if column not in cols:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                    )
            row = self._conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            elif row["version"] < SCHEMA_VERSION:
                # New tables + columns are handled above; record we are current.
                self._conn.execute(
                    "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
                )

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A serialised write transaction. Commits on success, rolls back on
        error.

        ``BEGIN IMMEDIATE``, not a plain (deferred) ``BEGIN``: a write
        transaction is always going to write, so it should acquire SQLite's
        write lock up front. A deferred ``BEGIN`` only takes a read lock
        until the first write statement, which under concurrent writers
        (V9 Phase 2.5: two OS processes hammering the same acquisition
        source's rate-limit row) produces a reader-to-writer *lock upgrade*
        race that ``PRAGMA busy_timeout`` does not reliably retry around —
        observed directly as ``sqlite3.OperationalError: database is
        locked`` under a cross-process acquisition test. ``BEGIN IMMEDIATE``
        contends for the write lock itself, which ``busy_timeout`` *does*
        retry, so a second writer (same process or a different one) simply
        waits for the first transaction to commit."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
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
