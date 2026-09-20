# V9 Limited Beta — Production Observability Baseline

**Starting HEAD**: `2b26d130711df18e87141a0aff81d9ba6445acfc`
**Final HEAD**: this commit

Backend/infra only. No `frontend/**` file was touched by this slice.

## 1. Why

`docs/V9_LIMITED_BETA_REALITY_AUDIT.md` (§20) found production observability
"essentially NOT BUILT": no structured logging, no request correlation, no
metrics, and a `/health` that returns a static `{"status": "ok"}` without
checking anything. Detoura already has substantial transactional logic for
search, payments, booking orchestration, and provider calls — this slice
makes that logic *operationally understandable* (request → domain event →
structured log → correlation → safe metric → health/readiness → actionable
failure signal) without redesigning any of it.

## 2. Existing observability audit (before this slice)

A fresh read-only audit (not re-trusting the Reality Audit's age) confirmed:

| Capability | Status |
|---|---|
| Structured/JSON logging | **ABSENT** — plain `logging.getLogger`, unconfigured, 5 call sites total across the whole backend |
| Request/correlation IDs | **ABSENT** |
| Request timing middleware | **ABSENT** (only `CORSMiddleware` + `MaxBodySizeMiddleware`) |
| Provider (Duffel/Stripe) call logging | **ABSENT** |
| Booking orchestration logging | **PARTIAL** — one `logger.warning` (Ops-recovery signal) |
| Payment logging | **ABSENT** — state transitions are persisted to `payment_events`/`payment_transactions` but never logged |
| Metrics | **ABSENT** — one unexported in-process `HttpMetrics` counter |
| Health endpoint | **PARTIAL** — `/health` (×2, engine + product routers) is a static liveness stub, never touches the DB or a provider (safe, but not readiness) |
| Readiness endpoint | **ABSENT** |
| Exception handling | **ABSENT handler**, but verified non-leaking by default Starlette behaviour (no `debug=True`) |
| PII/secret redaction | **PARTIAL** — no centralized filter; the 5 existing log sites were individually verified clean by `docs/V9_PHASE6_SECURITY_REPORT.md`'s PII/logging adversarial slice |
| Security/audit trail | **REAL & CONNECTED**, but separate from this slice — `persistence/audit.py`'s DB-backed `audit_events` table (Ops actions), untouched here |

Nothing here was duplicated: the audit's DB trail, the `payment_events`
table, and the 5 pre-existing `logger.*` call sites (each pinned by Phase
6's own `caplog` regression tests) are all left exactly as they were.

## 3. What this slice adds

A new `src/detoura/observability/` package — stdlib-only, no new dependency
(matches this project's existing "stdlib only" posture in
`providers/http.py`):

- **`logging.py`** — a JSON `logging.Formatter`, a context-local request id
  (`contextvars`), `sanitize_request_id()` (validates/replaces an untrusted
  client-supplied id), and `log_event(logger, event, **fields)` — the one
  call every instrumented site uses instead of a free-form message.
- **`metrics.py`** — a bespoke counter/histogram-sum registry (not
  `prometheus_client` — a handful of counters didn't justify the
  dependency) and a hand-written Prometheus text exporter.
- **`middleware.py`** — `CorrelationMiddleware`, pure ASGI (same reasoning
  as `api/body_limit.py`'s own avoidance of `BaseHTTPMiddleware`): assigns/
  validates the request id, times the request, logs one
  `request_completed`/`request_failed` event and records the HTTP metric,
  for every route, with zero per-endpoint boilerplate.

Every public function in this package **swallows its own exceptions**
(§18 below) — an observability bug can never become a transactional bug.

## 4. Request correlation

- `CorrelationMiddleware` is the outermost middleware (added last in
  `api/app.py::create_app()`, matching `MaxBodySizeMiddleware`'s own
  documented "Starlette runs the most-recently-added middleware first"
  ordering) — it wraps and times every request, including one CORS or the
  body-size guard itself rejects.
- An incoming `X-Request-Id` is accepted only if it matches
  `^[A-Za-z0-9_.-]{1,128}$` (`logging.sanitize_request_id`); anything else
  (oversized, containing CR/LF, control characters) is replaced with a
  fresh `uuid4().hex` rather than logged or echoed back — closes the
  header/log-injection and unbounded-value risk named in the contract (§5).
- The (validated or generated) id is bound to a `contextvars.ContextVar`
  for the life of the request, picked up automatically by every
  `log_event()` call in that request, and returned as the `X-Request-Id`
  response header.
- Background execution (the booking worker thread, `payment_service`
  calls made from it) does not inherit the HTTP request's context — Python
  threads do not propagate `contextvars` automatically, and redesigning
  `booking_flow`'s `threading.Thread` worker for propagation was explicitly
  out of scope (contract §5: "do not redesign worker architecture solely
  for propagation"). Those code paths still get full domain-event logging
  (§7/§8) with `booking_id`/`payment_id` correlation instead.

## 5. Structured logging

`configure_logging()` installs one JSON `StreamHandler` on the root logger
from `create_app()` (idempotent — safe if called more than once, e.g. by
tests that build the app repeatedly). Every event is one JSON line:

```json
{"timestamp": "...", "level": "INFO", "logger": "...", "message": "<event>",
 "event": "<event>", "request_id": "...", <event-specific fields>}
```

`DETOURA_LOG_LEVEL` (default `INFO`) controls verbosity.

## 6. Exception handling

`app.py` registers `@app.exception_handler(Exception)`: logs
`unhandled_exception` (method, route, exception **type** only — never the
message, which can embed arbitrary internal detail) with the request's
correlation id, and returns a generic
`{"detail": {"message": "An unexpected error occurred.", "request_id": ...}}`
500 — no stack trace, no exception message, ever reaches the client. It
never intercepts `HTTPException` — FastAPI's own path handles those
unchanged, with whatever message the route author chose.

(Starlette's `ServerErrorMiddleware` re-raises the original exception
*after* sending this response, for the ASGI server's own log — harmless in
production/uvicorn, but means a test must use
`TestClient(..., raise_server_exceptions=False)` to observe the response
instead of the exception; see `tests/test_v9_observability_baseline.py`.)

## 7. Search observability (`api/v1.py`)

`search()` is now a thin instrumentation boundary around the **unchanged**
`_search_impl()` (a pure rename + wrap, no logic moved): one
`search_started` / `search_completed` / `search_failed` event and one
`search_requests_total`/`search_duration_ms` metric per call, regardless of
which of `_search_impl`'s several internal return paths (live, synthetic,
degraded-fallback) actually answers. Fields: `mode`, `duration_ms`,
`result_count`, `supply_source` — never the raw request body/preferences.

## 8. Provider observability

- `providers/http.py`'s `RetryingHttpClient` — the shared transport behind
  Duffel, Amadeus, the market-prior-acquisition network adapter, and the
  Wikimedia image search — now takes an optional `provider=` label
  (defaults to `"unknown"`, so any caller that doesn't pass it keeps
  working unchanged) and wraps its **unchanged** retry/backoff logic
  (renamed to `_request_impl`) with one `provider_request_completed` log +
  `provider_requests_total`/`provider_call_duration_ms` metric per call,
  classifying the outcome as `ok` / `timeout` / `rate_limited` /
  `client_error` / `server_error` / `network_error`. Logs the request host
  only (`urlparse(url).hostname`) — never the full URL, query string,
  headers, or body. Duffel/Amadeus/Wikimedia/market-source call sites were
  updated to pass their real `provider=` label.
- `providers/stripe_payment.py` uses a plain `UrllibHttpClient` (no retry
  wrapper), so its `_post`/`_get` choke points got the same
  `provider_request_completed` logging directly — status/outcome/timing
  only, **never** the form-encoded request body (which carries amounts and
  the idempotency key) or the `Authorization: Bearer <secret_key>` header.

## 9. Payment observability (`services/payment_service.py`)

Every existing `store.record_event(db, PaymentEvent(event_type=...))` call
site (already the state machine's own persisted transition log) now has a
matching `log_event()` call using the **same** `event_type` value, mapped
through one explicit table (`_PAYMENT_LOG_EVENTS`) to the contract's
stable event names — `payment_created`, `payment_authorization_started`,
`payment_authorized`, `payment_requires_customer_action`, `payment_failed`,
`payment_reconciliation_required`, `payment_capture_started`,
`payment_captured`, `payment_capture_failed`,
`payment_authorization_cancelled`, `payment_refund_started`,
`payment_refunded`, `payment_refund_failed`, `payment_reconciled` — so the
application log and the persisted `payment_events` row can never drift onto
different taxonomies. No new transition was invented; nothing here changes
the state machine. Fields are `payment_id`/`booking_id` only — never an
amount, card detail, or provider secret.

`reconcile_payment`'s three `ReconciliationFinding` sites (previously
persisted but never logged) now also emit `payment_reconciliation_finding`
with `classification`/`local_status`/`provider_status` — the §11
UNKNOWN/recovery visibility the Beta explicitly calls out as important.

The webhook endpoint (`api/payments.py::provider_webhook`) logs
`payment_webhook_received` / `payment_webhook_rejected` /
`payment_webhook_duplicate_ignored` / `payment_webhook_processed` — never
the raw payload or the `Stripe-Signature`/`X-Signature` header.

## 10. Booking observability (`services/booking_orchestrator.py`, `services/booking_flow.py`)

New events, matched to the orchestrator's own domain terms:
`booking_intent_created`, `booking_confirmation_requested`,
`booking_claimed`, `booking_execution_started`,
`booking_revalidation_completed` (outcome: `ready`/`reconfirm_required`/
`failed`), `booking_leg_issued`, `booking_leg_failed`, and the terminal
`booking_complete`/`booking_partial_failure`/`booking_failed` (from
`_log_booking_outcome`, called from every `run_booking` exit path). Fields
are `booking_id`/`item_id`/`phase`/counts only — **never** `TravelerParty`
data or a full provider payload (verified directly by
`tests/test_v9_observability_baseline.py::test_booking_intent_created_log_carries_no_traveler_data`).

The one pre-existing pinned `logger.warning(...)` in `start_confirmation`'s
worker (Ops-recovery signal, Phase 6 `caplog`-tested — message left
byte-for-byte unchanged) now has a companion structured
`booking_recovery_required` event next to it, so Ops tooling can alert on
`event=booking_recovery_required` instead of parsing free text. A
`booking_execution_error` event was added to the worker's outer
`except Exception` (previously silent beyond the one warning path).

**Invariants preserved, unchanged**: `BOOKING SUCCESS != EMAIL SUCCESS`,
`RECOVERY_REQUIRED != CONFIRMED` — this slice adds visibility, never
resolution logic.

## 11. UNKNOWN / recovery signals

- Payment: `payment_reconciliation_required` and
  `payment_reconciliation_finding` (§9).
- Booking: `booking_recovery_required`, `booking_partial_failure` (§10).

Nothing auto-resolves anything — every one of these is a log line + a
counter, never a state transition of its own.

## 12–13. Metrics

`observability/metrics.py` — counters + histogram sums, in-process,
thread-safe (a `threading.Lock`), reset between test runs via
`reset_for_tests()`:

- `http_requests_total{method,status_class}`, `http_errors_total`,
  `http_request_duration_ms`
- `search_requests_total{mode,outcome}`, `search_failures_total`,
  `search_duration_ms`
- `provider_requests_total{provider,operation,outcome}`,
  `provider_failures_total`, `provider_call_duration_ms`
- `payment_transitions_total{event_type}`,
  `payment_reconciliation_required_total`
- `booking_executions_total{phase}`, `booking_partial_failure_total`,
  `booking_recovery_required_total`

**Cardinality policy**: every label is a bounded, small-cardinality concept
(method, status class, mode, provider name, operation, outcome, event
type, phase). No function in this module accepts or ever receives a
`booking_id`/`payment_id`/`user_id`/`request_id`/`selection_id`/offer id —
enforced by the module's own API shape (there is no parameter to smuggle
one through), and asserted directly by
`test_metrics_never_carry_identifier_shaped_labels`.

`GET /metrics` (Prometheus text format) is registered **only** when
`DETOURA_METRICS_ENABLED` is truthy — this process has no auth story of its
own for its endpoints, so the endpoint is opt-in and off by default.
**Deployment note**: if enabled, keep `/metrics` off any
publicly-reachable network path (reverse-proxy allowlist, internal
network, or a sidecar scraper) — it is unauthenticated.

## 14–15. Health / readiness

- `GET /health` (×2, pre-existing, unchanged) — liveness only, still a
  static `{"status": "ok", ...}`, still never touches the DB or a
  provider. Left alone deliberately: the contract explicitly warns against
  making liveness expensive or dependency-coupled.
- `GET /readyz` (new, in `app.py`) — checks **only** `get_db().query_one("SELECT 1")`.
  Returns `{"status": "ready"}` (200) or `{"status": "not_ready"}` (503).
  Deliberately does **not** call Duffel or Stripe: a temporary provider
  outage must not pull the whole process out of a load balancer's rotation
  when search/auth/read-only routes are still fine — exactly the
  "readiness must not cause provider traffic" concern the contract raises.

## 16. Privacy / redaction

- `log_event()` filters a fixed `FORBIDDEN_FIELDS` set (password,
  authorization, cookie, csrf, client/webhook secrets, card fields, email,
  traveler/passport/DOB fields, tokens, session, raw body/payload) out of
  every event's fields **before** they ever reach a `LogRecord` — not only
  at JSON-render time, so no handler (a test's `caplog`, a future second
  sink) can observe an unfiltered field either. This is defence in depth:
  the primary control is still that every instrumented call site was
  written to pass only safe identifiers (verified per call site in §7–10
  above), matching this contract's own warning against "a false sense of
  security with superficial regex-only redaction while leaving raw object
  logging elsewhere" — nothing here scans/redacts arbitrary strings.
- The 5 pre-existing log call sites (Phase 6-pinned) were **not modified**
  — their `caplog` regression tests (`test_v9_phase6_pii_security.py`) pass
  unchanged.
- No provider request/response body or header is ever logged anywhere in
  this slice (Duffel, Stripe, Amadeus, Wikimedia, market-source) — only
  host, operation, status, outcome classification, and timing.

## 17. Non-authoritative logging/metrics (§18)

Every `log_event()` and every `metrics.incr`/`observe` call wraps its body
in `except Exception: pass`. Proven directly:
`test_broken_logger_does_not_break_payment_creation` monkeypatches
`logging.Logger.log` to always raise, then runs a full
`create_payment()` — the payment is still created correctly.
`test_broken_metrics_do_not_raise` breaks the metrics registry's internal
dict the same way. Neither test needed the app-level fallback to be
reached more than once, matching the contract: an observability failure
never becomes a different commercial truth.

## 18. Tests

`tests/test_v9_observability_baseline.py` — 20 tests covering exactly the
contract's own checklist (request id generation/propagation/sanitization,
structured request event, exception correlation without stack-trace leak,
Authorization/Cookie/CSRF never logged, forbidden-field filtering, no
traveler data in booking logs, health liveness + no DB call, readiness
ready/not-ready, payment UNKNOWN→reconciliation-required event, booking
partial-failure event, provider timeout classification, metric-label
cardinality, and logging/metrics failure non-authoritativeness).

## 19. Test results

Targeted (this slice + everything it touches):
`test_v9_observability_baseline.py` (20/20), `test_v9_phase6_pii_security.py`,
`test_v9_payment_booking_coupling.py`, `test_v9_phase6_payment_security.py`,
`test_v9_phase6_booking_security.py`, `test_v9_phase6_network_security.py`,
`test_v75_booking.py`, `test_v8_booking.py`,
`test_v9_search_selection_booking_contract.py`, plus the full
provider/http-adjacent surface (`test_v4_real_providers.py`,
`test_v5_product.py`, `test_v75_adversarial.py`, `test_v75_provider.py`,
`test_v76_preflight.py`, `test_v85_ticket_operations.py`,
`test_v8_revalidation.py`, `test_v8_safety.py`, `test_v8_supply.py`,
`test_v9_phase25_network_safety.py`, `test_v9_phase26_destination_images.py`,
`test_v9_phase2_budget.py`, `test_v9_phase4_providers.py`,
`test_v9_provenance_fix.py`, `test_v9_search_intelligence_slice_1_5.py`,
`test_v9_search_live_wiring.py`, `test_v9_search_recorder.py`) — **all
green**.

Full regression: `pytest tests/` — **2450 collected, 2425 passed, 25
skipped, 0 failed, 0 errors.** (One transient failure was observed in an
earlier, sandbox-resource-contended run of this same full suite, in
`tests/test_adversarial.py::test_determinism_across_profiles_and_repeats` —
a core beam-search planner test with no dependency on anything this slice
touches; `src/detoura/algorithms/`, `models/`, and `profiles.py` are all
untouched by this change per `git status`. Re-ran that single test and its
whole file in isolation — passed both times. Non-reproducing, unrelated;
the clean full-suite run above supersedes it.)

## 20. Deployment / operator notes

- Set `DETOURA_LOG_LEVEL=INFO` (default) or `DEBUG` for more verbose JSON
  logs to stdout — no file/rotation handling is added here; container/
  platform log collection is assumed, matching the existing Dockerfile/
  `render.yaml` deployment shape.
- `GET /readyz` is the one to point a load balancer's readiness probe at;
  `GET /health` remains the liveness probe.
- `DETOURA_METRICS_ENABLED=1` turns on `GET /metrics` — keep it off a
  public network path; there is no auth on this endpoint.
- `X-Request-Id` on the response can be handed back by a client filing a
  support ticket, and grepped directly in the JSON logs.

## 21. Remaining observability gaps (explicitly out of scope here)

- No log shipping/aggregation/alerting backend is wired up — this slice is
  the application-side seam an operator points one at, not the platform
  itself (matches contract framing: "the goal is NOT to install a huge
  observability platform").
- Request-id propagation does not cross into the booking worker thread's
  background execution (§4) — booking/payment events there correlate by
  `booking_id`/`payment_id` instead, not by the original HTTP request id.
- `/metrics` has no authentication of its own — relies entirely on network
  placement when enabled.
- No distributed tracing (OpenTelemetry spans) — out of scope for a
  Limited Beta baseline per the contract's own "smallest useful" framing.
