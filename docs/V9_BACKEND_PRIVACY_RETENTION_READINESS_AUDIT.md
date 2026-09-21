# V9 Backend Privacy / Retention Technical Readiness Audit

Scope: backend/server/database only (`src/detoura/**`). Complementary to
`docs/V9_FRONTEND_PRIVACY_READINESS_AUDIT.md` (Codex, frontend-only). This is
a technical audit — it does not decide legal bases, retention durations, or
consent requirements. Those are called out explicitly as **POLICY / LEGAL
DECISION REQUIRED**.

Starting checkpoint: `c23cd22` ("V9 connect consumer password recovery").
A concurrent frontend commit (`b7108ad`, "V9 audit frontend privacy
readiness") landed on `frontend/**` and `docs/V9_FRONTEND_PRIVACY_READINESS_AUDIT.md`
while this audit was in progress; it is preserved untouched.

---

## Executive Technical Status

No CRITICAL findings. No raw card/CVC data, no session/login/OAuth
authentication bypass after account deletion, no export IDOR, no unexpected
persistence of sensitive traveler PII (passport, DOB, nationality). The
codebase already has strong, consistently-applied primitives: hashed secrets
at rest, fail-closed sandbox defaults for every third-party provider
(Stripe/Duffel/Google/Resend), a structural logging filter plus a discipline
of never passing PII-bearing objects to loggers, and ownership-scoped
accessors for financial/booking data.

The real gaps are (a) one concrete technical defect, now fixed
(unauthenticated/unthrottled analytics ingestion endpoint), and (b) a set of
**already self-acknowledged** policy gaps: no retention policy exists for
almost any table holding account, booking, payment, or communication data,
and the data-export feature returns only account/identity/booking-ID
metadata, not a full data-subject export. Both are explicitly documented
in-code by the team as deliberate scope decisions pending a policy call, not
oversights.

---

## Backend Personal Data Inventory

| Category | Model / file | PII? | Persisted? | Current retention |
|---|---|---|---|---|
| UserAccount | `models/account.py`, `persistence/accounts.py` (`user_accounts`) | email | yes | INDEFINITE until deletion (then anonymized) |
| AuthIdentity (Google) | `auth_identities` | provider email/subject | yes | EXPLICIT-DELETE on account deletion; otherwise indefinite |
| Password credential | `user_accounts.password_hash` | secret (hashed) | yes (hash only) | Cleared (NULL) on deletion |
| Sessions | `auth_sessions` | pseudonymous (token hash) | yes | SESSION-LIFETIME (checked), rows never purged — **NO POLICY DEFINED** for row deletion |
| Password reset state | `password_reset_tokens` | secret (hashed) | yes | TTL enforced at consume-time; rows never purged — **NO POLICY DEFINED** |
| Google OAuth transient state | `google_oauth_pending`, `pending_google_links` | PKCE verifier/nonce (raw), state (hashed) | yes, short-lived | TTL enforced at consume-time; rows never purged — **NO POLICY DEFINED** |
| Saved/owned trips | `trip_ownership` | none directly (FK to booking) | yes | INDEFINITE — **NO POLICY DEFINED**, deliberately untouched by account deletion |
| TravelerParty / traveler request data | `models/traveler.py` | name, DOB, gender, nationality, passport (when collected) | **NOT PERSISTED** — no `travelers` table exists | SESSION-LIFETIME / in-memory only |
| BookingIntent / BookingRun / JourneyConfirmation | `persistence/bookings.py`, `journey_confirmations` | minimal (`lead_name`, `lead_email`) | yes | INDEFINITE — **NO POLICY DEFINED** |
| Travel Pass / ticket state, provider order refs | `booking_items`, `ticket_operations` | none (ids/prices/states only) | yes | INDEFINITE — **NO POLICY DEFINED** |
| CheckoutSnapshot | `checkout_snapshots` | pricing tied to user_id | yes | `expires_at` is a **payment-validity window, not a data-retention TTL**; rows never purged — **NO POLICY DEFINED** |
| PaymentTransaction, Refund, PaymentEvent, provider webhook events | `payment_transactions`, `refunds`, `payment_events`, `payment_provider_events` | financial, pseudonymous | yes | INDEFINITE — **NO POLICY DEFINED** |
| Financial documents (receipts/invoices/credit notes) | `financial_documents` (incl. `pdf_blob`) | billing detail | yes | INDEFINITE by design (immutable ledger) — **NO POLICY DEFINED** on how long to keep |
| Communication records | `customer_communications`, `communication_attempts`, `communication_events` | recipient email | yes | INDEFINITE — **NO POLICY DEFINED**; body/subject content is **not persisted** |
| Analytics/events | `analytics_events` | none (allow-listed event names/props, PII-shaped values rejected) | yes | INDEFINITE — **NO POLICY DEFINED**, no cleanup mechanism at all |
| Structured logs | `observability/logging.py` | none by design (forbidden-fields filter + structural discipline) | n/a (log sink, not audited here as a DB store) | n/a |
| Search/market intelligence (non-PII) | `price_observations`, `search_traces`, `market_priors` | none | yes | **TTL, actively enforced** (180d / 365d) — the one working retention mechanism in the codebase |

No secrets or real user data are reproduced above — categories only.

---

## Persistence / Database Inventory

Single SQLite database, schema defined entirely in `src/detoura/persistence/db.py`
(`_DDL`, `SCHEMA_VERSION=12`). No separate migrations framework exists;
schema evolution is via idempotent `_ADD_COLUMNS` ALTERs and one manual
rebuild-and-swap. No SQL files or ORM migration directory exists elsewhere
in the repository.

**Cleanup jobs — mechanism vs. policy, kept distinct:**

- A **real, working, automatic** TTL-prune mechanism exists for
  `price_observations`/`search_traces` (`persistence/price_memory.py::prune`,
  auto-invoked after every search) and a **manual-only** one for
  `market_priors` (ops endpoint). These are non-PII market/search-intelligence
  tables.
- **No scheduler, cron, or background daemon exists anywhere in the backend**
  (`src/detoura` has no APScheduler/Celery/cron usage). All other tables —
  `auth_sessions`, `password_reset_tokens`, `google_oauth_pending`,
  `pending_google_links`, `bookings`, `checkout_snapshots`,
  `payment_transactions`, `financial_documents`, `customer_communications`,
  `analytics_events` — have **no cleanup mechanism at all**. Expired/consumed
  rows accumulate indefinitely; expiry is enforced only logically (checked at
  read/consume time), never by deletion.
- This is stated explicitly in-code: `services/account_lifecycle_service.py`
  (lines 3–5) says "No legal retention policy is established for this
  project, and this module does not invent one." That is accurate and
  extends to every table above.

A technical cleanup mechanism existing (as for search intelligence) is not
the same claim as a retention *policy* existing — no table in this backend
has both.

---

## Account Deletion Matrix

Endpoint: `POST /api/v1/auth/account/delete` (`api/auth_account.py`) →
`account_lifecycle_service.delete_account` → `persistence/accounts.py::scrub_account_for_deletion`.

| Category | Deletion behavior | Classification |
|---|---|---|
| UserAccount | status → DELETED, email tombstoned, password_hash NULLed | ANONYMIZED |
| Sessions | all sessions for user revoked | ERASED (functionally — see Deleted-Account Security) |
| Password reset records | not touched | NOT HANDLED (harmless — see Deleted-Account Security) |
| Google identity (AuthIdentity) | row hard-deleted | ERASED |
| Saved trips (`trip_ownership`) | never touched, by explicit design comment | RETAINED |
| Communications (`customer_communications`) | not touched; `recipient_address` (real email) persists | RETAINED / POLICY DEPENDENT |
| BookingIntent / BookingRun / JourneyConfirmation / TravelerParty / PaymentTransaction / CheckoutSnapshot / financial documents / provider order refs / tickets | none of these tables are referenced by the deletion code path at all | RETAINED, by deliberate documented policy (`account_lifecycle_service.py`: do not cascade-delete booking/payment/financial truth absent an approved retention contract) |

Deletion does **not** cascade into financial/booking truth — this is a
documented current-behavior choice made by the team, not an oversight, and
this audit does not second-guess it (per audit scope: do not invent
anonymization rules, do not cascade-delete financial truth).

---

## Deleted-Account Security

Independently re-verified via code inspection (not just re-reading the
deletion function) — **no defect found**:

- **Existing session after deletion**: cannot authenticate. Sessions are
  revoked at deletion time *and* `validate_session` independently re-checks
  `AccountStatus.ACTIVE` on every call regardless of session-row state
  (defense in depth, not reliant on a single revoke).
- **Deleted password credential**: cannot authenticate — `login()` checks
  account status before password verification, and the hash is NULLed
  anyway.
- **Deleted Google identity**: cannot resurrect the old account — the
  `auth_identities` row is hard-deleted, and email-based lookup can no
  longer match because the email was tombstoned. A fresh Google sign-in
  creates a brand-new account rather than re-linking.
- **Password reset resurrecting a deleted account**: safe — `confirm_password_reset`
  re-checks `AccountStatus.ACTIVE` before applying a new password, even
  though a leftover unconsumed reset token row is never explicitly deleted
  (harmless hygiene gap, not a security issue).
- **ID reuse / IDOR via ID recycling**: no risk — account, session, and
  booking identifiers are randomly generated tokens (`secrets.token_urlsafe`),
  never derived from an autoincrement counter that could be reused. The only
  autoincrement IDs in the schema (`promo_redemptions`, `audit_events`,
  `analytics_events`) are not ownership-controlling keys.
- **Owner-only resource exposure via reused foreign keys**: not found —
  `trip_ownership.user_id` is never nulled or reassigned; the only field
  released for reuse is the tombstoned email address itself (affects future
  registration/lookup by email only, not any FK-based ownership check, since
  all ownership is `user_id`-keyed).

One non-blocking observation: releasing the original email as
re-registrable immediately after deletion is a deliberate design choice
(the tombstone is user_id-derived, not the freed email), standard for most
consumer apps, but worth confirming it matches product/legal intent.

---

## Data Export Matrix

Endpoint: `GET /api/v1/auth/account/export` (`api/auth_account.py` →
`account_lifecycle_service.export_account_data`). `user_id` is taken
exclusively from the authenticated session cookie — **no IDOR**: there is no
request parameter through which a caller could name another account.

| Category | Status |
|---|---|
| Account metadata | EXPORTED NOW |
| Linked auth identities (provider, provider_email, linked_at) | EXPORTED NOW (internal `provider_subject` deliberately excluded — reasonable, debatable) |
| Owned booking IDs | EXPORTED NOW (opaque IDs only) |
| Saved trips / itinerary detail | TECHNICALLY AVAILABLE BUT NOT EXPORTED |
| Travelers (party/traveler PII) | TECHNICALLY AVAILABLE BUT NOT EXPORTED (not persisted at all beyond the booking flow — see Traveler PII) |
| Payments | TECHNICALLY AVAILABLE BUT NOT EXPORTED |
| Checkout snapshots | TECHNICALLY AVAILABLE BUT NOT EXPORTED |
| Financial documents (incl. PDFs) | TECHNICALLY AVAILABLE BUT NOT EXPORTED |
| Communications | TECHNICALLY AVAILABLE BUT NOT EXPORTED |
| Tickets / provider order references | TECHNICALLY AVAILABLE BUT NOT EXPORTED |
| External-provider-side data (Stripe vault objects, live Duffel order detail) | EXTERNAL PROVIDER DATA — correctly out of scope for a local export |
| Whether booking/financial/communication history counts as "the user's data" for export purposes | POLICY / LEGAL SCOPE REQUIRED (the module's own docstring flags this explicitly) |

The export is honest about its own limits and does not overclaim
completeness. Extending it is a wiring exercise (user-scoped accessors
already exist for payments, financial documents, and communications) once
scope is decided — not a security defect, and not built here per the
audit's instruction not to expand export scope without a policy decision.

---

## Retention Matrix

| Record | Current technical retention | Configured duration | Cleanup mechanism | Policy status |
|---|---|---|---|---|
| Accounts | INDEFINITE (anonymized on deletion) | n/a | n/a | NO POLICY DEFINED |
| Auth identities | EXPLICIT-DELETE (on account deletion) / INDEFINITE otherwise | n/a | n/a | NO POLICY DEFINED |
| Sessions | SESSION-LIFETIME (checked) / rows never purged | 14 days default TTL (`auth_config.py`) | none | NO POLICY DEFINED (row purge) |
| Password reset records | Checked at consume time / rows never purged | 30 min default TTL | none | NO POLICY DEFINED |
| Google OAuth transient state | Checked at consume time / rows never purged | short-lived (`pending_ttl_seconds`) | none | NO POLICY DEFINED |
| Saved trips | INDEFINITE | n/a | none | NO POLICY DEFINED |
| Analytics/events | INDEFINITE | n/a | none | NO POLICY DEFINED |
| Traveler information | SESSION-LIFETIME (not persisted to DB) | n/a | n/a | N/A — nothing persisted to define a policy over |
| Booking intents/runs, journey confirmations | INDEFINITE | n/a | none | NO POLICY DEFINED |
| Provider order references / tickets | INDEFINITE | n/a | none | NO POLICY DEFINED |
| Payments, refunds, payment events | INDEFINITE | n/a | none | NO POLICY DEFINED |
| Checkout snapshots | INDEFINITE (expires_at is a *validity* window, not retention) | 15 min validity window only | none | NO POLICY DEFINED |
| Financial documents | INDEFINITE (immutable ledger by design) | n/a | none | NO POLICY DEFINED |
| Communications | INDEFINITE | n/a | none | NO POLICY DEFINED |
| Logs | out of DB scope (log sink) | n/a | n/a | not assessed here |
| Metrics | out of DB scope | n/a | n/a | not assessed here |
| (non-PII) Search intelligence | TTL, actively enforced | 180 days | automatic, per-search + manual ops endpoint | DEFINED (technical only — not represented as a legal retention policy) |
| (non-PII) Market priors | TTL, manual-only enforcement | 365 days | manual ops endpoint only | DEFINED (technical only) |

No duration has been invented for any table above beyond what is literally
configured in code.

---

## Traveler PII

Independently reverified against current HEAD (not assumed from the
historical Phase 6 conclusion): **holds true**. `Traveler`
(`models/traveler.py`) fields — name, DOB, gender/title, email, phone,
nationality, passport fields — are:

- Collected via `POST` traveler-submission endpoint, validated, and kept
  **in-memory only** on `BookingRun.party` for the duration of that booking
  run.
- Passed to Duffel at order time (`providers/duffel.py::duffel_passengers_from`)
  as: given_name, family_name, born_on, email, phone, gender, title. Passport
  fields are **not currently transmitted to Duffel even when populated** —
  the adapter explicitly drops `identity_documents` for the sandbox flows
  this build exercises. This is a functional gap for any future live-Duffel
  rollout requiring passport data (silently omitted, not a leak) — flagged
  for whoever owns that rollout, not a privacy defect.
- **Not persisted to the database at all** — no `travelers` table exists in
  the schema. Only `lead_name`/`lead_email` (the two minimal fields already
  documented as intentional) land in `bookings`.
- **Not present** in `CheckoutSnapshot`, `CommercialQuote`, financial
  documents, or the confirmation domain (which is explicitly scoped to
  `lead_name` + `party_size` only).
- **Not passed to analytics** — no caller feeds `Traveler` fields into the
  analytics recorder, and the recorder independently allow-lists prop keys
  and rejects PII-shaped values as defense in depth.
- **Not logged** — zero call sites found that pass a `Traveler`/`TravelerParty`
  object or its fields to a logger; `observability/logging.py`'s
  forbidden-fields filter (passport, DOB, email, etc.) is a secondary
  control, not the primary one.

---

## Authentication / Secret Data

| Artifact | Handling |
|---|---|
| Password | Argon2id hash, salt embedded in the encoded hash; stored hashed. Constant-shape login path (dummy hash comparison) avoids user-enumeration via timing. |
| Session token | Raw value returned once (HttpOnly cookie), never logged; only a SHA-256 hash is persisted. |
| CSRF token | Same pattern — raw once, SHA-256 hash persisted. Deliberately non-HttpOnly (double-submit pattern; correct, carries no bearer capability alone). |
| Password reset code | `secrets.token_urlsafe(32)`, raw value emailed (the delivery channel, expected), SHA-256 hash persisted, single-use enforced atomically, 30-min TTL. |
| Google OAuth `state` | Hashed before storage. |
| Google OAuth PKCE verifier / nonce | Stored **raw** (not hashed) in `google_oauth_pending` — minor inconsistency vs. `state`. Not HIGH severity: short-lived, single-use, no reuse value once consumed, never exposed to the browser. Documented here as a low-priority hardening opportunity, not treated as a defect requiring an immediate fix. |
| Google access/refresh/ID tokens | Explicitly discarded after use — never returned to any caller, never persisted. Only derived claims (`sub`, `email`) are stored. |
| Logging | A structural forbidden-fields filter blocks password/session/CSRF/OAuth/email/traveler fields from structured logs; verified true by inspection of every logging call site in the auth, payment, and provider layers — none pass a raw model/request object or `.dict()`/`.model_dump()` output containing secrets or PII into a log call. |

No raw secret found persisted unhashed or written to logs/exceptions.

---

## Payment Data

No raw card number, CVC, or bank credential is ever received or persisted
by this backend — `authorize()` only ever forwards an opaque, client-side
Stripe.js token. Detoura does not even store card brand/last4 metadata; that
remains entirely Stripe-side. What Detoura persists locally: Stripe object
IDs (PaymentIntent, refund), amounts/currency, status, an idempotency key,
and an append-only `PaymentEvent`/`payment_provider_events` ledger — the
latter stores a deliberately reduced form of the webhook payload
(`{"livemode":..., "object_id":...}`), never Stripe's full raw body.

`CheckoutSnapshot` rows (full frozen pricing/quote detail, tied to
`user_id`) have **no data-retention TTL** — the `expires_at` field only
governs whether the frozen price may still be authorized against, and no
purge job removes expired rows. This is a genuine technical gap between "the
snapshot is no longer usable" and "the snapshot's data still exists
indefinitely," logged in the Retention Matrix above as NO POLICY DEFINED
rather than fixed here, since deleting it unilaterally would remove
transaction-adjacent evidence without an approved retention decision.

---

## Financial Documents

Receipts/invoices/credit notes are structurally immutable: there is no
`update_document` function; corrections are new rows linked via
`supersedes_document_id`, and refunds are new `CREDIT_NOTE` rows linked via
`adjusts_document_id`. Download access is ownership-checked at two levels
(trip ownership + document ownership, both against the authenticated
session's `user_id`), returning a uniform "not found" for both nonexistent
and not-yours cases (anti-enumeration). `Cache-Control: no-store` is set on
downloads.

`user_id` on `financial_documents` has no FK constraint and is untouched by
account deletion — documents outlive account deletion by design, consistent
with the deletion policy documented above. No TTL or archival policy exists
for this table (indefinite by design, consistent with an immutable-ledger
model, but the retention *duration* question remains open — see Retention
Matrix). VAT/legal-invoice compliance is explicitly flagged elsewhere in the
codebase's own docstrings as an open item, not resolved by this audit.

---

## Communications

`CustomerCommunication.recipient_address` (email) is the only PII field on
the model and is persisted indefinitely; message subject/body content is
**not persisted** anywhere — it is passed straight to the provider at send
time and never written to any table. Delivery status uses a
`PENDING → SENDING → SENT/FAILED/UNKNOWN` state machine with atomic
send-slot claiming (no double-send race). **No blind auto-retry of
`UNKNOWN`** exists — the only path that resolves `UNKNOWN` calls the
provider's own `.retrieve()` for ground truth; resends are only reachable
via explicit user/ops action.

**Gap**: `customer_communications.recipient_address` and
`bookings.lead_email`/`lead_name` are **not scrubbed on account deletion** —
the deletion scrub function only touches `user_accounts`, `auth_sessions`,
and `auth_identities`. This is the same category of decision as the
deliberate "don't cascade into booking/financial truth" policy already
documented for bookings/payments, but communications were not explicitly
named in that carve-out. Classified as **POLICY-DEPENDENT GAP**, not a
technical defect fixed by this audit — anonymizing a communication history
tied to a specific delivered email is itself a retention/anonymization
policy decision this audit is instructed not to invent.

---

## Logging / Observability

No wholesale request/response body logging found anywhere in the API,
observability, or provider layers. Every logging call site inspected
(payments, bookings, OAuth, HTTP transport, Stripe/Resend adapters) logs
only IDs, enum values, status codes, and durations — never a raw model,
request object, or serialized dict containing PII/secrets. The correlation
middleware logs only method/route/status/duration, never headers or bodies.
Request IDs are validated against a strict pattern or replaced with a fresh
UUID4 — no possibility of a secret riding in via a client-supplied request
ID.

One observability (not privacy) gap noted: the core auth modules
(`api/auth.py`, `api/auth_account.py`, `services/password_service.py`,
`services/auth_service.py`) have **zero** logging calls of any kind — safe
from a leakage standpoint, but it means there is currently no structured
visibility into auth failures/abuse via logs. Not a privacy defect; flagged
as an operational observation only.

---

## Analytics/Event Backend

`POST /api/v1/events` is intentionally unauthenticated (public site) and
enforces genuine input minimization: an allow-list of event names, an
allow-list of prop keys, and a regex-based rejection of PII-shaped prop
values (email patterns, long digit runs, and field names like "name",
"email", "phone", "dob", "passport", "address"). Session/visitor identifiers
are random, anonymous-by-construction tokens.

**Technical defect found and fixed**: this was the only externally-reachable
endpoint in the codebase with no rate limiting at all (every other endpoint
— OAuth start/callback/link — already uses the existing `RateLimiter`
service). An unauthenticated, unthrottled client could previously POST
unlimited requests. Fixed in this audit (see Technical Defects below).

No coupling to authoritative booking/payment records was found — the
analytics persistence module only ever touches `analytics_events`; it
cannot corrupt or influence booking/payment truth.

---

## Third-Party Backend Integrations

| Provider | Purpose | Data sent | Credential mechanism | Default posture |
|---|---|---|---|---|
| Stripe | Payments (PaymentIntents, refunds, webhooks) | amount, currency, opaque payment-method reference only | `STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET` via env var | **Sandbox by default**; a separate `live_charging_enabled` kill-switch must be explicitly set, and an ambiguous/non-test key fails closed |
| Duffel | Flight search/offers/orders/tickets | passenger given_name/family_name/DOB, route/date/traveler-count at order time | `DUFFEL_ACCESS_TOKEN` via env var | **Fails closed to test mode** — a token not prefixed `duffel_test_` is rejected by the adapter itself |
| Google | Sign-in identity verification only | `sub`, `email`, `email_verified` claims (scope is `openid email` only — no profile/name scope requested) | `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` via env var | Fails closed — disabled (503) with no configuration; no token ever persisted |
| Resend | Transactional email (booking confirmations) | recipient email, email content | `RESEND_API_KEY` via env var, sent only as a Bearer header to a fixed host | **Sandbox adapter by default**; requires both a provider selection and an explicit `COMMUNICATION_LIVE_SENDING_ENABLED` flag to send live |

All four integrations share the same architectural pattern — sandbox/test
mode by default with an explicit, separate kill-switch required for
production credentials — which is the strongest consistency finding of this
audit. This audit does not classify controller/processor roles or DPA
status for any of these providers; that is a legal determination.

---

## User Privacy Control Backend Capabilities

| Capability | Status |
|---|---|
| Logout (server-side invalidation) | REAL & CONNECTED |
| Session revocation — all sessions | REAL & CONNECTED (triggered by password change/reset) |
| Session revocation — single, targeted session | PARTIAL (only "current" or "all" exist; no list/revoke-one-specific-session endpoint) |
| Password change | REAL & CONNECTED |
| Password reset | REAL & CONNECTED |
| Google Sign-In | REAL & CONNECTED |
| Google linking | REAL & CONNECTED |
| Google unlinking (single identity, account otherwise intact) | NOT BUILT |
| Account deletion | REAL & CONNECTED |
| Data export | PARTIAL (see Data Export Matrix) |
| Saved-trip deletion (single trip) | NOT BUILT |
| Analytics preference persistence | NOT APPLICABLE (backend does not gate ingestion on a stored consent preference; frontend consent handling is out of this audit's scope) |
| Marketing preference | NOT APPLICABLE (no marketing-communication feature found in backend) |

---

## Technical Defects

### Found and fixed

1. **Unauthenticated analytics endpoint had no rate limiting.**
   `POST /api/v1/events` accepted unlimited requests from any unauthenticated
   client with no throttling, unlike every other externally-reachable
   endpoint in the codebase (OAuth start/callback/link all use the existing
   `RateLimiter` service). This is a storage/abuse-exhaustion vector.
   **Fix applied**: `src/detoura/api/v1.py` now rate-limits `track_events`
   per client IP (120 calls / 60s window) using the same
   `services/rate_limit.RateLimiter` primitive and `_client_ip` helper
   already used by the Google OAuth endpoints, returning `429` when
   exceeded. This bounds abuse without constraining real browser telemetry
   (each call is already capped at 50 events by `TrackEventsRequest`).
   Focused tests (`tests/test_v85_ops_c2.py -k "event or funnel"`,
   `tests/test_v85_release_blockers.py`) pass; full regression run (see
   below).

### Found, not fixed (require a policy decision — see Policy-Dependent Gaps)

- Google OAuth PKCE verifier/nonce stored raw rather than hashed in
  `google_oauth_pending` — minor inconsistency, not a defect requiring an
  immediate code change (short-lived, single-use, no reuse value).
- No table-level cleanup/purge job exists for `auth_sessions`,
  `password_reset_tokens`, `google_oauth_pending`, or `pending_google_links`
  — expired/consumed rows accumulate. Building a scheduler is a
  nontrivial addition (no scheduler infrastructure exists in this backend at
  all) and the *duration* to purge at is itself a retention-policy question;
  not implemented here.
- Communications and `bookings.lead_email`/`lead_name` are not scrubbed on
  account deletion (see Communications section) — anonymization rule not
  invented per audit instructions.

---

## Policy-Dependent Gaps

- **No retention policy defined** for accounts, sessions, password-reset
  records, Google OAuth transient state, saved trips, bookings, checkout
  snapshots, payments, financial documents, communications, or analytics
  events. This is the single largest open item and is already
  self-acknowledged in-code by the team.
- **Data export scope** — whether booking/payment/financial/communication
  history counts as exportable "user data" versus Detoura's own
  transaction/accounting record.
- **Communications retention/anonymization on account deletion** — whether
  a delivered email's recipient address should be scrubbed, retained, or
  anonymized after the owning account is deleted.
- **CheckoutSnapshot retention duration** — a purge policy is needed
  independent of the existing (unrelated) payment-validity TTL.
- **Financial document retention duration** — tied to accounting/tax
  requirements this audit is explicitly barred from deciding.

---

## External / Operational Dependencies

- Building any table-level cleanup/purge job requires introducing scheduler
  infrastructure (none exists in this backend today — all existing pruning
  is either inline-after-request or a manual ops endpoint call).
- DPA/controller-processor status for Stripe, Duffel, Google, and Resend is
  a legal determination, not assessed here.
- VAT/legal-invoice compliance for financial documents is tracked
  separately (referenced in-code as an open item) and is out of this
  audit's scope.

---

## Limited Beta Backend Blockers

| Finding | Blocker class | Owner |
|---|---|---|
| Analytics endpoint had no rate limiting | Was a CONDITIONAL BLOCKER — now fixed (NON-BLOCKER) | BACKEND |
| No retention policy for any account/booking/payment/communication table | CONDITIONAL BLOCKER — technically fine to launch, but Legal/Privacy sign-off is expected before or shortly after Limited Beta | LEGAL/PRIVACY, PRODUCT |
| Data export is metadata-only, not a full data-subject export | CONDITIONAL BLOCKER (depends on what Limited Beta's privacy commitments promise users) | PRODUCT, LEGAL/PRIVACY |
| Communications/lead-email not scrubbed on account deletion | NON-BLOCKER for Limited Beta (consistent with the already-accepted booking/financial retention posture), but should be an explicit decision, not a silent gap | PRODUCT, LEGAL/PRIVACY |
| CheckoutSnapshot / financial document retention duration undefined | NON-BLOCKER for Limited Beta, CONDITIONAL BLOCKER before General Availability or a compliance audit | LEGAL/PRIVACY, PRODUCT |
| No single-session revocation, no Google unlink, no per-trip deletion | NON-BLOCKER — feature gaps, not privacy defects | PRODUCT |

No BLOCKER (highest class) findings remain — the sole item that would have
qualified (unthrottled public endpoint) is fixed.

---

## Inputs Required From Privacy / Legal

1. Approve or set retention durations for: sessions, password-reset/OAuth
   transient records, saved trips, bookings, checkout snapshots, payments,
   financial documents, communications, and analytics events.
2. Decide the intended scope of "data export" for Limited Beta users —
   metadata-only (current state) or a fuller data-subject export including
   bookings/payments/financial documents/communications.
3. Decide whether a deleted account's associated communication records
   (recipient email) and booking lead-contact fields should be anonymized,
   retained as-is, or handled differently.
4. Confirm the financial-document retention duration required by
   accounting/tax obligations (referenced but not resolved elsewhere in the
   codebase).
5. Confirm the product intent behind releasing a deleted account's email
   address for immediate re-registration (current behavior, not flagged as
   a defect, but worth an explicit sign-off).

This audit does not provide legal advice and defers all of the above to
Legal/Privacy/Product.
