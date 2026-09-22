# V9 Limited Beta — Retention Register

Structured, repository-wide retention register for every persisted category
identified by `docs/V9_BACKEND_PRIVACY_RETENTION_READINESS_AUDIT.md` and
`docs/V9_FRONTEND_PRIVACY_READINESS_AUDIT.md`, reconciled against the
Limited Beta policy lock (see
`docs/V9_LIMITED_BETA_PRIVACY_POLICY_IMPLEMENTATION_REPORT.md` §0).

This register **states current technical fact and, where the policy lock has
decided one, the applicable retention class**. It does **not** invent a
numeric duration for `STATUTORY_FINANCIAL` or `LEGAL_CLAIMS` categories, and
it does not convert any table to a fixed multi-year period absent a
legally-reviewed source for that period. Where a duration is genuinely
undecided, the class is `POLICY_PENDING` and the "Duration" column says so
explicitly rather than guessing.

Retention-class semantics (fixed vocabulary — do not extend without a
policy update):

`SECURITY_TTL` · `PRODUCT_30_DAY` · `OPS_14_DAY` · `STATUTORY_FINANCIAL` ·
`LEGAL_CLAIMS` · `ACTIVE_ACCOUNT_LIFECYCLE` · `PROVIDER_CONTROLLED` ·
`POLICY_PENDING` · `NO_PERSONAL_DATA`

---

## A. Backend database tables (SQLite, `src/detoura/persistence/db.py`)

| Category | Table(s) | Purpose | Personal data? | Retention class | Trigger / start event | Deletion / anonymization mechanism | Automatic / manual | External provider? | Status |
|---|---|---|---|---|---|---|---|---|---|
| Account identity | `user_accounts` | Sign-in identity | Yes (email) | `ACTIVE_ACCOUNT_LIFECYCLE` | Account creation → account deletion | `scrub_account_for_deletion`: email tombstoned, password hash cleared, status → DELETED | Automatic, on deletion request | No | IMPLEMENTED |
| Linked auth identity | `auth_identities` | Google Sign-In link | Yes (provider email) | `ACTIVE_ACCOUNT_LIFECYCLE` | Identity link → account deletion | Hard-deleted in the same deletion transaction | Automatic, on deletion request | Google (identity claims only) | IMPLEMENTED |
| Sessions | `auth_sessions` | Authenticated session | Pseudonymous (token hash) | `SECURITY_TTL` | Session creation, 14-day default TTL (`auth_config.py`) | Revoked at deletion/logout/password change; **row purge after expiry not implemented** (no scheduler exists) | Logical expiry: automatic. Row purge: NOT BUILT | No | PARTIAL — expiry enforced, row purge is a POLICY_PENDING/engineering item (see §Pre-Beta Gate) |
| Password reset tokens | `password_reset_tokens` | One-time reset code | Secret (hashed) | `SECURITY_TTL` | Reset request, 30-min default TTL | Consumed atomically; unconsumed/expired rows never purged | Logical expiry: automatic. Row purge: NOT BUILT | No (delivery via Resend, no content stored) | PARTIAL |
| Google OAuth transient state | `google_oauth_pending`, `pending_google_links` | PKCE/state round trip | PKCE verifier/nonce (raw), state (hashed) | `SECURITY_TTL` | OAuth start, short TTL (`pending_ttl_seconds`) | Consumed atomically; unconsumed/expired rows never purged | Logical expiry: automatic. Row purge: NOT BUILT | Google (transport only) | PARTIAL |
| Saved/owned trips | `trip_ownership` | Booking ownership record | No PII directly (FK only) | `POLICY_PENDING` | Booking claimed | None — deliberately untouched by account deletion (RET-2) | Manual only if ever decided | No | NO POLICY DEFINED |
| Bookings / booking items | `bookings`, `booking_items` | Operational booking record incl. `lead_name`/`lead_email` | Yes (lead contact only) | `POLICY_PENDING` | Booking created | None — booking truth, never touched by account deletion (deliberate, ratified stance — see Implementation Report §3) | None | Duffel (order references only) | NO POLICY DEFINED |
| Journey confirmations | `journey_confirmations`, `confirmation_events` | Confirmation domain state | Minimal (`lead_name`, `party_size`) | `POLICY_PENDING` | Booking confirmed | None | None | No | NO POLICY DEFINED |
| Checkout snapshots | `checkout_snapshots` | Frozen price/quote at payment time | Pseudonymous (`user_id`) | `POLICY_PENDING` | Payment attempt started | None — `expires_at` governs payment-validity only, not row deletion (see Implementation Report §6) | None | No | NO POLICY DEFINED |
| Payments / refunds / events | `payment_transactions`, `refunds`, `payment_events`, `payment_provider_events` | Financial transaction ledger | Pseudonymous, financial | `STATUTORY_FINANCIAL` / `LEGAL_CLAIMS` (exact split undecided) | Payment created | None — immutable ledger, never touched by account deletion | None | Stripe (opaque references only) | NO DURATION DEFINED — Legal/Accounting input required; no duration invented here |
| Financial documents | `financial_documents`, `document_numbering` | Receipts / invoices / credit notes | Yes (billing detail, PDF) | `STATUTORY_FINANCIAL` | Document issued | None — structurally immutable, never touched by account deletion | None | No | NO DURATION DEFINED — tied to accounting/tax/VAT obligations, out of engineering's authority to set |
| Customer communications | `customer_communications` | Delivery record incl. recipient email | Yes (`recipient_address`) | `ACTIVE_ACCOUNT_LIFECYCLE` (deletion-triggered scrub) **+** `POLICY_PENDING` (standing duration for accounts never deleted) | Communication created → account deletion (if it happens) | **NEW, this slice**: `communications.scrub_recipient_for_user` anonymizes `recipient_address` for every communication tied to the deleted `user_id`; row, status, and ledger are otherwise untouched | Automatic, on deletion request. Standing (non-deletion) retention: NOT BUILT | Resend (delivery only; body/subject never persisted) | IMPLEMENTED for the deletion trigger; standing duration still open (Pre-Beta Question 9) |
| Communication attempts / events | `communication_attempts`, `communication_events` | Send-attempt and ledger history | No (no recipient address; `data` field structurally rejects `@`) | `NO_PERSONAL_DATA` | Communication created | None needed — contains no PII | N/A | Resend (provider message IDs only) | NO ACTION NEEDED |
| Analytics/events | `analytics_events` | Allow-listed event telemetry | No (PII-shaped values rejected at ingestion) | `POLICY_PENDING` (once analytics is ever enabled) / `NO_PERSONAL_DATA` (current content) | Event received (currently: no production consent grant exists, so effectively unused) | None | None | No | FAIL-CLOSED — no rows are created today; duration only matters if/when analytics is enabled (ANL-4) |
| Search / market intelligence | `price_observations`, `search_traces`, `market_priors` | Non-PII search/pricing intelligence | No | `NO_PERSONAL_DATA` | Search performed | Working TTL prune (`persistence/price_memory.py::prune`, auto-invoked; `market_priors` via manual ops endpoint) | `price_observations`/`search_traces`: automatic. `market_priors`: manual only | No | DEFINED (technical only — this is not a stand-in for a PII retention policy; see RET-9) |
| Audit events | `audit_events` | Append-only administrative audit trail | Pseudonymous (`user_id` as actor) | `LEGAL_CLAIMS` (audit trail retained for dispute/security-incident purposes) | Action performed | None | None | No | NO DURATION DEFINED |

## B. Client-side / browser storage (frontend, out of backend's write authority — tracked here for completeness)

| Category | Storage key | Retention class per policy lock | Current frontend implementation | Status |
|---|---|---|---|---|
| Journey draft | `detoura-journey-draft-v1` (localStorage) | `PRODUCT_30_DAY` (**locked**: "Journey draft client retention: 30 days maximum. Frontend responsibility.") | INDEFINITE — no TTL enforced (per frontend audit §3) | **OPEN GAP vs. locked policy** — frontend engineering item, out of this backend slice's ownership; flagged for Frontend/Product to implement the 30-day cap |
| Saved recommendations | `detoura-saved` (localStorage) | `POLICY_PENDING` — the 30-day lock names journey draft only, not saved recommendations | INDEFINITE — no TTL/count cap | NO POLICY DEFINED (RET-8) |
| Theme preference | `detoura-theme` (localStorage) | `NO_PERSONAL_DATA` | INDEFINITE, no PII | NO ACTION NEEDED |
| Attribution | `detoura.attribution.v1` (sessionStorage) | `POLICY_PENDING` | Session + 30-day TTL checked on read; inactive without consent | FAIL-CLOSED ACCEPTABLE PENDING DECISION |
| Measurement session/visitor IDs | `detoura.fk.s` / `detoura.fk.v` | `POLICY_PENDING` | Inactive without consent | FAIL-CLOSED ACCEPTABLE PENDING DECISION |

## C. Operational log retention

**Locked policy**: ordinary identifiable operational log retention is
**14 days maximum** (`OPS_14_DAY`), with an exception for records tied to an
active, documented security incident (which may be retained longer for the
duration of that incident's investigation) or another specifically-retained
record (e.g., an audit-trail row already covered by §A above).

**Technical fact**: this backend's own structured logger
(`observability/logging.py`) applies a forbidden-fields filter that blocks
PII/secrets from ever reaching a log line — verified true by inspection of
every logging call site (see the backend audit's Logging section) — but log
**retention** (how long a written log line survives) is controlled entirely
by the deployment/logging infrastructure (log shipper, hosting platform, log
sink retention setting), not by any code in `src/detoura`. No application
code enforces or can enforce a log-retention duration, because there is no
log storage layer inside this codebase to enforce it against.

**Explicit Ops/Deployment requirement (this document does not, and cannot,
implement this in application code)**:

> Staging and production log infrastructure (whatever ships/aggregates
> `src/detoura`'s structured log output — e.g. the hosting platform's log
> retention setting, a log-shipper's TTL, or the object-storage lifecycle
> policy of any log archive) MUST be configured to retain ordinary
> identifiable operational logs for **14 days or fewer**, except for logs
> specifically preserved for the duration of an active, documented security
> incident investigation.

This requirement is **OPS/DEPLOYMENT**, not **TECHNICAL IMPLEMENTATION** —
no code change in this repository can satisfy it; an operator must configure
the deployed logging platform to match. Do not close this item by pointing
at the forbidden-fields filter — that control prevents *what* gets logged,
not *how long* a log survives.

## D. What this register does not decide

- No numeric duration for `STATUTORY_FINANCIAL` or `LEGAL_CLAIMS` beyond
  what is already configured in code (i.e., none — this register documents
  the absence of a duration honestly rather than inventing one).
- No decision on whether `trip_ownership`/booking/payment/checkout-snapshot
  retention should ever expire — Pre-Beta Policy Questions 6–8 remain for
  Legal/Privacy + Product.
- No automation is built by this register. Building any table-level
  purge/cleanup job requires new scheduler infrastructure (none exists in
  this backend today — see the backend audit's External/Operational
  Dependencies section); that is scoped as its own engineering item once
  durations are decided.
