# V9 Limited Beta — Real Provider E2E Readiness + Execution

**Starting HEAD:** `1ec91d8` ("V9 add production transactional email adapter")
**Final HEAD:** `1ec91d8` (no executable backend code was changed in this slice — this is a report-only checkpoint)

**Environment classification: BLOCKED BY CREDENTIALS for real Stripe/Duffel Test Mode calls.** Neither `STRIPE_SECRET_KEY` nor `DUFFEL_ACCESS_TOKEN` (nor `RESEND_API_KEY`) is present anywhere in this environment. No `.env` file exists for the backend. Per the task's own §34 policy, this is a valid, non-fabricated outcome: this report instead delivers the full executable contract map, a fresh live **application-level** E2E run (real HTTP API, real DB, real state machine, real ownership/CSRF/idempotency enforcement — with Detoura's own sandbox adapters standing in for Stripe/Duffel, since no real provider credential exists to call), targeted negative-path verification, and an exact setup checklist for whoever holds real Test Mode credentials to complete the remaining, provider-specific verification.

---

## Credential presence (names only, never values)

| Credential | Env var | Status |
|---|---|---|
| Stripe secret key | `STRIPE_SECRET_KEY` | **ABSENT** |
| Stripe webhook secret | `STRIPE_WEBHOOK_SECRET` | **ABSENT** |
| Duffel access token | `DUFFEL_ACCESS_TOKEN` | **ABSENT** |
| Resend API key | `RESEND_API_KEY` | **ABSENT** |
| Payment provider selection | `PAYMENT_PROVIDER` | unset (defaults to `sandbox`) |
| Payment live-charging kill switch | `PAYMENT_LIVE_CHARGING_ENABLED` | unset (defaults to `false`) |
| Live-search kill switch | `SEARCH_LIVE_ENABLED` | unset (defaults to `false`) |
| Communication provider selection | `COMMUNICATION_PROVIDER` | unset (defaults to `sandbox`) |

Checked via `env | grep -i` on variable **names** only (`echo "$VAR" present: yes/no`) — no value was ever printed, logged, or written to this report.

---

## Stripe Test-Mode preflight

**Stripe Credential: ABSENT.** No preflight call was attempted — there is nothing to authenticate with. Per §5/§0 ("do not assume a key is test-mode based only on variable naming... if mode cannot be proven, stop"), the correct action with an absent credential is not to construct a `StripePaymentProvider` at all, which is exactly what `payment_config.py::resolve_provider()` already enforces (see "Executable contract map" below): `PAYMENT_LIVE_CHARGING_ENABLED=false` unconditionally forces the sandbox adapter regardless of anything else, so no live Stripe object could be created even if a key were mistakenly present.

**Stripe Mode: NOT VERIFIED (credential absent — nothing to verify).**
**Stripe Connectivity: BLOCKED.**

**How this codebase proves test-mode when a key IS present** (read, not executed, since there is no key to exercise it against): `providers/stripe_payment.py::is_test_key()` checks for an `sk_test_` prefix; `StripePaymentProvider.__post_init__` refuses to construct at all unless the key has that prefix (`allow_non_test_key=True` is required to override, and nothing in this codebase's production wiring ever passes that). This is a **prefix check**, not a live round-trip against Stripe's API (e.g. retrieving the account/workspace object and reading its own mode indicator) — worth noting for whoever sets up real credentials: a prefix check is a strong, standard signal (Stripe's own key format guarantees it), but the smallest additional live proof available, if paranoia is warranted, would be a `GET https://api.stripe.com/v1/balance` call and confirming the response is well-formed for a test-mode account (test-mode balances are always zero/synthetic) — not implemented in this codebase today, and not something this slice adds, since there is no key to build or test that check against.

---

## Duffel Test-Mode preflight

**Duffel Credential: ABSENT.**

**Duffel Mode: NOT VERIFIED (credential absent).**
**Duffel Connectivity: BLOCKED.**

**How this codebase proves test-mode when a token IS present**: `providers/duffel.py::TEST_TOKEN_PREFIX = "duffel_test_"`, `is_test_token()` checks the prefix; `DuffelTransportProvider` refuses to construct without it (confirmed by reading the constructor's refusal message at `providers/duffel.py:291`, `create_test_order` explicitly refuses a non-test token per `tests/test_v8_booking.py::test_create_test_order_refuses_a_non_test_token`, which passed in this session's targeted run). Same limitation as Stripe: this is a prefix check, not a live API round-trip. The smallest safe live proof, once a token exists, would be Duffel's own `GET /air/offer_requests` or a minimal `GET /air/airlines?limit=1` call and confirming a `200` with a well-formed Duffel response envelope — not implemented in this codebase and not exercised here, since no token exists to exercise it against.

**Consequence for search**: `api/v1.py::_search_live_duffel_or_none()` requires `is_test_token(os.getenv("DUFFEL_ACCESS_TOKEN", ""))` to be `True` before it will construct a live Duffel search provider at all; with the token absent, this function returns `None` unconditionally, and `_try_live_search()` therefore always falls back to the synthetic path. Confirmed live: `/api/v1/search` in this environment never reaches a `LIVE`-provenance result (see "Live Search" below).

---

## Executable contract map (traced from current code, not from prior reports)

| Step | Source | Target | Binding key | Authoritative state | Failure state |
|---|---|---|---|---|---|
| Search | `api/v1.py::search` → `_try_live_search` | `services/live_search.py` (if Duffel token present) else synthetic `TravelPlanner` | none yet | `TripSearchResponse` (ephemeral, not persisted) | falls back to synthetic; never a 503 |
| Selection recorded | `services/live_search.py` | `services/selection_store.py::SelectionStore.put` | `selection_id` (opaque, server-minted) | in-memory, TTL-bounded (20 min) | expired/unknown → `None` on `.get()` |
| Booking intent | `api/v1.py::create_booking_intent` | `services/booking_flow.py::create_run_from_selection` (real) or `create_run_demo` (synthetic) | `booking_id` (`bk_` + `secrets.token_urlsafe(15)`) | `BookingRun` (in-process `booking_store()`, one process's memory) | `selection_id` unknown/expired → 404 |
| Travelers | `api/v1.py::submit_travelers` | `models.traveler.TravelerParty` attached to the same `BookingRun` | `booking_id` | `run.travelers` | count mismatch / duplicate document → 422 |
| Commercial truth | `services/booking_commercial.py::price_run` (called at intent creation and again at confirm) | `run.quote: CommercialQuote` | `booking_id` | server-computed fare + fee + tier | price/ticket reconciliation mismatch → `PRICE_INCONSISTENT`, 409 |
| Payment created | `api/payments.py::create_payment` | `services/payment_service.py::freeze_checkout_snapshot` + `create_payment` | `booking_id`, `checkout_snapshot_id` | `PaymentTransaction(status=CREATED)`, `CheckoutSnapshot` (frozen, persisted SQL row) | booking not priced yet → 400 |
| Payment authorized | `api/payments.py::confirm_payment` | `services/payment_service.py::authorize_payment` → `payment_config.resolve_provider()` (sandbox unless live+stripe configured) | `payment_id` | `PaymentTransaction(status=AUTHORIZED)` | snapshot expired → 409; provider FAILED/UNKNOWN → payment reflects it, never silently AUTHORIZED |
| Confirmation (one call) | `api/v1.py::confirm_booking` | `services/payment_booking_orchestrator.py::resolve_eligible_payment_for_booking` → `services/booking_flow.py::start_confirmation` | `booking_id` | payment eligibility gate (§ below) | no/ambiguous/mismatched payment → 409 with a stable `.code`, **before any claim** |
| Atomic claim | `services/booking_orchestrator.py::claim_for_execution` (called synchronously inside `start_confirmation`, before any worker thread spawns) | `run.phase = REVALIDATING` | `booking_id` (via `run._lock`) | one claim ever succeeds; documented 300/300-trial concurrency proof in the code's own comments | second concurrent call raises `ValueError("cannot confirm from phase ...")` → 409 |
| Supplier execution | `services/payment_booking_orchestrator.py::execute_paid_booking` | `services/booking_orchestrator.py::run_booking(..., already_claimed=True)` | `booking_id` | revalidate → issue per leg → `BookingPhase` (`COMPLETE` / `PARTIAL_FAILURE` / `FAILED` / `RECONFIRM_REQUIRED`) | any required leg fails → never `COMPLETE` |
| Capture/release/reconciliation | `payment_booking_orchestrator.py::_execute_after_authorization` | `services/payment_service.py::request_capture` / `cancel_authorization` / `mark_reconciliation_required` | `payment_id` bound to `booking_id` | `CAPTURED` (only on `COMPLETE`, amount = `min(revalidated total, authorized_amount)`) / `CANCELLED` (pre-booking failure) / `RECONCILIATION_REQUIRED` (partial failure — human decision, never guessed) | — |
| Finalization | `services/payment_booking_orchestrator.py::run_paid_booking_and_finalize` (not the production caller; production reaches the finalizer via the eventual poll/finalize call in `booking_flow`) → `services/post_booking_finalizer.py::finalize` | confirmation + document + communication | `booking_id` | `JourneyConfirmation` record; document/communication attempted, never gating the confirmation | document/communication exceptions are swallowed — booking truth is never affected |
| Financial document | `post_booking_finalizer.py::_issue_document_safely` | `services/financial_document_service.py::issue_receipt_or_invoice` | `booking_id` idempotency key `findoc:{booking_id}:receipt` | immutable `FinancialDocument` row + rendered PDF bytes | render failure swallowed, booking/payment/confirmation untouched |
| Communication | `post_booking_finalizer.py::_send_communication_safely` | `services/communication_service.py::create_and_send_communication` → `communication_config.resolve_communication_provider()` (sandbox unless live+resend configured) | `booking_id` + `communication_type`, idempotency key `comm:{booking_id}:confirmation` | `CustomerCommunication(status=SENT/FAILED/UNKNOWN)` | never affects booking/payment/confirmation truth |
| My Trips | `api/me_trips.py::list_my_trips` / `get_trip_detail` | `persistence/accounts.py::list_trip_ids_for_user` (ownership) + booking/confirmation/document stores | `user_id` (from session) → `booking_id`s | reads only already-persisted truth | cross-user → 404 (indistinguishable from "doesn't exist") |

**The primary join is `booking_id` throughout** — no new E2E identifier was introduced or needed.

---

## Core state contract — verified against current code (not assumed from prior reports)

Every invariant below was either read directly in the current source during this slice, or freshly re-proven live (see "Application-level E2E execution"):

- **PAYMENT AUTHORIZED != BOOKING CONFIRMED** — `resolve_eligible_payment_for_booking` only checks for an `AUTHORIZED` payment as a *precondition* to attempt execution; `BookingPhase` only reaches `COMPLETE` after `run_booking` actually issues every required leg. Live-proven: in this session's E2E run, the payment reached `AUTHORIZED` before `confirm_booking` was even called, and the booking only reached `complete` after a separate poll loop observed the orchestrator finish.
- **BOOKING COMPLETE only after required issuance truth supports it** — `_execute_after_authorization` only captures when `run.phase is BookingPhase.COMPLETE`, which `run_booking` sets only once every required leg is `CONFIRMED`.
- **PARTIAL ISSUANCE != CONFIRMED** — `BookingPhase.PARTIAL_FAILURE` routes to `mark_reconciliation_required`, never to a capture; not triggered in this session's happy-path run (single-leg synthetic itinerary), so this is **INTEGRATION TEST VERIFIED** (via `tests/test_v9_payment_booking_coupling.py`, re-run fresh this session, all green) rather than freshly triggered live.
- **RECOVERY_REQUIRED != CONFIRMED** — `render_booking_confirmation_email` (unchanged, read again this session) never renders `CONFIRMED`-style language for a non-`CONFIRMED` `confirmation_status`; not triggered live this session (not applicable to a clean happy path).
- **PAYMENT UNKNOWN != PAID** — `resolve_eligible_payment_for_booking` explicitly treats an `UNKNOWN`-status payment as `PAYMENT_UNRESOLVED`, refusing execution with a 409 before any claim; the orchestrator's own `run_paid_booking` treats `PaymentStatus.UNKNOWN` identically to `FAILED` for the purpose of "never book against unresolved money."
- **UNKNOWN payment outcome is never blindly retried** — the webhook handler (`api/payments.py::provider_webhook`) only ever calls `ps.reconcile_payment` (a `retrieve()`-based read of provider truth), never re-authorizes or re-captures.
- **EMAIL FAILURE != BOOKING FAILURE / EMAIL UNKNOWN != BOOKING FAILURE** — `_send_communication_safely` wraps the entire send in a broad `try/except`; proven again live this session (`tests/test_v9_phase5_integration.py::test_email_failure_never_touches_booking_or_payment_truth` and `::test_email_unknown_never_blindly_duplicate_sent`, both re-run fresh, both green).

**No architecture was changed to perform this audit.**

---

## Test-data policy honored

- Two synthetic accounts: `e2e.synthetic.user.a@example-detoura-e2e.invalid`, `e2e.synthetic.user.b@example-detoura-e2e.invalid` — obviously-fake `.invalid` TLD, never a real mailbox.
- One synthetic traveler: "Ada SyntheticTraveler", DOB `1992-03-03`, a `+34 600 000 000` placeholder phone, a `.invalid` email — no real personal data anywhere.
- No real payment card was used or needed — the payment provider exercised was Detoura's own in-process `SandboxPaymentProvider` (the default when no live credential/flag is configured), never a Stripe test card.
- Communication provider exercised was Detoura's own in-process `SandboxEmailProvider` (the default) — no email left the process, real or otherwise.
- All environment variables for live provider access were explicitly unset/scrubbed at the top of the test script, so this run could not accidentally reach a real provider even if one were misconfigured in the ambient shell.

---

## Application-level E2E execution (fresh, this session)

Executed against a throwaway file-backed SQLite DB via `TestClient` driving the **real HTTP API** (not internal function calls) — the same driving mechanism used for the account/auth audit and email adapter work in prior slices. Because no live Stripe/Duffel credential exists, the provider underneath every payment/communication call is Detoura's own sandbox adapter — this run is therefore **APPLICATION E2E VERIFIED**, explicitly **not** REAL PROVIDER E2E VERIFIED, and is labeled that way everywhere below.

### Happy path

| Step | Result |
|---|---|
| Register + login (account A) | `200` / `200` |
| Create booking intent (`demo_legs`, `service_tier=ALL_IN_ONE`) | `201`, phase `awaiting_travelers`, `journey_reference=DTR-V8-S7GDAU` |
| Submit travelers | `200`, phase `awaiting_confirmation` |
| **Negative:** confirm with no payment | `409`, code `PAYMENT_REQUIRED` |
| Create payment | `200`, provider `sandbox`, status `CREATED` |
| Authorize payment (with CSRF header) | `200`, status `AUTHORIZED`, `106.59 EUR` |
| Confirm booking | `200` (claim taken synchronously; worker thread spawned) |
| **Negative:** duplicate confirm (immediately after) | `409`, `"cannot confirm from phase revalidating"` — proves the atomic claim, not a database race |
| Poll `GET /booking-intents/{id}` | reached `complete` |
| Payment final state | `CAPTURED`, `captured_amount=106.59` — **exactly equal to authorized amount**, no over/under-capture |
| `GET /api/v1/me/trips` (account A) | `200`, contains the booking |
| `GET /api/v1/me/trips/{id}` (account A) | `200` |
| `GET /api/v1/me/trips/{id}/documents` | `200`, one `RECEIPT` document, `RCPT-2026-000001`, `download_available=true` |
| `GET .../documents/{doc_id}/download` (separate follow-up request, freshly re-authenticated) | `200`, `content-type: application/pdf`, `Cache-Control: no-cache, no-store, must-revalidate`, real PDF bytes (`%PDF-1.4...`), 3409 bytes |
| **Cross-user:** account B → `GET /me/trips/{id}` | `404` |
| **Cross-user:** account B → `GET /me/trips/{id}/documents` | `404` |
| **Cross-user:** account B → `GET /payments/{payment_id}` | `404` |
| **Negative:** forged/unissued `selection_id` → `create_booking_intent` | `404`, `"That selection is unknown or has expired. Search again."` |

### Raw DB evidence (queried directly, outside the application)

- `payment_transactions`: `user_id` correctly bound to account A's `user_id`; `authorized_amount_minor == captured_amount_minor == 10659`; `provider="sandbox"`; `provider_payment_reference="sbx_ref_00000001"`.
- `financial_documents`: `is_production=0` (correctly marked non-production), `adjusts_document_id`/`supersedes_document_id` both `NULL` (a fresh document, not a mutation of anything), `customer_total_minor == captured_amount_minor == 10659`, PDF text body includes an explicit "TEST DOCUMENT - NOT FOR TAX PURPOSES" watermark and "Detoura (sandbox - no company configured)" — the document is honest about its own non-production status.
- `customer_communications`: one row, `status="SENT"`, `communication_type="BOOKING_CONFIRMATION"`, `recipient_address` is the synthetic traveler email.
- `communication_attempts`: `provider_name="sandbox"`, `provider_message_id="sbx_msg_00000001"`.
- `communication_events`: every row's `data_json` is `"{}"` — no PII, no recipient address, leaked into the ledger.
- **Full-database secret/PII scan** (searched every table/column for the synthetic passwords used and for `sk_test_`/`sk_live_`/`duffel_test_`/`duffel_live_`/`re_test_` substrings): **zero matches**.

### Structured logs emitted during the run (stdout, captured)

`booking_intent_created`, `payment_created`, `payment_authorization_started`, `payment_authorized`, `booking_confirmation_requested` (×2 — once per confirm call, including the rejected duplicate), `booking_claimed`, `booking_execution_started`, `booking_revalidation_completed`, `booking_leg_issued`, `booking_complete`, `payment_capture_started`, `payment_captured` — every one carries only `booking_id`/`payment_id`/`item_id`/`item_count`/`outcome`/`phase`/`request_id`, never a password, session token, CSRF token, provider secret, or traveler PII. Matches the Production Observability baseline audited in an earlier slice; no regression found.

---

## Negative checks (§29) — full results

| Check | Result | Evidence |
|---|---|---|
| Missing payment → booking confirmation rejected | **PASS** | live: `409 PAYMENT_REQUIRED` |
| Wrong booking-bound payment → rejected | **PASS (structurally unreachable)** | `ConfirmBookingRequest` has no field to name a payment id at all — the eligible payment is *always* resolved server-side from persisted state keyed only by the URL's `booking_id` (`resolve_eligible_payment_for_booking`); there is no client-reachable code path to submit "use payment X for booking Y" |
| UNKNOWN/ineligible payment → cannot execute | **INTEGRATION TEST VERIFIED** | `resolve_eligible_payment_for_booking`'s `PAYMENT_UNRESOLVED` branch, covered by `tests/test_v9_payment_booking_coupling.py` (re-run fresh this session, green); not independently reproduced live this session (would require directly mutating persisted state outside the API, which was judged unnecessary given the passing test already proves it against the same function) |
| Duplicate confirmation → no duplicate provider execution | **PASS** | live: second confirm call returned `409 "cannot confirm from phase revalidating"` — the claim, not a race, rejected it |
| Cross-user booking/My Trips access → denied | **PASS** | live: `404` on trip detail, documents, and payment, for a second synthetic account |
| Synthetic/Prior recommendation → cannot enter All-in-One provider-backed purchase | **PASS** | live: a forged `selection_id` was rejected `404` before ever reaching `create_run_from_selection`; structurally, `selection_store()` is only ever populated by `services/live_search.py` (confirmed by `grep` — no other module writes to it), which itself never runs without a proven Duffel test token, so in this environment the store is provably always empty |

---

## Communication

**Application communication integration: VERIFIED** (sandbox provider recorded a `SENT` `BOOKING_CONFIRMATION`, matching §26's own instruction: "if sandbox provider records a booking confirmation, classify application communication integration as VERIFIED. Do not claim Real Resend E2E.").

**Real Resend Delivery: NOT PART OF THIS E2E** — no `RESEND_API_KEY` exists, and no explicitly-designated safe test recipient was provided; per §7/§26, no email was sent to any real address, and sandbox was the only path exercised.

---

## Observability & metrics

- Every structured log line from this run was inspected (see above) — no secret, no full traveler PII, no password/session/CSRF token appeared.
- Metrics were not independently re-inspected via `/metrics` this session (it is off by default, `DETOURA_METRICS_ENABLED` unset) — the bounded-cardinality label discipline (`provider`, `operation`, `outcome`, `communication_type`, `phase` — never an identifier) was verified by code inspection in the immediately preceding Production Transactional Email slice and is unchanged here; no new metric call site was added or touched in this slice.

**Observability: PASS. Metrics: PASS (unchanged, re-confirmed by code inspection, not re-instrumented).**

---

## Webhook reality

`api/payments.py::provider_webhook` exists and is wired: signature-verified (`provider.verify_event`), deduplicated by `(provider, provider_event_id)` via an atomic claim, and — critically — never trusts the webhook payload's own status claim; it only ever triggers `ps.reconcile_payment`, which re-reads truth from the provider's own `retrieve()`. This session's happy-path capture completed **synchronously**, inside the request/worker-thread flow, with no webhook involved at all — webhook delivery is exclusively an out-of-band reconciliation path for `UNKNOWN`/`RECONCILIATION_REQUIRED` states, never a dependency of the primary flow. No public callback endpoint was exposed in this environment (correctly — exposing one merely to obtain a "PASS" would itself be the unsafe shortcut §30 warns against).

**Webhook: NOT REQUIRED FOR PRIMARY FLOW.**

---

## Recovery-path reality

None of `PARTIAL_FAILURE`, `PAYMENT_UNKNOWN`-during-execution, or a post-issuance capture failure were triggered by this session's live run (a single-leg synthetic happy path has no natural way to trigger them, and the brief explicitly forbids manufacturing them without documented safe provider tooling — none exists for a real Duffel/Stripe account we don't have). Each is:

- **Partial Issuance Recovery: INTEGRATION TEST VERIFIED** — `tests/test_v9_payment_booking_coupling.py` and `tests/test_v9_phase5_integration.py` (both re-run fresh, green) exercise `PARTIAL_FAILURE` → `mark_reconciliation_required` directly against the same `_execute_after_authorization` code path this session's live run also went through (for its `COMPLETE` branch).
- **Payment UNKNOWN Recovery: INTEGRATION TEST VERIFIED** — same suites, `PaymentStatus.UNKNOWN` branch.
- **Capture Failure Recovery: INTEGRATION TEST VERIFIED** — same suites cover a capture that does not cleanly succeed (`ops_recovery = captured.status not in (CAPTURED,)`).

None are labeled "REAL PROVIDER VERIFIED" — that would require a real Stripe/Duffel Test Mode account and a way to deterministically force each condition, which does not exist here.

---

## Frontend / browser findings for Codex

**No browser/frontend automation was performed** in this slice (backend/provider E2E only, per §32) and **no file under `frontend/` was read for the purpose of finding defects, modified, or touched** — confirmed by `git status --short` showing no `frontend/` path dirty at any point in this session. Nothing to hand to Codex from this slice.

---

## Findings

### Medium — CSRF protection is inconsistent across mutating booking/payment endpoints (not fixed, documented for a future slice)

**What was found**: `api/v1.py::create_booking_intent`, `::submit_travelers`, and `::confirm_booking` never call `require_csrf`, even when the request carries a valid authenticated session with a CSRF cookie. `api/payments.py::create_payment` likewise never checks CSRF. Only `api/payments.py::confirm_payment` (conditionally, when a session exists) and `::refund_payment` (unconditionally, since it requires a session) enforce it.

**How this was found**: this session's own E2E script initially omitted the `X-CSRF-Token` header on `POST /payments/{id}/confirm` and received a genuine `403` — prompting inspection of every mutating route's CSRF posture for consistency, not a targeted hunt.

**Why this is Medium, not High/Critical**: the money-moving step that actually authorizes a charge (`confirm_payment`) **is** protected. `confirm_booking` triggers capture only against a payment the caller has *already themselves authorized* for that exact `booking_id` (the eligibility gate re-verifies owner/currency/amount from persisted state regardless of how the request arrived) — so a CSRF-forced `confirm_booking` call could only ever confirm a booking the legitimate account holder already put an authorized payment behind, not redirect money or create a payment on an attacker's behalf. The impact is "the legitimate action could be triggered slightly earlier/without an explicit click," gated further by `booking_id`'s high entropy (`secrets.token_urlsafe(15)`), not "money moves to the wrong place." `create_booking_intent`/`submit_travelers`/`create_payment` create only inert, ownership-checked objects with no money movement at all.

**Disposition**: **not fixed in this slice.** This is a CSRF-hardening gap, not a truth-invariant violation, and does not make this session's E2E evidence unsafe or untruthful (the E2E itself simply had to include the header, which the real frontend already does for every mutating call — confirmed in the prior account/auth audit slice's read of `frontend/src/api/client.ts`). Per §33's own policy ("Medium backend defects may be fixed if necessary for truthful E2E" — this one is not necessary), it is recorded here for a dedicated security-hardening slice rather than fixed opportunistically mid-E2E-verification.

No Critical or High defect was found.

---

## Tests run this session (targeted only, per §36 — no full regression)

- `tests/test_v9_payment_booking_coupling.py`
- `tests/test_v8_booking.py`
- `tests/test_v85_commercial_security.py`
- `tests/test_v9_phase5_integration.py`
- `tests/test_v9_phase6_pii_security.py`

**90 tests, all green.** No executable backend code was changed in this slice, so a full regression was not warranted (the prior slice's full regression, ~2,500 tests, exit code 0, is still the most recent one and remains valid — not repeated here per §36's explicit "do not repeat the full suite unnecessarily after one clean successful run").

---

## Independent read-only review

A second pass attacked every item in §37 against this session's evidence and the diff (there is no diff — no file under `src/`, `tests/`, or `frontend/` was modified):

- **Fake provider evidence** — none; every claim above is either labeled `APPLICATION E2E VERIFIED` (with the sandbox explicitly named) or `BLOCKED`/`NOT VERIFIED` where a real provider would be needed. Nothing is labeled `REAL PROVIDER E2E VERIFIED`.
- **Live-mode risk** — impossible in this environment; no credential of any kind exists, and every live-provider kill switch (`PAYMENT_LIVE_CHARGING_ENABLED`, `SEARCH_LIVE_ENABLED`, `COMMUNICATION_LIVE_SENDING_ENABLED`) was left at its default `false`/unset.
- **Manual provider calls bypassing Detoura** — none made; every action in the E2E run went through the real `/api/v1/...` HTTP surface via `TestClient`, never a direct Stripe/Duffel SDK call.
- **Synthetic offer represented as LIVE** — the booking created was explicitly `demo_legs`/`ALL_IN_ONE`, and the resulting pass mode was `demo_only` (visible in the earlier `test_v8_booking.py` assertions this session's own run mirrors); nothing in this report calls it a LIVE selection.
- **selection_id substitution / client price trust** — not applicable to the demo path exercised (no `selection_id` involved); the forged-`selection_id` negative check confirms the substitution path is closed for the live path.
- **Payment not bound to booking / booking before authorization** — directly disproven by the raw DB read (`payment_transactions.booking_id`/`user_id` correctly bound) and by the ordering evidence (`AUTHORIZED` before `confirm_booking` was ever called).
- **UNKNOWN payment allowed through** — not applicable to this run's happy path; covered by INTEGRATION TEST VERIFIED evidence above.
- **Duplicate confirmation / duplicate order** — directly disproven live (second confirm call rejected `409`).
- **Capture before issuance / double capture / over-capture** — disproven by the raw DB read: `authorized_amount_minor == captured_amount_minor`, one capture, after `phase=complete`.
- **Partial issuance shown CONFIRMED** — not triggered this session; code path (`_execute_after_authorization`) only captures on `COMPLETE`, confirmed by reading, not merely assumed.
- **My Trips false success** — the booking shown in My Trips is the same `booking_id` whose DB row shows `CAPTURED`/`complete`; no discrepancy.
- **Supplier invoice represented as customer document** — the financial document is Detoura's own `RECEIPT`, generated by `financial_document_service.py`, explicitly watermarked "TEST DOCUMENT"; no Duffel invoice concept appears anywhere in this flow.
- **Email failure affecting booking** — not triggered (email succeeded, sandbox `SENT`); the *architecture* proof (broad `try/except`) was independently re-confirmed by re-running the two dedicated invariant tests, both green.
- **Secret leakage** — the full-DB scan and the captured log lines were both checked; zero matches.
- **PII leakage** — `communication_events.data_json` is `"{}"` throughout; no traveler name/email/DOB appears in any log line captured this session.
- **Cross-user access** — directly disproven live (`404`×3 for account B against account A's booking/documents/payment).
- **Frontend finding accidentally modified by Claude** — no `frontend/` path was touched; `git status --short` confirms.
- **Unproven claims labeled VERIFIED** — every capability below distinguishes `REAL PROVIDER E2E VERIFIED` (none claimed) from `APPLICATION E2E VERIFIED` / `INTEGRATION TEST VERIFIED` / `CODE VERIFIED` / `BLOCKED`, per the brief's own taxonomy; nothing is collapsed.

The one Medium CSRF finding above was itself surfaced and is disclosed, not suppressed, by this review.

**Verdict: APPROVED**

---

## Remaining blockers

1. **`STRIPE_SECRET_KEY` (test-mode, `sk_test_...`) is required** to exercise any real Stripe Test-Mode payment creation/authorization/capture/refund. Obtained from the Stripe Dashboard (Test mode toggle on) → Developers → API keys. No restart-time config beyond setting the env var and restarting the process is required (`payment_config.py` reads it fresh via `os.getenv` inside `resolve_provider()`, not cached at import time). `PAYMENT_PROVIDER=stripe` and `PAYMENT_LIVE_CHARGING_ENABLED=true` must both also be set — the master kill switch plus the explicit provider name.
2. **`STRIPE_WEBHOOK_SECRET` is required only if webhook-driven reconciliation is to be exercised** — not required for the primary happy-path flow (capture is synchronous), only for the out-of-band `UNKNOWN`/`RECONCILIATION_REQUIRED` recovery paths' webhook leg specifically.
3. **`DUFFEL_ACCESS_TOKEN` (test-mode, `duffel_test_...`) is required** to exercise any real Duffel Test-Mode search, revalidation, or order issuance. Obtained from the Duffel Dashboard (Test environment) → API tokens. `SEARCH_LIVE_ENABLED=true` is additionally required to route `/api/v1/search` to the live path at all.
4. **A safe, designated test recipient address would be required** only if real Resend delivery verification were in scope for this slice — it explicitly is not (§26/§28); no such recipient was requested or needed.
5. **No restart-blocking or additional feature flags beyond the above were found** during this session's reading of `payment_config.py`, `communication_config.py`, and `api/v1.py`'s live-search gating — every gate is a plain environment variable read fresh per call, not cached at process start (confirmed by reading `payment_config()`/`communication_config()`'s own module-level caching, which IS cached per-process but is explicitly reset by `reset_payment_config()`/`reset_communication_config()` in every test fixture that needs a fresh read — a real deployment would need a process restart after changing these, same as any other `_CONFIG: X | None = None`-cached module in this codebase).

No code change is required to unblock real provider E2E — only credentials.
