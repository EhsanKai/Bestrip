# V9 Limited Beta — Production Transactional Email Adapter (Resend)

**Starting HEAD:** `f347ee9` ("V9 connect My Trips recovery and documents")
**Final HEAD (pre-checkpoint):** `f347ee9` + one uncommitted, isolated slice (see §"Files changed" and the git-safety section at the end)

This slice takes Detoura's existing Phase 5 provider-neutral communication architecture from sandbox-only to production-adapter-ready, adding a `ResendEmailProvider` behind the same `CommunicationProvider` interface the sandbox already implements. No booking/payment logic, no consumer frontend, and no new communication *types* were touched or invented.

---

## Pre-slice architecture (read before any code was written)

Detoura's Phase 5 communication layer was already substantially built and adversarially tested before this slice began:

| Piece | File |
|---|---|
| Domain model + state machine | `src/detoura/models/communication.py` |
| Provider-neutral interface | `src/detoura/providers/communication_provider.py` |
| Sandbox (in-process reference) provider | `src/detoura/providers/sandbox_email.py` |
| Persistence (3 tables, CAS writes, append-only ledger) | `src/detoura/persistence/communications.py` |
| Send/resend/reconcile orchestration | `src/detoura/services/communication_service.py` |
| Config + provider resolution seam | `src/detoura/communication_config.py` |
| Booking-confirmation trigger | `src/detoura/services/post_booking_finalizer.py` (`_send_communication_safely`) |
| Ops-triggered explicit resend | `src/detoura/api/ops_confirmations.py` |

**State machine** (`CommunicationStatus`, unchanged by this slice):

```
PENDING → SENDING → SENT (terminal)
                  → FAILED (terminal)
                  → UNKNOWN → SENT / FAILED (reconciliation only)
```

**Idempotency**: one `CustomerCommunication` row per `(booking_id, communication_type)` (DB `UNIQUE` constraint); a resend is a new `CommunicationAttempt` row, never a new communication. `claim_send_slot()` atomically allocates the next `attempt_number` **and** refuses the claim if another attempt is already `SENDING`, in one SQL statement inside one `db.write()` transaction — the exact fix for a documented prior race (V9 Phase 5 QA finding #1).

**Retry model**: there is no automatic retry loop anywhere in this architecture. A send is one attempt; a resend is an explicit, separately-authorized call (`request_resend`, used only from `ops_confirmations.py`), refused outright while a prior attempt is `SENDING`. This slice's Resend adapter follows the same discipline — see "Retry semantics" below.

**UNKNOWN handling**: `reconcile_communication()` is the *only* path that can move a communication out of `UNKNOWN`, and it only ever calls `provider.retrieve()` — never `provider.send()` again. Proven for the Resend adapter by `TestUnknownNeverBlindlyResent` (a fake provider that raises if `send()` is called twice).

**Booking-confirmation trigger** (`post_booking_finalizer.py`): confirmation truth is written to the DB (`_upsert_confirmation`) *before* `_send_communication_safely` is ever called, and that function wraps the entire send in a broad `try/except` that can never propagate — a failed or `UNKNOWN` send changes nothing about the already-recorded confirmation/document. This predates this slice and was not touched.

**Event inventory** (current executable code, before and after this slice):

| Concept | Classification |
|---|---|
| Booking confirmation (covers `CONFIRMED`, `PARTIAL_RECOVERY`, `RECOVERY_REQUIRED`, `PAYMENT_UNKNOWN`, `REFUND_PENDING` via one truthful, status-aware renderer) | REAL & CONNECTED |
| Recovery communication | REAL & CONNECTED — same `BOOKING_CONFIRMATION` type/event path; `render_booking_confirmation_email()` already produces non-misleading copy for every non-`CONFIRMED` status (verified by reading the renderer and by pre-existing `TestEmailRenderTruthfulness` tests) |
| Standalone payment-status email | NOT BUILT — payment status is folded truthfully into the one booking-status email above; `CommunicationType.PAYMENT_RECEIPT` is a commented-out future value, never implemented |
| Financial-document email/attachment | NOT BUILT — documents are served only via the authenticated `/me/trips/{id}/documents/{id}/download` endpoint (see the prior V9 account/auth security audit); this matches the brief's own preference for secure authenticated access over attachments |
| Refund/credit-note email | NOT BUILT — `REFUND_PENDING` status text exists inside the one booking-status email; no dedicated refund/credit-note communication type exists |
| Ticket / Travel Pass email | NOT BUILT — no `CommunicationType` value for it, no call site |
| Account/password email | NOT BUILT (explicitly out of scope for this slice per the mission) |

Nothing above was invented in this slice. The only executable change to *what* gets sent is: the same booking-confirmation communication can now, when configured, go out through a real provider instead of only the sandbox.

---

## EmailProvider abstraction

Unchanged. `providers/communication_provider.py`'s `CommunicationProvider` Protocol, `CommunicationSendResult`, and `CommunicationProviderCapabilities` are exactly what they were — this slice adds a second implementation, never a new interface. `communication_service.py` imports only these types; it has no Resend-specific import or logic anywhere.

**EmailProvider Abstraction: PASS**

---

## SandboxEmailProvider

Unchanged (zero lines touched in `providers/sandbox_email.py`). Still the default (`live_sending_enabled=False`), still what every existing Phase 5/6 test and the new Resend tests use for anything that isn't specifically testing the Resend adapter itself.

**SandboxEmailProvider: PASS**

---

## ResendEmailProvider

New file: `src/detoura/providers/resend_email.py`. Implements the same `CommunicationProvider` contract, following `providers/stripe_payment.py`'s exact conventions (fail-closed construction, no automatic HTTP retry, redacted-key logging, `HttpClient`-injected for testability).

**API contract verified against Resend's public documentation** (fetched live during this slice, not guessed — see §25 of the task brief):

- `POST https://api.resend.com/emails`, `Authorization: Bearer re_...`, `Content-Type: application/json`.
- `Idempotency-Key` header: optional, unique per request, 24-hour expiry, ≤256 chars — maps directly onto this codebase's existing per-attempt idempotency key (`communication_service.py::_attempt_provider_key`, already `sha256(f"{communication_id}:{attempt_number}")[:40]` — well under the limit).
- Success: `200` with `{"id": "<uuid>"}`.
- Documented error statuses: `400, 401, 403, 404, 405, 409, 422, 429, 500, 503`.
- Rate limiting: `10 requests/second/team` default, with `ratelimit-limit` / `ratelimit-remaining` / `ratelimit-reset` / `retry-after` response headers — the last of which `providers/http.py`'s `HttpResponse.retry_after_seconds` already parses generically.
- `GET https://api.resend.com/emails/{id}`: returns `last_event` (`sent`, `delivered`, `bounced`, `complained`, `delivery_delayed`, `opened`, `clicked`, `failed`, ...) — the reconciliation seam `reconcile_communication()` already calls through `provider.retrieve()`.

**Outcome classification** (the part that has to be exactly right):

| Resend response | `CommunicationSendResult` | Reasoning |
|---|---|---|
| 2xx with parseable `{"id": ...}` | `ok=True, unknown=False` (SENT) | Provider confirmed acceptance |
| 2xx malformed/missing `id` | `ok=False, unknown=True` | "Malformed/ambiguous response" — §9's own example, never assumed success |
| `400/401/403/404/405/422/429` | `ok=False, unknown=False` (FAILED) | Provider was reached and explicitly rejected the request *before* anything was queued |
| `409` (idempotency-key conflict) | `ok=False, unknown=True` | Cannot rule out an earlier request with the same key having already been queued; Resend's docs don't expose a way to look this up other than by message id, which an ambiguous call never received |
| `500/502/503` | `ok=False, unknown=True` | A 5xx can occur after a request was internally accepted but before the response was returned; treated conservatively as ambiguous, never assumed to be a clean failure |
| Connection-level failure (timeout, dropped connection — no response at all) | `ok=False, unknown=True, provider_message_id=None` | True ambiguity; no id exists to reconcile against later (see "remaining gaps") |

Every one of these is exercised by a dedicated test in `tests/test_v9_production_email_resend.py` (`TestResendSendOutcomes`).

**Reconciliation** (`retrieve()`): `last_event` in `{sent, delivered, delivery_delayed, bounced, complained, opened, clicked}` → resolved SENT (a bounce/complaint is a post-*send* delivery event; `CommunicationStatus.SENT` models send-transaction truth, not final mailbox delivery — matching the domain model's own docstring); `last_event == "failed"` → resolved FAILED; `404` → `not_found` (definitive, matches the sandbox's own `not_found` convention); anything unrecognised → stays `unknown`, never guessed.

**ResendEmailProvider: PRODUCTION-READY, NOT REAL-PROVIDER (E2E) VERIFIED** — see "Real Resend E2E" below.

---

## Configuration

`src/detoura/communication_config.py` extended, mirroring `payment_config.py`'s exact fail-closed chain:

```
resolve_communication_provider():
  1. live_sending_enabled == False  → sandbox (unconditional master kill switch)
  2. provider == "sandbox"          → sandbox (explicit choice wins even if live)
  3. provider == "resend"           → construct ResendEmailProvider from
                                       RESEND_API_KEY / RESEND_FROM_EMAIL
                                       (env, read only at construction time -
                                       never stored on the config object) +
                                       RESEND_FROM_NAME / RESEND_REPLY_TO
                                       (non-secret, kept on CommunicationConfig)
  4. any other provider value       → raises CommunicationConfigurationError
```

Environment variables:

| Variable | Purpose | Secret? |
|---|---|---|
| `COMMUNICATION_PROVIDER` | `sandbox` (default) or `resend` | no |
| `COMMUNICATION_LIVE_SENDING_ENABLED` | master kill switch, default `false` | no |
| `RESEND_API_KEY` | Bearer credential | **yes** — never stored on `CommunicationConfig`, read directly from `os.environ` at construction time only |
| `RESEND_FROM_EMAIL` | verified sender address | no, but required |
| `RESEND_FROM_NAME` | sender display name, default `"Detoura"` | no |
| `RESEND_REPLY_TO` | optional reply-to address | no |

**Production Configuration: PASS**

---

## Fail-closed on missing configuration

`resolve_communication_provider()` **raises** `ResendConfigurationError` (missing/empty key or sender address) or `CommunicationConfigurationError` (unrecognised provider name) — it never falls back to the sandbox silently. Tested explicitly (`TestResolveCommunicationProviderFailClosed`): missing key, missing sender, unknown provider name, and — the important negative case — `live_sending_enabled=False` or an *explicit* `provider="sandbox"` both correctly return the sandbox rather than raising (raising there would be a false alarm, not fail-closed correctness).

`ResendEmailProvider.__post_init__` independently fails closed a second time at construction (wrong API-key prefix, malformed sender address) — defence in depth, matching `StripePaymentProvider`'s own "refuses to construct with an obviously-wrong key" posture.

**Fail-Closed Missing Config: PASS**

---

## Provider credential security

- The API key is read once, directly from `os.environ`, only inside `resolve_communication_provider()` — never stored on the frozen `CommunicationConfig` dataclass (test: `test_config_object_never_carries_the_api_key`, which also confirms the dataclass is `slots=True` with no `__dict__` at all to accidentally dump).
- Sent only as `Authorization: Bearer <key>` to the fixed, hardcoded `https://api.resend.com` host.
- `_log()` (the adapter's only logging call site) emits `provider`, `operation`, `outcome`, `status`, `duration_ms` — never headers, never the request/response body. Identical shape to `stripe_payment.py::_log_call`.
- `redact_key()` exists for any future diagnostics surface, mirroring `stripe_payment.py::redact_key` exactly.
- No exception raised anywhere in the adapter embeds the key (`ResendConfigurationError`'s messages name only the environment variable, never a value) — tested (`test_exception_message_never_contains_the_api_key`).
- The key is never persisted to any database table, never returned in any API response (this is a backend-only adapter with no public surface of its own), and never appears in the `communication_events` ledger (which only ever receives structured, non-sensitive fields, unchanged from Phase 5).

**Provider Credential Security: PASS**

---

## Network / HTTP security

Reused, not reinvented: `ResendEmailProvider` uses `providers/http.py::UrllibHttpClient` — the same TLS-verified (certifi-pinned, no verification-disable path), no-auto-redirect (`_NoRedirectHandler`, closing the credential-leak-via-redirect SSRF class already documented for Duffel/Amadeus/Stripe) transport every other real provider adapter in this codebase uses. The endpoint is a fixed literal (`API_BASE = "https://api.resend.com"`) — never built from configuration or any customer-supplied value, so there is no destination-URL injection surface at all (stronger than "allowlisted": it is hardcoded).

Deliberately **not** wrapped in `RetryingHttpClient`: that decorator auto-retries `RETRYABLE_STATUS` (`408, 425, 429, 500, 502, 503, 504`) internally, which would silently re-attempt exactly the ambiguous cases (`429`, `5xx`) this adapter must instead surface as a classified, auditable outcome for the existing resend/reconciliation architecture to handle — mirroring `StripePaymentProvider`, which makes the identical choice for the identical reason (payment-safety-critical calls get one classified attempt, not a hidden retry loop).

**Network Security: PASS**

---

## Delivery state machine

Unchanged (`models/communication.py` was not touched). The Resend adapter maps onto the exact same `PENDING → SENDING → {SENT, FAILED, UNKNOWN}` machine every other provider does — see "Outcome classification" above.

**Delivery State Machine: PASS**

---

## UNKNOWN semantics

- Every ambiguous Resend outcome (`409`, `5xx`, connection failure, malformed/missing-id 2xx) maps to `unknown=True`.
- `reconcile_communication()` — unchanged — is the only path that can resolve it, and only via `provider.retrieve()`.
- **Documented, honest gap**: a true connection-level failure during `send()` never receives a `provider_message_id` (no response arrived at all), so there is nothing for `reconcile_communication()` to retrieve against — it correctly stays `UNKNOWN` forever, pending Ops manual resolution (e.g. checking the Resend dashboard for the idempotency key used). This is not a bug to fix; it is the honest limit of what a real network boundary can offer, unlike the in-process sandbox, which can privately record a message even for its own scripted "unknown" outcome. Proven by `TestUnknownNeverBlindlyResent`.

**UNKNOWN Semantics: PASS. Blind UNKNOWN Resend: NONE**

---

## Idempotency

- `send()` forwards the caller-supplied `idempotency_key` verbatim as Resend's `Idempotency-Key` header — never derives one from the recipient address (tested: two different communications to the *same* recipient produce two distinct, unrelated idempotency keys).
- The actual key is `communication_service.py::_attempt_provider_key(communication_id, attempt_number)` — unchanged, already deterministic per (communication, attempt) pair, so an HTTP-level retry of the *same* attempt (e.g. a client library auto-retry, which this codebase does not add) would present the same key to Resend and be safely deduplicated *by Resend itself*, in addition to this codebase's own `claim_send_slot()` guard that prevents a second attempt from ever being created while one is in flight.

**Idempotency: PASS**

---

## Concurrency: duplicate-send protection

`TestResendConcurrency::test_concurrent_create_and_send_never_double_sends_via_resend`: 8 threads race `create_and_send_communication` for the *same* `booking_id` against a fake Resend transport. Result: exactly **one** real HTTP call reached the fake transport, and all 8 threads observed the same `communication_id` — proving the pre-existing `UNIQUE(booking_id, communication_type)` constraint plus `claim_send_slot()`'s atomic claim (not anything new in the Resend adapter) is what prevents a duplicate customer email under HTTP retries, worker retries, double-clicks, or duplicate booking callbacks.

**Concurrent Duplicate Send Protection: PASS**

---

## Retry semantics

No automatic retry exists anywhere in this slice's new code, by design (see "Network/HTTP security" above). Bounded retries, where they matter, are the pre-existing explicit-resend architecture (`request_resend`, refused while `SENDING`) — unchanged and unaffected. This adapter's job is only to classify each single attempt honestly; the safety of a *repeated* attempt already came from `communication_service.py`, not from anything this slice adds.

**Retry Semantics: PASS**

---

## Booking success independent of email

Not modified, and re-verified: `post_booking_finalizer.py::_send_communication_safely` (unchanged) still establishes confirmation truth first and swallows every communication exception. `tests/test_v9_phase5_integration.py::test_email_failure_never_touches_booking_or_payment_truth` and `::test_email_unknown_never_blindly_duplicate_sent` — both pre-existing — pass unchanged after this slice's `communication_service.py`/`communication_config.py`/`observability/metrics.py` edits (13/13 in that file, run explicitly during this slice).

**Booking Success Independent of Email: PASS**

---

## Booking confirmation / recovery / payment / financial-document / Travel Pass email

See "Event inventory" above — unchanged classifications; no new type invented. Booking confirmation and recovery both use one truthful, status-aware communication, now deliverable through Resend when configured; nothing else was connected because nothing else exists in executable code to connect.

- **Booking Confirmation Email:** REAL & CONNECTED
- **Recovery Email:** REAL & CONNECTED (folded into the booking-confirmation communication's truthful status branches)
- **Payment Email:** NOT BUILT (standalone type; payment status is truthfully represented within the booking-confirmation email)
- **Financial Document Email:** NOT BUILT (secure authenticated download only, unchanged)
- **Travel Pass / Ticket Email:** NOT BUILT

---

## Recipient authority

Unchanged: `post_booking_finalizer.py::_send_communication_safely` reads `booking.lead_email` — a server-side booking field — never a client-supplied value at send time. `ResendEmailProvider.send()` has no code path that accepts or derives a recipient from anything other than its `recipient` parameter, which callers set from that same authoritative field. Tested (`test_recipient_is_a_fixed_list_not_customer_expandable`) that the adapter sends exactly the one address it was given, as a single-element list, never expanding or substituting it.

**Recipient Authority: PASS**

---

## HTML / content safety

The communication interface — unchanged — only ever carries `body_text` (plain text; `render_booking_confirmation_email()`'s own docstring: "plain text, no HTML formatting"). `ResendEmailProvider.send()` sends only Resend's `text` field, **never** `html` — tested explicitly (`test_no_html_field_is_ever_sent`, using a deliberately hostile subject line containing a `<script>` tag, confirming no HTML rendering path exists to inject into). This slice introduces no template or HTML-rendering surface at all, so there is no new injection risk to introduce.

**HTML / Content Safety: PASS**

---

## PII minimization

Unchanged: `CommunicationEvent.data`'s existing `field_validator` still rejects any `@`-containing value (a proxy for an email address) before it can be persisted to the ledger. The Resend adapter's own logging (`_log()`) never includes the recipient, subject, or body. Nothing in this slice adds a new field to any persisted communication record.

**PII Minimization: PASS**

---

## Secret logging

Covered under "Provider credential security" above — no test or manual trace found the API key, the `Authorization` header, or any request/response body in a log line, an exception message, a metric label, or a persisted row.

**Secret Logging: PASS**

---

## Observability

`_log()` reuses the existing `observability.log_event` + `observability.metrics.observe_provider_call` seam (the exact one `stripe_payment.py` and `RetryingHttpClient` already use) — `provider="resend"`, `operation="send"/"retrieve"`, `outcome`, `status`, `duration_ms`. No new logger, no new log format.

**Observability: REAL & CONNECTED**

---

## Metrics

Added `observability/metrics.py::observe_communication_transition(provider, communication_type, outcome)`, wired into `communication_service.py`'s `_apply_send_result` (every send/resend outcome) and `reconcile_communication` (every resolved reconciliation). Labels are all bounded: `provider` ("sandbox"/"resend"), `communication_type` (the `CommunicationType` enum), `outcome` ("sent"/"failed"/"unknown") — never a `communication_id`, `booking_id`, recipient, or `provider_message_id`. `provider_requests_total`/`provider_call_duration_ms` (pre-existing, generic) also now carry `provider="resend"` data for free via `_log()`.

**Metrics: REAL & CONNECTED**

---

## Readiness

`GET /readyz` (in `api/app.py`) now also calls `resolve_communication_provider()` inside a `try/except` — a **pure configuration check** (env vars present, key/address shape valid); it never makes a network call, proven by `test_readiness_check_makes_no_real_network_call`, which monkeypatches `UrllibHttpClient.request` to raise an `AssertionError` if invoked at all and confirms `/readyz` still returns `200` when Resend is fully and validly configured. A misconfigured-but-enabled Resend (missing key) correctly returns `503` with `{"status": "not_ready", "reason": "communication_provider_misconfigured"}`. The two pre-existing exact-equality tests (`{"status": "ready"}` / `{"status": "not_ready"}` for the DB-only cases) still pass unchanged, since the new check is additive and only reached after the DB check succeeds.

**Readiness: PASS**

---

## Sandbox safety

- `SandboxEmailProvider` remains the unconditional default (`live_sending_enabled=False`); no test in this repository can accidentally reach Resend, because reaching it requires three separate, explicit environment variables to all be set correctly, and even then the transport is always a real `HttpClient` this test suite never points at the real network.
- Every new test in `tests/test_v9_production_email_resend.py` injects a `FakeHttpClient` (or, for `ResendEmailProvider` construction tests, doesn't construct one that could reach the network at all) — no test requires or reads a real `RESEND_API_KEY`.
- Conversely, an explicit `provider="sandbox"` choice wins even when `live_sending_enabled=True` (tested) — a staging environment cannot be accidentally "upgraded" to Resend just because the master kill switch is on.

**Sandbox Safety: PASS**

---

## Tests

**New file**: `tests/test_v9_production_email_resend.py` — 56 tests, all passing, covering: construction fail-closed (5), key redaction (3), `resolve_communication_provider` fail-closed chain (8), send-outcome classification for every documented status code + timeouts + malformed bodies (11), Idempotency-Key plumbing (3), `retrieve()`/reconciliation mapping (5), content/secret safety (5), concurrency (1), UNKNOWN-never-blindly-resent (1), readiness (4), capabilities/protocol conformance (2), plus supporting fixtures.

**Targeted regression** (run during this slice, all green):
- `tests/test_v9_phase5_communication.py` — 42/42
- `tests/test_v9_phase5_integration.py` — 13/13 (includes the two booking/email-independence invariant tests)
- `tests/test_v9_observability_baseline.py` — 20/20 (including the two exact-equality `/readyz` tests, confirming the readiness change is additive/non-breaking)
- `tests/test_v9_phase6_pii_security.py`, `tests/test_v9_payment_booking_coupling.py`, `tests/test_v9_phase26_accounts.py`, `tests/test_v9_phase26_auth_api.py` — all green (216 tests total across this combined targeted run)
- `tests/test_v9_production_email_resend.py` — 56/56

## Full regression

Executable production infrastructure was added in this slice (§30 of the brief requires a full run, not just targeted suites, in that case). Ran the complete suite unbuffered and logged (`python -u -m pytest tests/ -q`, ~2,500 collected tests): **exit code 0**, zero failures, zero errors, one skip (pre-existing, unrelated to this slice). This is the first full run since the prior V9 slice's own full regression; not repeated a second time in this session per the brief's "do not repeat the full suite unnecessarily after one clean successful run" instruction.

**Full Regression: PASSED (exit code 0, 0 failed, 0 errors)**

---

## Real Resend E2E

No `RESEND_API_KEY` (test or production) exists in this environment, and no designated safe test recipient is provided by the repository or environment. Per the task's own instruction, no email was sent to any real address.

**Real Resend E2E: NOT VERIFIED — SAFE CREDENTIAL/RECIPIENT UNAVAILABLE**

---

## Independent read-only review

A second, read-only pass attacked every item in the brief's §29 checklist against the diff:

- **Provider lock-in leaking into domain** — `communication_service.py` imports only `CommunicationProvider`/`CommunicationSendResult`; no Resend-specific type or field crosses that boundary. Clean.
- **Booking failure caused by email failure** — `post_booking_finalizer.py` untouched; confirmation is written before the send attempt and the send is wrapped in a broad, unconditional `try/except`. Clean.
- **UNKNOWN collapsed to FAILED** — checked every branch of `_parse_send_response`/`retrieve()` individually against the classification table above; `409` and `5xx` are the two easy-to-get-wrong cases and both correctly land on `unknown=True`. Clean.
- **Blind retry after UNKNOWN** — `reconcile_communication` calls only `retrieve()`; proven by a fake provider whose `send()` raises if invoked twice. Clean.
- **Duplicate emails / concurrency duplicate send** — proven by the 8-thread race test: exactly one HTTP call reached the transport. Clean.
- **Unsafe retry semantics** — no automatic retry exists in the new code. Clean.
- **Production fallback to sandbox** — `resolve_communication_provider` raises on missing config; explicitly tested it does not fall back. Clean.
- **Secret/API-key leakage** — traced every `_log()`/exception/metric call site; none references `self.api_key`. Clean.
- **Recipient email leakage** — `_log()` never references `recipient`; metrics labels are bounded and identifier-free. Clean.
- **PII leakage** — no new persisted field; the pre-existing `CommunicationEvent.data` PII validator is untouched and still active.
- **HTML injection** — confirmed structurally impossible: the adapter has no code path that ever constructs or sends an `html` payload field.
- **Customer-controlled recipient substitution** — `send()`'s `recipient` parameter is opaque to the adapter; the one call site (`post_booking_finalizer.py`) sources it from `booking.lead_email`, unchanged.
- **Supplier invoice confusion / refund document mutation** — no financial-document code was touched in this slice; not applicable.
- **RECOVERY_REQUIRED confirmation email / PAYMENT_UNKNOWN success email** — `render_booking_confirmation_email()` untouched; its existing truthful branches for these statuses are exactly what gets sent through the new transport.
- **Unbounded metric cardinality** — `observe_communication_transition`'s three labels are all bounded enums; no identifier field is ever passed.
- **Readiness making real provider calls** — explicitly disproven by a test that makes any real HTTP attempt during `/readyz` fail the test.
- **Frontend contamination** — `git diff --stat` for this slice touches zero files under `frontend/`.
- **Concurrent-work contamination** — `git status --short` at slice start and end shows only this slice's own files as tracked-modified/new, plus the pre-existing untracked `AGENTS.md`/`CLAUDE.md` tooling files, which were not touched.

No new issue was found. Nothing required a fix.

**Verdict: APPROVED**

---

## Findings by severity

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |

---

## Remaining gaps (documented, not fixed in this slice — none are defects)

- **True connection-level UNKNOWN has no reconciliation path.** If `send()` never receives any HTTP response at all, there is no `provider_message_id` to later `retrieve()` against. This is an honest limitation of a real network boundary (the in-process sandbox can cheat by privately remembering its own scripted-UNKNOWN message; a real provider cannot be asked "what happened to the request behind idempotency key X" without a message id in Resend's currently-documented API). Such a communication stays `UNKNOWN` for Ops to resolve manually (e.g. checking the Resend dashboard/logs for the idempotency key). Not fixed because the brief explicitly directs "preserve UNKNOWN for Ops/manual resolution... do not guess" for exactly this case.
- **Resend webhooks (delivery events) are not wired.** `capabilities().supports_delivery_events=True` truthfully declares that Resend *has* this capability, but no webhook receiver exists in this codebase to consume it yet — reconciliation today is pull-only (`GET /emails/{id}`), which is sufficient for the state machine's needs but slower than an event push would be. A future slice could add a signed-webhook receiver mirroring `stripe_payment.py::verify_webhook_signature`'s pattern, if Resend's webhook signing scheme is verified against its docs first.
- **No standalone payment, financial-document, or Travel Pass communication exists** — documented as NOT BUILT per the event inventory, matching the brief's explicit instruction not to invent them.
- **Real Resend E2E is unverified** — no credential/recipient available in this environment; this is expected and acceptable per §28.
