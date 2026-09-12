# V9 Phase 4 — Payment Architecture & Transaction Foundation

Branch `claude/travel-planner-mvp-nvb267`. Starting checkpoint `8c4598f`
(V9 Phase 3, closed/approved). Adds the financial transaction layer around
the existing Search → Recommendation → Tier → TravelerParty → Review →
Confirmation → Revalidation → Multi-leg booking orchestration → Travel
Pass → Cancellation/change/recovery pipeline, without reopening any of it.

**Core principle, enforced structurally, not just by convention:**
`CUSTOMER PAYMENT != SUPPLIER BOOKING`, `PAYMENT STATE != BOOKING STATE`.
Two independent state machines, coordinated by a third layer that owns
neither.

## Architecture

```
CommercialQuote (V8.5, unchanged - the ONLY source of customer-price truth)
      |
CheckoutSnapshot                <- NEW: immutable, versioned, TTL-bounded
  (persistence/payments.py)         freeze of what a payment refers to
      |
PaymentTransaction               <- NEW: models/payment.py state machine
  (models/payment.py,
   services/payment_service.py)
      |
PaymentProvider (protocol)       <- NEW: providers/payment_provider.py
      |                  \
SandboxPaymentProvider    StripePaymentProvider   <- NEW, both implement
(deterministic, in-        (real REST adapter,        the same protocol
 process, what every        never live-tested here)
 test in this phase
 actually exercises)
      |
run_paid_booking()               <- NEW: services/payment_booking_orchestrator.py
  authorization-first coordinator between payment_service and the
  EXISTING, UNCHANGED booking_orchestrator.run_booking()
      |
api/payments.py (consumer)  +  api/ops_payments.py (Ops)
```

Nothing above the `CheckoutSnapshot` line changed. `run_booking()` itself
is called unmodified; the orchestrator only decides, from its *return
value*, what payment action follows.

---

## Payment provider decision (§M)

**Selected: Stripe**, for the EU/Germany target market — broad card + SEPA
coverage, native SCA/3DS via PaymentIntents, first-class manual-capture
authorization (`capture_method=manual`), a mature Refunds API, signed
webhooks with a documented, verifiable HMAC scheme, an unambiguous test/live
key prefix (`sk_test_`/`sk_live_`), and a REST API this codebase's existing
`providers/http.py` transport already fits with no vendored SDK.

This is an **engineering judgement, not a legal one** — see
[Legal/compliance open items](#legalcompliance-open-items). No claim is made
here about Stripe being contractually or legally sufficient for Detoura's
merchant-of-record structure.

**Honesty about verification**: this environment has no live Stripe test
API key. `providers/stripe_payment.py` is written faithfully against
Stripe's publicly documented REST API (PaymentIntents, Refunds, webhook
signature scheme) and is exercised by this project's tests only through its
pure/HTTP-mocked paths (`tests/test_v9_phase4_providers.py`) — never against
Stripe's real servers. The **sandbox adapter**
(`providers/sandbox_payment.py`) is what every other Phase 4 test, and the
API by default, actually runs end-to-end: a deterministic, in-process,
stateful reference implementation of the exact same `PaymentProvider`
protocol, mirroring the precedent `SnapshotTransportProvider` already set
for Duffel. If a real `STRIPE_SECRET_KEY` is ever supplied, the Stripe
adapter is what runs — but that has not happened in this environment, and
this report does not claim a live-verified Stripe integration.

**Capability differences are modelled explicitly** via
`ProviderCapabilities` (manual capture, partial capture, partial refund,
customer-action/SCA, webhooks, idempotency keys, unsupported payment
methods) — never assumed uniform across providers.

**Deferred payment methods**: BNPL and crypto are explicitly declared
unsupported by both adapters' `capabilities()` — out of scope per the Phase
4 brief, not silently ignored.

---

## Payment domain (§A/§D)

`models/payment.py`, mirroring the exact style of `models/booking.py`'s
`BookingState`/`ALLOWED_TRANSITIONS` pattern:

- **`PaymentStatus`**: `CREATED`, `REQUIRES_CUSTOMER_ACTION`, `AUTHORIZED`,
  `CAPTURE_PENDING`, `CAPTURED`, `FAILED`, `CANCEL_PENDING`, `CANCELLED`,
  `REFUND_PENDING`, `PARTIALLY_REFUNDED`, `REFUNDED`, `UNKNOWN`,
  `RECONCILIATION_REQUIRED` — with a fully explicit `ALLOWED_TRANSITIONS`
  table. Same-state transitions are always allowed (idempotent no-op);
  every other transition not in the table raises `InvalidPaymentTransition`
  — fails closed.
- **`TERMINAL_STATUSES = {CANCELLED, FAILED, REFUNDED}`** — `CAPTURED` is
  deliberately excluded: it legitimately transitions onward to refund
  states, so it is not a state-machine dead end. **`SETTLED_STATUSES =
  {CAPTURED, REFUNDED}`** is the separate "money successfully collected"
  concept this distinction needed (see [Bugs found](#bugs-found-and-fixed-during-self-directed-qa)).
- **`CheckoutSnapshot`** (frozen): the immutable, versioned freeze of
  journey identity, service tier, the full `CommercialQuote` (never
  recomputed), and a TTL-bounded `expires_at`. Persisted (`checkout_snapshots`
  table) — durable across a process restart, unlike Phase 1's in-memory
  `SelectionStore`, which was evaluated and found insufficient for this
  purpose.
- **`PaymentTransaction`** (frozen): `payment_id`, `journey_reference`,
  `booking_id`, `user_id` (nullable — anonymous flow), `checkout_snapshot_id`,
  `currency`, `customer_total`, `status`, `provider`,
  `provider_payment_reference`, `idempotency_key` (unique), `authorized_
  amount`/`captured_amount`/`refunded_amount`, timestamps, `version`
  (optimistic concurrency). A Pydantic validator rejects `captured_amount >
  authorized_amount` (when authorized > 0) and `refunded_amount >
  captured_amount` at construction time — an invalid amount shape cannot
  even be built, let alone persisted.
- **`Refund`** (frozen), **`PaymentEvent`** (append-only ledger row),
  **`PaymentAllocation`**, **`ReconciliationFinding`**.
- **No `Refund.is_full` property.** Full-vs-partial is derived by the
  *service* layer from arithmetic (`abs(new_refunded - captured) < CENTS`),
  never asserted by the model — the exact bug class named in the brief
  ("partial refund must never be labeled full") is structurally
  impossible to introduce by a model author asserting the wrong thing.

## Money truth (§B)

Nothing in the payment layer computes a customer price. `freeze_checkout_
snapshot()` takes an already-priced `CommercialQuote` as a required
argument and freezes it verbatim; `create_payment()` copies `customer_
total`/`currency` from the snapshot, never from a request body or a fresh
computation. `_write_allocations()` builds `PaymentAllocation` rows
directly from `quote.breakdown`'s existing `supplier_total`, `detoura_
service_fee`, `detoura_markup`, `tax`, `discount` fields — it does not
duplicate V8.5's commercial engine, only reads its already-computed
output. A static AST-based test
(`test_v9_phase4_domain.py::test_market_prior_and_optimizer_estimate_types_never_imported_by_payment_domain`)
asserts `models/payment.py` never imports `market_prior`/`opportunity`/
`beam_search`/`candidate_funnel` — Bootstrap Market Prior and optimizer
estimates cannot become payable price, by construction, not by convention.

## Idempotency (§J) and concurrency

- **Creation** (payment, refund): `INSERT` with a `UNIQUE` constraint on
  `idempotency_key`; a duplicate raises `sqlite3.IntegrityError`, caught,
  and the existing row is re-`SELECT`ed and returned (`created=False`) —
  never a read-then-write race.
- **State transitions**: every mutating write is a compare-and-swap —
  `UPDATE ... WHERE id=? AND version=?`; `rowcount==0` raises `StaleVersion`.
- **Webhook/provider events**: `payment_provider_events` has `PRIMARY KEY
  (provider, provider_event_id)` — the `INSERT` itself is the atomic
  dedup claim.
- **Provider-facing idempotency keys**: `_provider_idempotency_key(payment_id,
  operation, version)` — deterministic per (payment, operation, **and
  current version**). The version is deliberately included: see the capture/
  cancel/UNKNOWN-recovery bug below.

## Booking + Payment orchestration (§E/§F/§G)

`services/payment_booking_orchestrator.py::run_paid_booking()` — **payment,
booking, and recovery are reasoned about independently**, never merged into
one state machine. It calls `payment_service` for every money action and
the existing, unmodified `booking_orchestrator.run_booking()` for every
supplier action, and decides what one implies for the other.

**Selected strategy: authorization-first**, exactly as the brief's §F
preference:

1. Freeze the checkout snapshot, create the payment, **authorize**.
2. Only once authorized does `run_booking()` run — revalidate, then issue
   leg by leg.
3. `run.phase is COMPLETE` → **capture**.
4. A pre-booking failure (`FAILED`, `RECONFIRM_REQUIRED`,
   `PRICE_INCONSISTENT` — nothing was ever sent to a supplier, or a price
   change means nothing should have been) → **cancel/release the
   authorization in full**. No captured money.
5. `PARTIAL_FAILURE` (the hardest case, §G) → **held at
   `RECONCILIATION_REQUIRED`**, never auto-captured, never auto-cancelled.
   Capturing the full amount would charge for an incomplete trip;
   cancelling would leave Detoura exposed for legs it *did* successfully
   commit to. Neither is a decision either state machine can make alone —
   it is recorded with full detail (which items confirmed/failed/
   unattempted) for a human to resolve, coordinating cleanly with the
   booking side's own `RECOVERY_REQUIRED`/`PARTIAL_FAILURE` states.

## Refunds (§I)

Full and partial are the same code path — never re-implemented as
"generic" (Ops) vs. "consumer" variants with different invariants. `0 <=
refunded_amount <= captured_amount` is enforced both at the model
(validator) and at the service (`InvalidRefundAmount` before any provider
call). The consumer API (`POST /payments/{id}/refund`) only ever refunds
the **full remaining amount** — a client cannot choose an amount; only
`api/ops_payments.py`'s Ops refund endpoint may pass an explicit partial
amount, through the identical `payment_service.request_refund()`.

## Reconciliation (§K/§P)

`reconcile_payment()` is the **only** path that resolves `UNKNOWN`/
`RECONCILIATION_REQUIRED` — always by calling the provider's own
`retrieve()`, never by trusting a caller's (including Ops's, including a
webhook's) claim. `_classify()` + `_SAFE_SYNC_MAP` distinguish:

- **SAFE_TO_SYNC** — a narrow, explicitly-enumerated set of unambiguous
  local→provider combinations (e.g. local `UNKNOWN`, provider `authorized`
  → sync to `AUTHORIZED`) — applied automatically, with a `RECONCILED`
  ledger event.
- **REVIEW_REQUIRED** / **CRITICAL** — everything else becomes a
  `ReconciliationFinding` a human inspects via Ops; the payment escalates
  to `RECONCILIATION_REQUIRED` if it was not already there. Nothing is
  auto-corrected.

`RECONCILIATION_REQUIRED` deliberately has an *asymmetric* safe-sync
policy: `provider says "captured"/"refunded"/"partially_refunded"` is
safe to sync (proves an ambiguous refund/cancel attempt on an
already-captured payment never actually changed anything), but `provider
says "authorized"/"cancelled"` is **not** auto-synced from this state,
because that is exactly what a §G partial-booking-failure hold's own
provider-side state looks like — auto-syncing it would silently resolve a
hold that is supposed to require an explicit human capture/cancel
decision.

## Security (§S/§T/§Y)

- **IDOR**: `_get_owned_payment()` returns `None` — identical to "does not
  exist" — for both a missing payment and one owned by a different signed-in
  user (the same anti-enumeration pattern as Phase 2.6's My Trips). Verified
  for read, confirm, and refund.
- **CSRF**: double-submit check (`require_csrf`, reused from `api/auth.py`)
  on confirm/refund whenever a session is present; the anonymous flow (no
  session) is unaffected, preserving legacy compatibility.
- **Amount/currency tampering**: `POST /payments` reads only `booking_id`
  and `idempotency_key` from the request body — any other field (`amount`,
  `currency`, `customer_total`, …) is silently ignored; the payable amount
  always comes from the booking's own already-priced `CommercialQuote`.
  Verified by a test that submits `amount=1.00` against a €150 booking and
  asserts the created payment is still €150.
- **Quote replay/expiry**: an expired `CheckoutSnapshot` makes both
  `create_payment` and `confirm` fail explicitly (`QuoteExpired` /
  HTTP 409) — never silently charged at the old or a recomputed amount.
- **Webhook forgery/replay**: an invalid/unsigned event is rejected (400);
  a provider-name mismatch against this deployment's configured provider is
  rejected (400); a duplicate `provider_event_id` is a no-op
  (`duplicate_ignored`) via the atomic dedup claim; a webhook's own claimed
  status is never trusted directly — it only triggers a *real*
  `reconcile_payment()` call.
- **Ops**: every route requires `DETOURA_OPS_TOKEN` (503 otherwise, fail
  closed — the same posture as every other Ops router). There is **no
  generic "set payment status" endpoint** — every Ops action goes through
  the identical `payment_service` functions a normal request would; a
  forged `status`/`amount` field in a request body that the endpoint does
  not read is simply ignored (verified explicitly).

---

## Bugs found and fixed during self-directed QA

Every one of these was found by this project's own tests/smoke-testing
before any external QA pass, and each has explicit regression coverage.

1. **`SandboxPaymentProvider` deadlock** — `_cached_or()` held a
   `threading.Lock` across the whole `compute()` call, which itself
   re-acquired the same lock. Fixed: `RLock`.
2. **Fault injection not wired for `capture`/`cancel_authorization`/
   `refund`** — these only receive the sandbox-*generated*
   `provider_reference`, never the caller's Detoura reference the magic
   suffix lives on. Fixed: `_Account.detoura_reference` remembers the
   original reference at `authorize` time.
3. **Ledger ordering** — `payment_events` rows written within one logical
   operation can share an identical `occurred_at`; sorting by
   `(occurred_at, event_id)` (a random token) produced a non-chronological
   order. Fixed: sort by `(occurred_at, rowid)`.
4. **`TERMINAL_STATUSES` incorrectly included `CAPTURED`**, which has
   legitimate outgoing transitions to refund states. Fixed: removed it,
   added `SETTLED_STATUSES` for the distinct "money collected" concept.
5. **Provider idempotency keys permanently "poisoned" a stuck payment.**
   `_provider_idempotency_key(payment_id, operation)` was keyed only on
   the payment and operation — so once a capture/cancel/authorize call
   returned `UNKNOWN` (a real timeout), the sandbox's own idempotency-key
   cache would replay that identical `UNKNOWN` result **forever**, on
   every future retry, even after `reconcile_payment()` proved via
   `retrieve()` that the operation genuinely never executed at the
   provider. This is a severe bug: it would make a stuck authorization/
   capture *permanently unrecoverable* — violating invariant §X.18
   directly (a real production incident: money authorized, held forever).
   Fixed: the key now also incorporates the payment's **current
   `version`**, so a real state change (a reconciliation sync bumps the
   version via its own CAS) naturally produces a fresh provider
   idempotency key on the next attempt, while two calls racing at the
   *same* version (a genuine concurrent duplicate) still collapse onto one
   provider call. Regression: `test_capture_unknown_reconciles_without_
   duplicate_capture`, `test_refund_unknown_reconciles_without_duplicate_
   refund` (both use a purpose-built `OneShotUnknown` provider wrapper
   that fails exactly once, rather than the sandbox's *permanent*
   scripted-suffix faults, to distinguish "timeout that later clears" from
   "this test card always declines").
6. **`_SAFE_SYNC_MAP` had no entry for `RECONCILIATION_REQUIRED`** at all,
   so a refund that went `UNKNOWN` (escalating straight to
   `RECONCILIATION_REQUIRED`, not `UNKNOWN`) could reconcile and learn the
   true state, but had **no path back to an actionable status** — Ops
   could inspect the finding but never resolve it, since there is
   deliberately no generic status-setter. Fixed: added a narrow,
   deliberately asymmetric safe-sync policy for this source state (see
   [Reconciliation](#reconciliation-kp) above) that resolves the
   refund-ambiguity case without reopening the partial-booking-failure
   hold.
7. **`resolve_provider()` constructed a brand-new `SandboxPaymentProvider()`
   on every call** — the single most severe finding. The sandbox is
   deliberately *stateful* in-process (it must remember an authorized
   reference so a later capture/refund/reconcile call can find it), but
   every HTTP request calls `resolve_provider()` independently, so a
   confirm's authorize and a later capture/refund/webhook-triggered
   reconcile would **always** get two different, unrelated sandbox
   instances — the second one's account table empty, "not_found",
   capture/refund permanently `FAILED`. This would have made the entire
   sandbox payment flow non-functional beyond a single request in any
   real deployment (every service-layer/orchestrator test in this suite
   passed a single provider object explicitly across calls within one
   test function, which is exactly why this did not surface until the
   HTTP-level API security tests, which go through `resolve_provider()`
   the way real traffic does). Fixed: `payment_config._sandbox_provider()`
   is a lazy, process-wide singleton (mirroring `booking_flow.
   booking_store()`'s existing pattern); `reset_payment_config()` clears
   it too, for test isolation.

---

## Tests

| File | Count | Covers |
|---|---:|---|
| `test_v9_phase4_domain.py` | 20 | State machine, money invariants, snapshot expiry, static money-truth proof |
| `test_v9_phase4_service.py` | 21 | Idempotency, refund partial/full, reconciliation classification, allocations, CAS, audit trail |
| `test_v9_phase4_orchestrator.py` | 13 | All 6 mandated controlled E2E scenarios + additional named failure-injection cases |
| `test_v9_phase4_providers.py` | 33 | Sandbox + Stripe adapters, webhook signature/replay, TEST/LIVE guard |
| `test_v9_phase4_api_security.py` | 19 | IDOR, CSRF, tampering, quote expiry, webhook forgery/replay, Ops gating |
| **Total** | **106** | |

### Controlled E2E — exact results

1. **HAPPY PATH** — quote → payment → authorization → multi-leg booking →
   capture → `CAPTURED` → booking `COMPLETE`. PASS.
2. **BOOKING FAILURE BEFORE SUPPLIER COMMITMENT** — auth → booking `FAILED`
   → authorization `CANCELLED` → `captured_amount == 0`. PASS.
3. **PARTIAL BOOKING FAILURE** — auth → leg A `CONFIRMED`, leg B `FAILED`
   → `RECONCILIATION_REQUIRED`, `authorized_amount` intact, `captured_
   amount == 0`, ledger records exactly which items confirmed/failed. PASS.
4. **CAPTURE UNKNOWN** — capture times out once → `UNKNOWN` → reconcile →
   true state (`AUTHORIZED`, never captured) → a fresh capture (new
   provider idempotency key) succeeds, no duplicate. PASS.
5. **REFUND UNKNOWN** — refund times out once → `RECONCILIATION_REQUIRED`
   → reconcile → synced back to `CAPTURED` (refund never applied) → a
   fresh refund succeeds, no duplicate. PASS.
6. **DUPLICATE CONFIRM** — 5 concurrent `authorize_payment` calls against
   the same payment → one financial execution (one provider reference, one
   authorized amount), no errors. PASS.

---

## Known limitations

- **Stripe adapter is not live-verified.** No real test-mode API key was
  available in this environment. It is written faithfully against Stripe's
  documented API and unit-tested at the pure-mapping/HTTP-mocked level
  only.
- **Production/live charging remains disabled.** `PaymentConfig.
  live_charging_enabled` defaults to `False`; even set `True` with
  `PAYMENT_PROVIDER=stripe`, a live (`sk_live_`) key is refused by
  `StripePaymentProvider.__post_init__` unless `allow_non_test_key=True`
  is passed explicitly at construction — which `resolve_provider()` never
  does. Going live requires a deliberate code change and a separate
  release decision, not a config flag alone.
- **FX/multi-currency is out of scope.** Every payment binds to its
  checkout snapshot's single currency; nothing here converts between
  currencies.
- **`_SAFE_SYNC_MAP` is deliberately narrow.** Combinations outside the
  enumerated safe set always become a human-reviewed finding — this is
  intentional conservatism (§P), not incompleteness, but it does mean some
  legitimately-safe-in-practice states require a manual Ops resolution
  step this phase does not attempt to eliminate.
- **No confirmation email, invoice, or UI** — explicitly out of scope per
  the brief; `api/payments.py` is a backend/API seam only, for
  ChatGPT-owned UI to integrate against later.
- **Ledger is append-only rows, not a full accounting platform** — enough
  to reconstruct a payment's history and audit every irreversible action,
  not a general ledger/ERP system.

## Legal/compliance open items

Explicitly **not** resolved by this phase; each requires a specialist
review before real money moves:

- Merchant-of-record / contractual structure between Detoura, the
  traveller, and Stripe.
- PSD2/SCA regulatory implications for Detoura's specific flow.
- Full PSP integration architecture (settlement, payouts, reserves).
- VAT/tax treatment of the Detoura service fee vs. supplier fare.
- Consumer refund and cancellation-rights obligations (EU distance-selling,
  package-travel rules).
- Package-travel classification (whether Detoura's booking of multiple
  supplier legs for one price triggers package-travel regulation).
- Insolvency protection requirements for prepaid travel funds.
- Payment surcharge restrictions (EU rules on passing card fees to
  consumers).
- Invoice requirements (VAT-compliant invoice generation is out of scope
  here; see "Out of scope").

This phase's own claim is limited to: the payment **architecture**,
**state machine**, **ledger**, **provider abstraction**, **sandbox
integration**, and **recovery/reconciliation** exist and are tested. It
does not claim Detoura is legally ready to take real payments.
