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

SCHEMA_VERSION = 12

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

-- ==================================================================
-- V9 Phase 2.6 Part A — accounts, sessions, trip ownership. UserAccount is
-- deliberately thin (no PII beyond a normalized email); Traveler/passenger
-- data lives entirely elsewhere and is never joined into this table. A
-- session's raw token is never stored - only its SHA-256 hash - and
-- csrf_token_hash is likewise a hash, never the value handed to the browser.
-- ==================================================================
-- password_hash is nullable (V9 Google auth): an account created entirely
-- through Google Sign-In has no password credential at all - not an empty
-- string, not a random hidden value, genuinely NULL - until/unless it goes
-- through the password-reset flow to establish one (see password_service.py
-- and docs/V9_GOOGLE_AUTH_ACCOUNT_LIFECYCLE_REPORT.md §11). A pre-existing
-- database's column is relaxed from NOT NULL by Database._relax_password_hash_nullable().
CREATE TABLE IF NOT EXISTS user_accounts (
    user_id             TEXT PRIMARY KEY,
    email_normalized    TEXT NOT NULL UNIQUE,
    password_hash       TEXT,
    status              TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    last_login_at       TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id          TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    token_hash          TEXT NOT NULL UNIQUE,
    csrf_token_hash     TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    revoked_at          TEXT,
    last_seen_at        TEXT
);
CREATE INDEX IF NOT EXISTS ix_session_user ON auth_sessions (user_id);
CREATE INDEX IF NOT EXISTS ix_session_token_hash ON auth_sessions (token_hash);

-- One row per booking an authenticated user made. A booking with no row
-- here is a valid, permanent anonymous/historical journey - never backfilled
-- (V9 Phase 2.6 §A8).
CREATE TABLE IF NOT EXISTS trip_ownership (
    booking_id          TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    journey_reference   TEXT NOT NULL DEFAULT '',
    claimed_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trip_owner ON trip_ownership (user_id);

-- ==================================================================
-- V9 Google Sign-In + Account Lifecycle. Google identity is bound to the
-- provider's stable subject (`sub`), never to email alone - see
-- docs/V9_GOOGLE_AUTH_ACCOUNT_LIFECYCLE_REPORT.md for the full linking
-- policy. No Google access/refresh token is ever persisted here: Detoura
-- only ever needs the one-time identity claim at login, never ongoing
-- access to a Google API on the user's behalf.
-- ==================================================================
CREATE TABLE IF NOT EXISTS auth_identities (
    identity_id         TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    provider            TEXT NOT NULL,
    provider_subject    TEXT NOT NULL,
    provider_email      TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (provider, provider_subject)
);
CREATE INDEX IF NOT EXISTS ix_identity_user ON auth_identities (user_id);

-- One opaque, high-entropy, single-use password-reset token per row - only
-- its SHA-256 hash is ever stored, exactly like a session token. Doubles as
-- the "set a first password" mechanism for a Google-only account (§11):
-- proving control of the registered email is the same bar either way.
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token_hash          TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    consumed_at         TEXT
);
CREATE INDEX IF NOT EXISTS ix_reset_user ON password_reset_tokens (user_id);

-- Transport-level OAuth state for one in-flight Google authorization-code +
-- PKCE round trip (state -> code_verifier/nonce). Persisted (not in-process)
-- so a callback landing on a different worker/process still finds it - see
-- services/google_oauth.py. `state_hash` is SHA-256 of the opaque state
-- value that actually travels to Google and back, mirroring the session/
-- reset-token hash-at-rest convention. `link_user_id` is set only when this
-- round trip was started by an already-authenticated user explicitly
-- connecting their Google account (Case F), never inferred after the fact.
CREATE TABLE IF NOT EXISTS google_oauth_pending (
    state_hash          TEXT PRIMARY KEY,
    code_verifier       TEXT NOT NULL,
    nonce               TEXT NOT NULL,
    link_user_id        TEXT,
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    consumed_at         TEXT
);
CREATE INDEX IF NOT EXISTS ix_google_pending_expires ON google_oauth_pending (expires_at);

-- A Google identity waiting to be claimed by an existing password account
-- with the same email (Case C - "existing password account, same email,
-- unauthenticated Google sign-in"). Never auto-links; this row only lets a
-- caller who then separately proves control of the matching password
-- account (an authenticated session whose own email matches) complete the
-- link explicitly - see google_auth_service.py::confirm_google_link.
CREATE TABLE IF NOT EXISTS pending_google_links (
    link_id             TEXT PRIMARY KEY,
    provider_subject    TEXT NOT NULL,
    provider_email      TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    consumed_at         TEXT
);

-- ==================================================================
-- V9 Phase 3: Destination Attractiveness
-- ==================================================================
-- One row per (destination_id, model_version). Versioned by design: a new
-- model_version never overwrites or reinterprets an old row - it is a new
-- row, so a stored profile's meaning never silently changes underneath a
-- caller that cached it, and the whole history of a destination's scores
-- across model revisions stays queryable. "Current" is whichever
-- model_version the running config names (see attractiveness_config.py),
-- not implicitly "the highest row" or "the last written".
CREATE TABLE IF NOT EXISTS destination_attractiveness (
    destination_id            TEXT NOT NULL,
    model_version             INTEGER NOT NULL,
    sightseeing_score         REAL,
    culture_score             REAL,
    food_score                REAL,
    nightlife_score           REAL,
    nature_score              REAL,
    uniqueness_score          REAL,
    short_trip_score          REAL,
    experience_density_score  REAL,
    aggregate_score           REAL,
    confidence                TEXT NOT NULL DEFAULT 'UNKNOWN',
    provenance                TEXT NOT NULL DEFAULT 'UNKNOWN',
    source                    TEXT NOT NULL DEFAULT '',
    updated_at                TEXT NOT NULL,
    PRIMARY KEY (destination_id, model_version)
);
CREATE INDEX IF NOT EXISTS ix_attractiveness_version
    ON destination_attractiveness (model_version);

-- ==================================================================
-- V9 Phase 4: Payment Architecture & Transaction Foundation
-- ==================================================================
-- Everything here follows the same money discipline as `bookings` /
-- `economics`: integer minor units, never float, and UNKNOWN is a real
-- distinct value from 0.

-- The immutable checkout/payment snapshot (V9 Phase 4 §C). Once written, a
-- row is never updated - a new snapshot is a new row, and a payment always
-- points at the exact snapshot it was authorized against. This is a
-- server-owned price freeze: the CommercialQuote that priced it is embedded
-- verbatim (quote_json), never recomputed from live rules later.
CREATE TABLE IF NOT EXISTS checkout_snapshots (
    snapshot_id           TEXT PRIMARY KEY,
    booking_id            TEXT NOT NULL,
    journey_reference     TEXT NOT NULL,
    user_id               TEXT,
    service_tier          TEXT NOT NULL,
    currency              TEXT NOT NULL,
    customer_total_minor  INTEGER NOT NULL,
    markup_policy_id      TEXT NOT NULL DEFAULT '',
    markup_policy_version INTEGER NOT NULL DEFAULT 0,
    price_provenance      TEXT NOT NULL DEFAULT '',
    quote_json            TEXT NOT NULL DEFAULT '{}',
    revalidation_json     TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL,
    expires_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_checkout_snapshot_booking ON checkout_snapshots (booking_id);

-- One row per payment transaction - the current, mutable state. History is
-- never reconstructed from this row alone; `payment_events` is the
-- append-only ledger of every irreversible step that got it here.
-- `idempotency_key` is UNIQUE: a retried "create payment" with the same key
-- is a single atomic INSERT attempt, never a read-then-write race.
CREATE TABLE IF NOT EXISTS payment_transactions (
    payment_id                  TEXT PRIMARY KEY,
    journey_reference           TEXT NOT NULL,
    booking_id                  TEXT NOT NULL,
    user_id                     TEXT,
    checkout_snapshot_id        TEXT NOT NULL,
    currency                    TEXT NOT NULL,
    customer_total_minor        INTEGER NOT NULL,
    status                      TEXT NOT NULL,
    provider                    TEXT NOT NULL,
    provider_payment_reference  TEXT,
    idempotency_key             TEXT NOT NULL UNIQUE,
    authorized_amount_minor     INTEGER NOT NULL DEFAULT 0,
    captured_amount_minor       INTEGER NOT NULL DEFAULT 0,
    refunded_amount_minor       INTEGER NOT NULL DEFAULT 0,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    version                     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_payment_booking ON payment_transactions (booking_id);
CREATE INDEX IF NOT EXISTS ix_payment_user ON payment_transactions (user_id);
CREATE INDEX IF NOT EXISTS ix_payment_provider_ref ON payment_transactions (provider_payment_reference);
CREATE INDEX IF NOT EXISTS ix_payment_status ON payment_transactions (status);

-- Append-only financial ledger (V9 Phase 4 §Q). Every irreversible money
-- action gets a row here, in order, and a row here is never updated or
-- deleted - the current-state row above can be rebuilt from this history if
-- it is ever in doubt. Never a payment credential in `detail`/`data_json`.
CREATE TABLE IF NOT EXISTS payment_events (
    event_id            TEXT PRIMARY KEY,
    payment_id          TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    occurred_at          TEXT NOT NULL,
    amount_minor        INTEGER,
    detail               TEXT NOT NULL DEFAULT '',
    data_json           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_payment_event_payment ON payment_events (payment_id, occurred_at);

-- Inbound provider event (webhook) receipt + dedup. The primary key IS the
-- idempotency mechanism: a duplicate or replayed webhook is a second INSERT
-- attempt against the same (provider, provider_event_id) and fails
-- atomically - never a SELECT-then-INSERT race. Out-of-order events are
-- tolerated by the payment state machine, not by this table.
CREATE TABLE IF NOT EXISTS payment_provider_events (
    provider            TEXT NOT NULL,
    provider_event_id   TEXT NOT NULL,
    payment_id          TEXT,
    event_type          TEXT NOT NULL DEFAULT '',
    received_at         TEXT NOT NULL,
    payload_json        TEXT NOT NULL DEFAULT '{}',
    processed           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, provider_event_id)
);
CREATE INDEX IF NOT EXISTS ix_provider_event_payment ON payment_provider_events (payment_id);

-- Refunds are their own entity, not a field on payment_transactions -
-- multiple refund attempts against one payment are multiple rows here.
-- `idempotency_key` UNIQUE is the same atomic-claim pattern as payments.
CREATE TABLE IF NOT EXISTS refunds (
    refund_id                  TEXT PRIMARY KEY,
    payment_id                  TEXT NOT NULL,
    amount_minor                INTEGER NOT NULL,
    currency                    TEXT NOT NULL,
    status                      TEXT NOT NULL,
    reason                      TEXT NOT NULL DEFAULT '',
    idempotency_key              TEXT NOT NULL UNIQUE,
    provider_refund_reference    TEXT,
    created_at                   TEXT NOT NULL,
    updated_at                   TEXT NOT NULL,
    version                      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_refund_payment ON refunds (payment_id);

-- Accounting/reconciliation-friendly breakdown of what one payment
-- economically covers (V9 Phase 4 §H) - never a duplicate of the V8.5
-- commercial engine, just a record of which components a captured payment
-- corresponds to. Written once, from the same PriceBreakdown/quote the
-- checkout snapshot froze.
CREATE TABLE IF NOT EXISTS payment_allocations (
    allocation_id        TEXT PRIMARY KEY,
    payment_id           TEXT NOT NULL,
    component            TEXT NOT NULL,
    label                TEXT NOT NULL DEFAULT '',
    amount_minor         INTEGER NOT NULL,
    currency             TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_allocation_payment ON payment_allocations (payment_id);

-- Reconciliation discrepancies (V9 Phase 4 §P) - classified, never
-- silently auto-corrected. Ops resolves them explicitly.
CREATE TABLE IF NOT EXISTS reconciliation_findings (
    finding_id           TEXT PRIMARY KEY,
    payment_id           TEXT NOT NULL,
    local_status         TEXT NOT NULL,
    provider_status      TEXT NOT NULL,
    classification       TEXT NOT NULL,
    detail               TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    resolved             INTEGER NOT NULL DEFAULT 0,
    resolved_at          TEXT
);
CREATE INDEX IF NOT EXISTS ix_reconciliation_payment ON reconciliation_findings (payment_id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_open ON reconciliation_findings (resolved);

-- V9 Phase 5: journey confirmations (Agent 1). Exactly one row per booking
-- (booking_id UNIQUE is the idempotency mechanism for a replayed/concurrent
-- finalizer run). booking_phase/payment_status are snapshots taken at
-- eligibility-evaluation time, never live joins.
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

-- V9 Phase 5: financial documents (Agent 2) - receipt / invoice / credit
-- note. An issued row is NEVER updated. A refund adds a CREDIT_NOTE row
-- pointing at the original via adjusts_document_id (does NOT invalidate
-- it); a correction adds a row via supersedes_document_id (does). Money is
-- integer minor units; NULL means UNKNOWN, never zero.
CREATE TABLE IF NOT EXISTS financial_documents (
    document_id               TEXT PRIMARY KEY,
    document_type             TEXT NOT NULL,
    document_number           TEXT NOT NULL UNIQUE,
    booking_id                TEXT NOT NULL,
    journey_reference         TEXT NOT NULL,
    user_id                   TEXT,
    currency                  TEXT NOT NULL,
    supplier_transport_minor  INTEGER,
    supplier_baggage_minor    INTEGER,
    supplier_fees_minor       INTEGER,
    detoura_markup_minor      INTEGER,
    detoura_service_fee_minor INTEGER,
    discount_minor            INTEGER,
    tax_minor                 INTEGER,
    customer_total_minor      INTEGER NOT NULL,
    captured_amount_minor     INTEGER NOT NULL,
    refunded_amount_minor     INTEGER NOT NULL DEFAULT 0,
    issued_at                 TEXT NOT NULL,
    adjusts_document_id       TEXT,
    supersedes_document_id    TEXT,
    is_production             INTEGER NOT NULL DEFAULT 0,
    idempotency_key           TEXT NOT NULL UNIQUE,
    pdf_blob                  BLOB
);
CREATE INDEX IF NOT EXISTS ix_findoc_booking ON financial_documents (booking_id);
CREATE INDEX IF NOT EXISTS ix_findoc_user ON financial_documents (user_id);
CREATE INDEX IF NOT EXISTS ix_findoc_adjusts ON financial_documents (adjusts_document_id);
CREATE INDEX IF NOT EXISTS ix_findoc_supersedes ON financial_documents (supersedes_document_id);

-- One row per numbering series (e.g. RCPT-2026). next_value is the last
-- number handed out, incremented inside a write transaction - never
-- read-then-written back.
CREATE TABLE IF NOT EXISTS document_numbering (
    series      TEXT PRIMARY KEY,
    next_value  INTEGER NOT NULL DEFAULT 0
);

-- V9 Phase 5: customer communications (Agent 3) - one logical
-- booking-confirmation email per booking; a resend is a new
-- communication_attempts row on the same communication, never a new one.
CREATE TABLE IF NOT EXISTS customer_communications (
    communication_id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL,
    journey_reference TEXT NOT NULL,
    user_id TEXT,
    channel TEXT NOT NULL,
    communication_type TEXT NOT NULL,
    status TEXT NOT NULL,
    recipient_address TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(booking_id, communication_type)
);
CREATE INDEX IF NOT EXISTS idx_communications_booking ON customer_communications(booking_id);
CREATE INDEX IF NOT EXISTS idx_communications_user ON customer_communications(user_id);

CREATE TABLE IF NOT EXISTS communication_attempts (
    attempt_id TEXT PRIMARY KEY,
    communication_id TEXT NOT NULL REFERENCES customer_communications(communication_id),
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    provider_message_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(communication_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_attempts_communication ON communication_attempts(communication_id);

CREATE TABLE IF NOT EXISTS communication_events (
    event_id TEXT PRIMARY KEY,
    communication_id TEXT NOT NULL REFERENCES customer_communications(communication_id),
    attempt_id TEXT REFERENCES communication_attempts(attempt_id),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail TEXT DEFAULT '',
    data_json TEXT DEFAULT '{}',
    FOREIGN KEY(communication_id) REFERENCES customer_communications(communication_id)
);
CREATE INDEX IF NOT EXISTS idx_events_communication ON communication_events(communication_id);
CREATE INDEX IF NOT EXISTS idx_events_occurred ON communication_events(occurred_at);
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
            self._relax_password_hash_nullable()
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

    def _relax_password_hash_nullable(self) -> None:
        """``CREATE TABLE IF NOT EXISTS`` only shapes a brand-new database -
        a database created before V9 Google auth already has
        ``user_accounts.password_hash`` as ``NOT NULL`` and SQLite has no
        ``ALTER COLUMN ... DROP NOT NULL``, so an existing deployment needs
        the standard SQLite rebuild-and-swap. A cheap ``PRAGMA table_info``
        check makes this a no-op on every startup after the first (fresh
        databases already get the nullable column straight from ``_DDL``
        above, so this never runs at all for them)."""
        cols = self._conn.execute("PRAGMA table_info(user_accounts)").fetchall()
        password_col = next((c for c in cols if c["name"] == "password_hash"), None)
        if password_col is None or password_col["notnull"] == 0:
            return
        self._conn.execute(
            "CREATE TABLE user_accounts__v9_migration ("
            " user_id TEXT PRIMARY KEY,"
            " email_normalized TEXT NOT NULL UNIQUE,"
            " password_hash TEXT,"
            " status TEXT NOT NULL DEFAULT 'ACTIVE',"
            " created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL,"
            " last_login_at TEXT)"
        )
        self._conn.execute(
            "INSERT INTO user_accounts__v9_migration "
            "SELECT user_id, email_normalized, password_hash, status,"
            " created_at, updated_at, last_login_at FROM user_accounts"
        )
        self._conn.execute("DROP TABLE user_accounts")
        self._conn.execute("ALTER TABLE user_accounts__v9_migration RENAME TO user_accounts")

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
