# V9 Beta Contract & State-Machine Lock

**Documentation only. No executable code changed. No frontend touched.
Nothing committed.**

Audited HEAD: `6b47e2d`. This is the authoritative Limited-Beta contract
for Your Journey → Checkout → Payment → Booking → Recovery → Customer
Documents → My Trips, derived entirely from the executable code that
already exists — not a new architecture. It is meant to be the shared
source of truth for backend implementation, product/UX design, and
frontend integration on the next slice (Payment↔Booking Coupling).

Every claim below cites the actual file:line it was read from. Where a
prior report (Phase 4/5/6, the Limited Beta Reality Audit) is mentioned, it
is context, not evidence — the evidence is the code citation next to it.

---

## Absolute truth rules (carried forward, not weakened)

`BOOKING SUCCESS != EMAIL SUCCESS` / `EMAIL FAILURE != BOOKING FAILURE` —
verified structurally this slice, not merely by convention:
`communication_service.py` never imports `persistence.bookings` or
`persistence.payments` at all, and `post_booking_finalizer._send_communication_safely`
wraps the entire send in a bare `except Exception` that only ever returns
`(None, str(error))` (`post_booking_finalizer.py:246-276`).

`PAYMENT AUTHORIZED != PAYMENT CAPTURED` — `PaymentStatus.CAPTURED` is
deliberately **not** in `TERMINAL_STATUSES` (`payment.py:136-138`); capture
is its own explicit, separately-gated transition (`payment_service.py:245-285`).

`PAYMENT UNKNOWN != PAID` — `UNKNOWN` is in `MONEY_AT_RISK_STATUSES`
(`payment.py:147-152`), never in `SETTLED_STATUSES = {CAPTURED, REFUNDED}`
(`payment.py:143`); the only function that resolves it is `reconcile_payment`,
which always re-asks the provider, never trusts a local guess
(`payment_service.py:464-521`).

`RECOVERY_REQUIRED != CONFIRMED` — `evaluate_confirmation_eligibility`'s
`recovery_state` check is unconditional and short-circuits straight to
`PARTIAL_RECOVERY` before any other rule, including a phase that would
otherwise qualify as `CONFIRMED` (`models/confirmation.py:350-357`).

`PROVIDER TIMEOUT != PROVIDER FAILURE` — `_revalidate_item`/`_issue_item`
catch `TimeoutError` (checked ahead of `OSError`, since it's a subclass)
into `BookingState.TIMEOUT` distinctly from `(ProviderHttpError, OSError)`
into `BookingState.PROVIDER_FAILURE` — both route toward
`RECOVERY_REQUIRED`, never a blind `FAILED` that would read as "safe to
just retry" (`booking_orchestrator.py:466-576`, docstring 521-539).

`CUSTOMER FINANCIAL DOCUMENT != SUPPLIER INVOICE` — `FinancialDocument`'s
`ItineraryLeg` conversion structurally cannot carry a provider order/offer
id (`financial_document_pdf.py` docstring 29-36, enforced at the
`financial_document_service._itinerary` boundary).

`TRAVELER != USER ACCOUNT` — traveler data (name/DOB/passport) lives on
`TravelerParty`/`BookingRun.party`, never on the account/session model;
ownership is resolved from `owner_user_id`, set once from the server-side
session at booking-intent creation (`api/v1.py:974`), never inferred from a
traveler's email.

`BOOKING OWNERSHIP MUST COME FROM SERVER-RESOLVED ACCOUNT CONTEXT` —
confirmed: `owner_user_id = session.user_id if session is not None else None`
(`api/v1.py:974`), never a client-supplied field
(`booking_orchestrator.py:172-179` docstring: "never accepted from a
client-supplied field, and never reassigned afterwards").

`ANONYMOUS BOOKING REMAINS VALID` — confirmed the default path; `claim_trip`
is idempotent for the same user and refuses (never overwrites) for a
different one (`persistence/accounts.py:169-201`).

`SERVER OWNS COMMERCIAL/FINANCIAL TRUTH` — no client-suppliable
price/fee/markup field exists on the booking-intent request model
(confirmed, Limited Beta Reality Audit §7, re-confirmed this slice via
`CommercialPricingService.quote()`'s signature, `commercial.py:65-77` —
every pricing input is either server-computed or a policy/promo lookup).

`MARKET PRIOR IS NEVER A BOOKABLE PRICE` — `not_a_quote: bool = True` is
stamped on every prior row/signal; no accessor yields a bookable amount
(`models/market_prior.py`, established in the Search Intelligence Slice 1.5
report, unaffected by this slice).

`ESTIMATED ACCOMMODATION/TRANSFER COSTS ARE NOT PAYABLE-NOW ITEMS` —
confirmed structurally: `PriceBreakdown` has no accommodation/transfer/extras
field at all (`models/commercial.py:68-139`); `_supplier_transport(run)`
explicitly states "the whole-trip estimate ... is never used here"
(`booking_commercial.py:38-44`).

No Phase 4/5/6 security, payment, booking, ownership, idempotency, privacy,
or recovery invariant is weakened by anything in this document — this
document only names and reconciles what already exists.

---

## Part 1–3. Current state machines and transitions (verbatim)

### Journey/Selection

`SelectedOffer` (frozen dataclass, `selection_store.py:46-72`): `offer_id,
provider, origin, destination, leg_label, travelers, discovered_amount,
discovered_currency, discovered_raw_amount, discovered_raw_currency,
discovered_baggage_cabin, discovered_baggage_checked, discovered_hold_supported,
discovered_expires_at, discovered_departure, discovered_arrival, required`.

`Selection` (frozen dataclass, `selection_store.py:75-85`): `selection_id,
recommendation_id, trip_label, currency, discovered_total, offers,
created_at`.

No enum here — a `Selection` either exists in the in-memory
`SelectionStore` (process-local `OrderedDict`, 20-minute TTL, lazy
expiry-on-read, `selection_store.py:39,94-95,142-157`) or it has expired/
never existed (`get()` returns `None`).

| | Meaning | Who enters | Who leaves | Persisted? | Customer-safe? | Terminal? | Retryable? | Side effects |
|---|---|---|---|---|---|---|---|---|
| Selection exists | A real (live-search-derived) set of offers is available to book | `live_search.py::_selected_offers_for` → `selection_store().record()` (`live_search.py:225-237`) | expiry (TTL) or `create_run_from_selection` consuming it (read-only, doesn't delete) | In-process only, **not durable** (no DB row, no Redis — single-worker structure by design, `selection_store.py:14-17`) | Yes (search results) | N/A | N/A | none |
| Selection expired/missing | `GET`/consume fails | TTL elapses or process restarts | — | — | — | — | — | none |

### Booking (`BookingState`, `models/booking.py:33-58`)

Verbatim members: `DRAFT, REVALIDATING, READY, USER_CONFIRMED, BOOKING,
CONFIRMED, PRICE_CHANGED, UNAVAILABLE, EXPIRED, TIMEOUT, PROVIDER_FAILURE,
PARTIAL_FAILURE, RECOVERY_REQUIRED, FAILED, NOT_ATTEMPTED`.

`ALLOWED_TRANSITIONS` (`booking.py:89-128`):

| State | Meaning | Enters from | Persisted? | Customer-safe? | Terminal? | Retryable? |
|---|---|---|---|---|---|---|
| `DRAFT` | leg not yet touched | initial construction | Item, in `BookingRun` (see below) | Yes | No (→REVALIDATING, FAILED) | N/A |
| `REVALIDATING` | fresh price/availability check in flight | `run_booking`, `guided_booking.prepare_journey` | Yes | Yes | No | N/A |
| `READY` | revalidated, bookable | `_revalidate_item` success | Yes | Yes | No | N/A |
| `USER_CONFIRMED` | about to attempt issuance | `run_booking` issuance loop | Yes | Yes | No | N/A |
| `BOOKING` | issuance request in flight | `run_booking` | Yes | Yes | No | N/A |
| `CONFIRMED` | ticket issued, `provider_order_id` set | `_issue_item` success | Yes | Yes | **Yes** | No |
| `PRICE_CHANGED` | revalidation found a price move | `_revalidate_item` | Yes | Yes | No | Yes (→REVALIDATING/USER_CONFIRMED) |
| `UNAVAILABLE` | fare gone | `_revalidate_item`/`_issue_item` (`DuffelOfferGone`) | Yes | Yes | No | Yes (→REVALIDATING) |
| `EXPIRED` | offer hold expired | `_revalidate_item` | Yes | Yes | No | Yes |
| `TIMEOUT` | **uncertain** — request timed out, outcome unknown | `_revalidate_item`/`_issue_item` (`TimeoutError`) | Yes | With care (see §11) | No | →REVALIDATING or RECOVERY_REQUIRED |
| `PROVIDER_FAILURE` | **uncertain** — transport/HTTP error, outcome unknown | `_revalidate_item`/`_issue_item` (`ProviderHttpError`/`OSError`) | Yes | With care | No | →REVALIDATING or RECOVERY_REQUIRED |
| `PARTIAL_FAILURE` | journey-level: some required legs confirmed, at least one didn't | derived (`JourneyBookingIntent.outcome`) | Yes | Yes, but framed carefully | No (→RECOVERY_REQUIRED only) | No, human-gated |
| `RECOVERY_REQUIRED` | a human must decide | reachable from TIMEOUT/PROVIDER_FAILURE/PARTIAL_FAILURE | Yes | Yes | No (→FAILED only) | Ops-only |
| `FAILED` | terminal failure | many paths | Yes | Yes | **Yes** | No |
| `NOT_ATTEMPTED` | never tried (e.g. optional leg skipped after a required-leg stop) | `_mark_unattempted`, issuance-loop `stop` branch | Yes | Yes | No | Yes |

Terminal states: `CONFIRMED`, `FAILED` only (`booking.py:89-128`, empty
outgoing transition sets). `SETTLED_STATES = {CONFIRMED}` (`booking.py:131`).

`GuidedBookingState` (`booking.py:61-80`, Basic/self-service only):
`READY_TO_BOOK, EXTERNAL_BOOKING_STARTED, BOOKING_CONFIRMATION_REQUIRED,
CONFIRMED, UNKNOWN`. `is_progress` is `True` only for `CONFIRMED`
(`booking.py:77-80`). **Detoura never self-asserts `CONFIRMED` in this
flow** — only the traveler's own report can (`mark_ticket`'s
`guided_reported_by="traveller"`, `guided_booking.py:84-105`).

### BookingRun / journey aggregate — `BookingPhase` (`booking_orchestrator.py:52-70`)

Verbatim: `AWAITING_TRAVELERS, AWAITING_CONFIRMATION, REVALIDATING,
RECONFIRM_REQUIRED, ISSUING, COMPLETE, PARTIAL_FAILURE, FAILED,
GUIDED_BOOKING, PRICE_INCONSISTENT`.

This is a **separate state axis from `BookingState`** — `BookingPhase`
describes the whole journey/run; `BookingState` describes one leg/item.

| Phase | Meaning | Entered by | Customer-safe? | Terminal? |
|---|---|---|---|---|
| `AWAITING_TRAVELERS` | run created, party not yet attached | `create_run_from_selection`/`create_run_demo` | Yes | No |
| `AWAITING_CONFIRMATION` | party attached, ready for user's final confirm | `attach_travelers` | Yes | No |
| `REVALIDATING` | `claim_for_execution` has fired — the atomic single-execution claim | `claim_for_execution` (`booking_orchestrator.py:264-302`) | Yes | No |
| `RECONFIRM_REQUIRED` | price moved beyond tolerance during revalidation | `run_booking` (`booking_orchestrator.py:369-375`) | Yes | No (loops back through `AWAITING_CONFIRMATION`/`REVALIDATING` on new confirm) |
| `ISSUING` | tickets are being issued | `run_booking` (`:376`) | Yes | No |
| `COMPLETE` | all required legs `CONFIRMED` | `run_booking` outcome derivation (`:431-438`) | Yes | **Yes** (terminal phase) |
| `PARTIAL_FAILURE` | some required legs failed after issuance began | same | Yes, with care | **Yes** (feeds `RECOVERY_REQUIRED` at the confirmation layer) |
| `FAILED` | no required legs confirmed | same, or revalidation-stage stop (`:358-368`) | Yes | **Yes** |
| `GUIDED_BOOKING` | Basic/self-service — no Detoura-issued order | `guided_booking.prepare_journey` (`:66`) | Yes | Terminal for the orchestrator (traveler owns the rest) |
| `PRICE_INCONSISTENT` | server-side commercial-quote vs. bookable-fare mismatch caught at confirm time | `confirm_booking` (`v1.py:1136-1138`) | Yes | No — blocks confirmation outright, nothing booked |

`claim_for_execution` is the **single-execution guard**: under one
`run._lock` acquisition, it requires `run.phase in (AWAITING_CONFIRMATION,
RECONFIRM_REQUIRED)` or raises `ValueError`; the phase-set to
`REVALIDATING` happens in the same critical section
(`booking_orchestrator.py:264-302`). This is the only way `run.phase`
leaves those two states into `REVALIDATING` — **Classification: SAFE**
idempotency protection (§18).

### Confirmation (`ConfirmationStatus`, `models/confirmation.py:143-176`)

Verbatim: `PENDING_VERIFICATION, CONFIRMED, PARTIAL_RECOVERY, CANCELLED,
SUPERSEDED`. `ALLOWED_TRANSITIONS` (`:182-208`): `PENDING_VERIFICATION`→
`{CONFIRMED, PARTIAL_RECOVERY, CANCELLED, SUPERSEDED}`; `CONFIRMED`→
`{PARTIAL_RECOVERY, CANCELLED, SUPERSEDED}` (**not terminal** — an airline
can cancel a leg after Detoura confirms); `PARTIAL_RECOVERY`→`{CONFIRMED,
CANCELLED, SUPERSEDED}`; `CANCELLED`/`SUPERSEDED`→`{}`. `TERMINAL_STATUSES
= {CANCELLED, SUPERSEDED}` (`:211-213`); `FINALIZED_STATUSES = {CONFIRMED,
CANCELLED, SUPERSEDED}` (`:218-222` — `PARTIAL_RECOVERY`/
`PENDING_VERIFICATION` explicitly **not** finalized, i.e. still "in play").

`evaluate_confirmation_eligibility(*, booking_phase, payment_status,
has_payment, recovery_state) -> ConfirmationStatus | None`
(`models/confirmation.py:260-373`) — this is the **single authoritative
function that decides what a customer's confirmation status is**, and its
rule order matters (first match wins):
1. Booking failed with money committed → `PARTIAL_RECOVERY`; no money
   committed → `None`.
2. Pre-commitment phase → same as above.
3. **`recovery_state` set at all → unconditionally `PARTIAL_RECOVERY`**,
   regardless of what phase/payment would otherwise imply.
4. `phase == PARTIAL_FAILURE` → `PARTIAL_RECOVERY`.
5. Payment status indeterminate (`UNKNOWN`/`RECONCILIATION_REQUIRED`/
   missing) → `PENDING_VERIFICATION`.
6. `phase == COMPLETE` **and** payment paid → `CONFIRMED` — **the only
   route to `CONFIRMED`**.
7. Anything else (including an unrecognized phase string) →
   `PENDING_VERIFICATION` (fail-closed default).

### Financial documents

`FinancialDocumentType` (`models/financial_document.py:89-105`): exactly
`RECEIPT, INVOICE, CREDIT_NOTE`. `FinancialDocumentStatus` (`:108-119`,
**derived, never stored**): `ISSUED, SUPERSEDED`. Immutability: no
`update_document` function exists anywhere in
`persistence/financial_documents.py` (confirmed by exhaustive symbol grep);
the model itself is `ConfigDict(frozen=True)` with no `version` field by
design (`:24-31`).

### Communication (`CommunicationStatus`, `models/communication.py:41-61`)

Verbatim: `PENDING, SENDING, SENT, FAILED, UNKNOWN`. `ALLOWED_TRANSITIONS`
(`:67-83`): `PENDING`→`{SENDING, FAILED, UNKNOWN}`; `SENDING`→`{SENT,
FAILED, UNKNOWN}`; `SENT`/`FAILED`→`{}`; `UNKNOWN`→`{SENT, FAILED}`
(reconciliation only, never auto-resolved).

### Payment (`PaymentStatus`, `models/payment.py:32-62`)

Verbatim, 13 members: `CREATED, REQUIRES_CUSTOMER_ACTION, AUTHORIZED,
CAPTURE_PENDING, CAPTURED, FAILED, CANCEL_PENDING, CANCELLED,
REFUND_PENDING, PARTIALLY_REFUNDED, REFUNDED, UNKNOWN,
RECONCILIATION_REQUIRED`. Full `ALLOWED_TRANSITIONS` table at
`payment.py:68-130`. `TERMINAL_STATUSES = {CANCELLED, FAILED, REFUNDED}`
(`:136-138` — **`CAPTURED` deliberately not terminal**).
`SETTLED_STATUSES = {CAPTURED, REFUNDED}` (`:143`).
`MONEY_AT_RISK_STATUSES = {AUTHORIZED, CAPTURE_PENDING, CAPTURED,
CANCEL_PENDING, REFUND_PENDING, PARTIALLY_REFUNDED, UNKNOWN,
RECONCILIATION_REQUIRED}` (`:147-152`).

| State | Meaning | Persisted? | Customer-safe? | Terminal? |
|---|---|---|---|---|
| `CREATED` | payment row exists, no provider call yet | Yes | Yes | No |
| `REQUIRES_CUSTOMER_ACTION` | provider needs 3DS/etc. | Yes | Yes | No |
| `AUTHORIZED` | funds held, not captured | Yes | Yes | No |
| `CAPTURE_PENDING` | capture request in flight | Yes | Yes | No |
| `CAPTURED` | funds taken — **not terminal**, refund still possible | Yes | Yes | No (by design) |
| `FAILED` | authorization/capture definitively failed | Yes | Yes | **Yes** |
| `CANCEL_PENDING` | authorization release in flight | Yes | Yes | No |
| `CANCELLED` | authorization released, no money taken | Yes | Yes | **Yes** |
| `REFUND_PENDING` | refund in flight | Yes | Yes | No |
| `PARTIALLY_REFUNDED` | some money returned, not all | Yes | Yes | No |
| `REFUNDED` | fully refunded | Yes | Yes | **Yes** |
| `UNKNOWN` | provider outcome ambiguous (timeout/transport error) | Yes | With care | No — `reconcile_payment` only |
| `RECONCILIATION_REQUIRED` | escalated ambiguity, human-gated | Yes | With care | No — Ops `reconcile` only |

`PaymentTransaction` is itself `frozen=True`; every transition is
`model_copy` + compare-and-swap on `version`
(`persistence/payments.py:196-222`), never in-place mutation.

---

## Part 4. The disconnected boundary — precisely

**Consumer payment entry point:** `POST /api/v1/payments` →
`POST /{payment_id}/confirm` (`api/payments.py:67-148`).

**Consumer booking-confirm entry point:**
`POST /api/v1/booking-intents/{booking_id}/confirm` (`v1.py:1110-1159`).

**Every caller of `run_paid_booking()`/`run_paid_booking_and_finalize()`:**
`services/payment_booking_orchestrator.py` (its own definition), and two
test files (`tests/test_v9_phase4_orchestrator.py`,
`tests/test_v9_phase6_payment_security.py`). **Zero matches anywhere under
`src/detoura/api/`** — confirmed twice independently this program (Limited
Beta Reality Audit + this slice's own fresh grep).

**Every caller of `run_booking()`:** `services/booking_flow.py::start_confirmation`'s
background `_worker()` (`booking_flow.py:346-348`), and
`payment_booking_orchestrator.run_paid_booking` internally (unreachable,
above).

**Every caller of `start_confirmation`/`claim_for_execution`:**
`start_confirmation` ← `api/v1.py::confirm_booking` (`v1.py:1150-1156`,
All-in-One tier only). `claim_for_execution` ← `start_confirmation`
(`booking_flow.py:330`) and `run_booking` itself when
`already_claimed=False` (`booking_orchestrator.py:341-342`).

**Every caller of `request_capture`:** `api/ops_payments.py::ops_capture`
(`:158`, Ops-only) and `payment_booking_orchestrator.py:186` (unreachable).
**No consumer route calls `request_capture` at all.**

**Every caller of `request_refund`:** `api/payments.py::refund_payment`
(consumer, full-remaining-amount only) and `api/ops_payments.py::ops_refund`
(Ops, partial allowed).

### What prevents a payment authorization from authorizing exactly one booking execution today?

**Nothing connects the two code paths.** `confirm_booking`
(`v1.py:1110-1159`) never reads, checks, or references any payment state —
it calls `prepare_journey`/`start_confirmation` unconditionally once
`reconcile_run_price` passes. `confirm_payment`
(`api/payments.py:117-148`) never reads, checks, or references any booking
state — it calls `authorize_payment` and returns. The one function that
would join them, `run_paid_booking`, exists, is fully implemented and
unit-tested, and is simply never called from either route. This is a
missing function call, not a missing capability — **the fix is wiring, not
new logic** (Part 21).

### The live, exploitable consequence of this gap — stated explicitly, per independent review

This is not merely an inconvenience. Traced end to end, today, in the live
disconnected architecture: **calling `POST /booking-intents/{id}/confirm`
on an `ALL_IN_ONE` (paid) tier booking for which no payment has ever been
created reaches `ConfirmationStatus.CONFIRMED`, a real entry in My Trips,
and an automatic "your booking is confirmed" customer email — with zero
payment ever authorized or captured.**

The mechanism: `confirm_booking` → `start_confirmation` → `run_booking`
(the background worker) can reach `BookingPhase.COMPLETE` with zero payment
awareness (confirmed above). That same worker automatically calls
`post_booking_finalizer.try_finalize` (`booking_flow.py:375`), which reads
`payment_store.list_payments_for_booking(db, booking_id)` → an **empty
list** → `has_payment=False`. `evaluate_confirmation_eligibility`'s own
rule for this exact case (`models/confirmation.py`) treats "no payment
record exists" as "nothing was owed, so nothing is outstanding" —
`payment_paid` is set `True` vacuously, and rule 6 (`phase == COMPLETE and
payment_paid`) fires, producing `ConfirmationStatus.CONFIRMED`. The
finalizer then sends the real "booking confirmed" email
(`_send_communication_safely`, gated only on confirmation status, not
payment status). The only thing this path correctly refuses is the
financial document (`issue_receipt_or_invoice` requires `captured >
CENTS`, raises `NoCapturedPayment` — silently swallowed by the finalizer's
broad `except`, so no receipt is issued, but nothing else about the
customer-facing truth reflects that).

Supplier-side exposure is bounded (Duffel orders on this path are always
Test Mode by the fail-closed token check already established in Phase 6 —
no real money moves on the supplier side), but the **customer-facing
truth is false**: Detoura tells a traveler "you have this booking" when
nothing was ever paid.

**Classification: a pre-Beta blocker, not ordinary next-slice backlog.**
Until the Payment↔Booking Coupling slice (Part 21) ships, either (a) the
`ALL_IN_ONE` tier must not be exposed to real traffic at all, or (b) a
minimal interim guard must reject `confirm_booking` for `ALL_IN_ONE`
bookings with no `AUTHORIZED`/`CAPTURED` payment on record — this document
does not choose between (a)/(b) (that is an implementation decision, out
of scope for a documentation-only slice) but states plainly that shipping
`ALL_IN_ONE` to real users before this is closed would let a real customer
receive a false "confirmed" booking and a false confirmation email for
free. `BASIC` (self-service) tier is unaffected by this specific finding —
it never asserts `CONFIRMED` from Detoura's own knowledge at all (Part 3).

---

## Part 5. Authoritative Beta flow (using existing architecture)

The mega-prompt's proposed 17-step flow is **correct and matches the
existing architecture almost exactly**, with three precision edits derived
from the code:

1. Journey selected → `SelectionStore.record()` (existing, in-memory, 20-min TTL — see Part 6 gap).
2. Booking intent created → `create_run_from_selection` (existing).
3. TravelerParty attached → `attach_travelers` (existing).
4. Server calculates commercial truth → `price_run`/`CommercialPricingService.quote` (existing).
5. **CheckoutSnapshot created/frozen → `freeze_checkout_snapshot`** (existing — this must happen *before* step 6, and the snapshot is what step 8's binding check validates against).
6. Stripe payment authorization → `create_payment` + `authorize_payment` (existing, `AUTHORIZED`).
7. User gives one final confirmation → **currently the point where `confirm_booking` is called with no awareness of step 6 at all — this is exactly the gap Part 4 identifies.**
8. Server validates authorization belongs to the exact immutable checkout/booking intent → **does not currently happen as a single check; the pieces exist (Part 6) but are not joined.**
9. Atomic booking execution claim → `claim_for_execution` (existing, SAFE per Part 3).
10. Fare revalidation → `_revalidate_item`/`run_booking`'s revalidation stage (existing).
11. Required ticket issuance → `_issue_item`/`run_booking`'s issuance loop (existing).
12. Determine booking outcome → `JourneyBookingIntent.outcome` (existing, derived never asserted).
13. Determine payment action → **this is precisely `run_paid_booking`'s decision table (Part 4/7) — implemented, unwired.**
14. Capture / refund / preserve UNKNOWN → `request_capture`/`cancel_authorization`/`mark_reconciliation_required` (existing, individually correct).
15. Generate documents → `post_booking_finalizer.try_finalize` → `_issue_document_safely` (existing, gated on `ConfirmationStatus.CONFIRMED`).
16. Persist My Trips truth → `persist_run`/`_claim_ownership` (existing — already happens at booking-intent creation, not at confirm; see Part 14 for why this matters).
17. Queue/send communication independently → `_send_communication_safely` (existing, structurally decoupled from booking/payment truth — verified this slice, not merely claimed).

**Why NOT reorder payment and irreversible booking side effects**:
`run_paid_booking`'s own "authorization-first" strategy (Part 4/7) already
encodes the correct, existing-architecture-safe ordering — authorize before
attempting anything irreversible with a provider, capture only after a
successful terminal outcome, never issue a ticket without a successful
authorization first. This is not a new design decision; it is what the
disconnected module already implements. **The authoritative flow is: Part
5's 17 steps in the order listed**, with step 13 realized as a direct call
to (or a route through) `run_paid_booking`'s existing decision logic.

---

## Part 6. Coupling key — binding identifiers

**Existing identifiers** (Part 1/4/Domain recon):
`booking_id` (stable, assigned at intent creation), `selection_id` (from
`SelectionStore`, in-memory/ephemeral), `checkout_snapshot_id`,
`payment_id`, `user_id`/`owner_user_id` (server-resolved), provider order
IDs (`item.provider_order_id`, per-leg, assigned only on `CONFIRMED`).

**Binding that must hold**: `CheckoutSnapshot.booking_id` ↔
`PaymentTransaction.checkout_snapshot_id` **already exists and is already
correct** — a payment cannot be created without a real, unexpired snapshot
(`create_payment` raises `QuoteExpired` otherwise, `payment_service.py:106-107`),
and `PaymentTransaction.customer_total` is "always copied from the
checkout snapshot's quote, never client-supplied and never recomputed"
(`payment.py:262-264`). **This is the correct, existing coupling key —
`booking_id`, already present on both `CheckoutSnapshot` and
`PaymentTransaction`.**

**Gap found (not a missing identifier, a missing field)**:
`CheckoutSnapshot`/`PaymentTransaction` carry **no `selection_id`** — only
`BookingRun.selection_id` does (`booking_flow.py:193`), a structure the
payment domain never reads. This does not block coupling (the flow never
needs to re-derive the selection from the payment side — `booking_id` is
sufficient to join `BookingRun` ↔ `CheckoutSnapshot` ↔ `PaymentTransaction`
at query time), but it means an Ops operator reading a payment record alone
cannot see which search selection produced it without a second join through
`BookingRun`. **Not a blocker; worth a one-field addition if Ops tooling
needs it later — out of scope for the coupling slice itself.**

**What must never be substitutable, and why the existing architecture
already prevents it**:
- *Another booking_id*: `create_payment`'s ownership check
  (`api/payments.py:87-93`) ties the payment to the booking's own
  `run.user_key`; `run_paid_booking` (when wired) would receive `run`
  directly, not a client-suppliable id.
- *Another payment*: `confirm_booking`'s `run_paid_booking` call (once
  wired) would need to look up the payment **by `booking_id`**, not accept
  one from the request body — the existing `confirm_booking` route
  (`v1.py:1110`) takes no payment reference in its body today
  (`ConfirmBookingRequest` per `api/contracts.py`), which is already the
  correct shape.
- *Another amount*: `PaymentTransaction.customer_total` is copied from the
  snapshot, never client-supplied (`payment.py:262-264`) — already
  enforced.
- *Another user*: both `create_payment`'s ownership check and
  `owner_user_id`'s server-resolution (Part 0) already prevent this.
- *Another journey*: `booking_id` is the single join key across every
  domain (`BookingRun`, `CheckoutSnapshot`, `PaymentTransaction`,
  `JourneyConfirmation` all key on it) — already sufficient.

**No new identifier is required.** `booking_id` is the correct, existing,
sufficient coupling key. The gap is a missing *function call* (Part 4), not
a missing identifier.

---

## Part 7. Payment decision table (A–M)

Derived directly from `payment_booking_orchestrator.run_paid_booking`'s
existing (but unwired) implementation, `payment_service.py`'s transition
functions, and the confirmation/document/communication rules above.

| # | Scenario | Payment state | Booking state | Customer-facing | Retry? | Auto-retry? | Capture? | Refund? | Manual recovery? | Documents? | Email? | My Trips? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | Authorization fails | `FAILED` | never attempted | "Payment failed" | Yes (new attempt) | No | No | N/A | No | No | No | Not shown as booked |
| B | Authorization outcome UNKNOWN | `UNKNOWN` | never attempted (§10: never proceed to book against unresolved money) | "We're checking your payment" | No, wait for reconciliation | No — `reconcile_payment` only, provider-driven | No | No | **Yes**, Ops (`reconcile`) | No | No | Visible, `PENDING_VERIFICATION` |
| C | Authorized, revalidation fails before issuance | `AUTHORIZED` → release (`cancel_authorization`) | `FAILED` (pre-booking failure phase) | "Couldn't book, nothing charged" | Yes, fresh search | No | No | N/A (never captured) | **Yes** if release doesn't cleanly reach `CANCELLED` | No (no confirmation reaches CONFIRMED) | No | Visible, reflects `FAILED` |
| D | Authorized, price increases outside tolerance | `AUTHORIZED` → release | `RECONFIRM_REQUIRED`/`FAILED` (pre-booking failure phase) | "Price changed, please reconfirm" | **Yes — explicit `requires_customer_reconfirmation=True`** | No | No | N/A | Yes if release fails | No | No | Visible |
| E | Authorized, all required tickets issue | `AUTHORIZED` → `request_capture` → `CAPTURED` | `COMPLETE` | "Booking confirmed" | N/A | N/A | **Yes** | No | No (happy path) | **Yes** — `RECEIPT` | **Yes** | `CONFIRMED` |
| F | Authorized, first required ticket issues, a later required ticket fails definitively | authorization held, **neither captured nor released** | `PARTIAL_FAILURE` | "Some tickets booked, one didn't — we're reviewing" | No | No | No, human decides | No, human decides | **Yes, mandatory** — `mark_reconciliation_required` | No (confirmation stays `PARTIAL_RECOVERY`, never `CONFIRMED`) | Yes — the finalizer sends regardless of confirmation status (`try_finalize`) | Visible, `PARTIAL_RECOVERY`, **never hidden** |
| G | Authorized, provider outcome UNKNOWN after an irreversible request | payment status whatever `request_capture`/`cancel_authorization` left it at (often `UNKNOWN`), `requires_ops_recovery=True` | phase-dependent | "We're verifying your booking" | No | No | Only after reconciliation | Only after reconciliation | **Yes, mandatory** | No, until resolved | No, until resolved | Visible, not silently confirmed |
| H | Optional leg fails, all required legs succeed | same as E | `COMPLETE` (optional-leg failure never sets `stop=True`, `booking_orchestrator.py:396-408`) | "Booking confirmed" (optional leg noted separately) | N/A for required part | N/A | Yes | No | No, unless product decides optional-leg failure needs disclosure | Yes | Yes | `CONFIRMED` |
| I | Capture succeeds | `CAPTURED` | (booking already `COMPLETE`, this is post-hoc) | "Payment complete" | N/A | N/A | done | No | No | Yes (if not already issued) | possibly (see Part 16) | `CONFIRMED` |
| J | Capture outcome UNKNOWN | `UNKNOWN` (set inside `request_capture`) | `COMPLETE` (booking already succeeded — **this is exactly why booking and capture must not be conflated**) | "Booking confirmed, payment verifying" | No | No | Reconciliation only | Reconciliation only | **Yes** | Yes — documents are gated on confirmation status, not payment status (Part 15) | Yes | `CONFIRMED` |
| K | Capture fails definitively | `FAILED` | `COMPLETE` (tickets already issued — **real product/financial risk**, see contradiction §22-3) | "Booking confirmed" (financially exposed) | No | No | No | N/A | **Yes, urgent** | Yes (confirmation already reached CONFIRMED) | Yes | `CONFIRMED` |
| L | Refund succeeds | `REFUNDED`/`PARTIALLY_REFUNDED` | unaffected (booking truth is independent of refund, per the payment model's own design) | "Refunded" | N/A | N/A | N/A | done | No | Yes — `CREDIT_NOTE` | Yes | unaffected |
| M | Refund outcome UNKNOWN | `RECONCILIATION_REQUIRED` (set by `_apply_refund_result`, `payment_service.py:393-437`) | unaffected | "Refund processing" | No | No | N/A | Reconciliation only | **Yes** | No, until resolved | No, until resolved | unaffected |

No scenario above fabricates certainty after an UNKNOWN outcome — every
UNKNOWN/ambiguous row routes to a human-gated reconciliation path that
already exists in the code (`reconcile_payment`, `mark_reconciliation_required`,
Ops recovery routes).

---

## Part 8. Capture policy

**Traced Phase 4/5 design intent, not chosen by preference**:
`run_paid_booking`'s own scenario (a) — capture only after `run.phase is
COMPLETE` (`payment_booking_orchestrator.py:185-195`) — is the *only*
capture trigger implemented anywhere in the payment↔booking coupling
module. There is no partial-capture code path, no capture-before-issuance
code path, anywhere in `payment_booking_orchestrator.py`. **The existing
design was built to support exactly one policy: capture only after every
required leg reaches `CONFIRMED`.**

**PROPOSED CONTRACT (explicitly labeled as proposed, matching existing
design, not new preference)**: Detoura captures the full authorized amount
once, and only once, `run.phase == COMPLETE`. It never captures on partial
success. It never captures before issuance. This is the safest minimum-Beta
policy and it is also the *only* policy the existing code was built to
express — there is no invented alternative here.

**What happens when issuance partially succeeds and money remains only
authorized**: per scenario F (Part 7) — the authorization is neither
captured nor released; `mark_reconciliation_required` is called with
`data={confirmed_items, failed_items, unattempted_items}`
(`payment_booking_orchestrator.py:214-239`); a human (Ops) must decide
whether to capture the value of the confirmed legs only (a **partial
capture**, which `request_capture`'s `amount` parameter already structurally
supports, `payment_service.py:245`, even though `run_paid_booking` itself
never calls it with a partial amount), release the authorization entirely
and treat the confirmed tickets as a Detoura-absorbed cost, or another
resolution — **this decision is deliberately not automated by the existing
code, and this contract does not automate it either.**

---

## Part 9. Price change contract

Traced `PriceTolerance.accepts()` (`models/booking.py:147-171`, default
`(absolute=0.0, percentage=0.0)` = **no increase tolerated by default** —
"no looser hidden default", confirmed at `revalidation.py:149`) and the
full `offer_comparator.py` scenario table:

| Scenario | Automatic continuation? | New confirmation required? | Payment authorization must change? | Booking stops? | Old authorization released? |
|---|---|---|---|---|---|
| Price decreases | **Yes** — disclosed only | No | No (customer pays less or the same; existing architecture doesn't auto-adjust a lower authorization down — worth flagging, see §22) | No | No |
| Small increase within tolerance | **Yes** — disclosed, not blocking (`READY_WITH_MINOR_CHANGE`) | No | **Not automatically — the existing code has no mechanism to bump an already-authorized amount.** This is a real gap (see §22-2). | No | No |
| Increase outside tolerance | No | **Yes** — `USER_RECONFIRMATION_REQUIRED` | Old authorization must be released (Part 7 scenario D) before any new one | **Yes**, until reconfirmed | **Yes** |
| Currency mismatch | No | Yes, effectively a fresh booking attempt | Yes — a different currency cannot reuse the same authorization | **Yes, outright** — checked before price movement is even considered (`revalidation.py:198-209`) | Yes |
| Offer expiry | No | Yes (fresh search) | Old authorization released | **Yes** | Yes |

**The backend must never silently charge more than the user's authorized
commercial truth** — this already holds structurally: `authorize_payment`
authorizes exactly `payment.customer_total` (`payment_service.py:192-196`),
itself copied unmodified from the frozen `CheckoutSnapshot`
(`payment.py:262-264`); there is no code path anywhere that increases an
authorized amount post-hoc. A price increase outside tolerance therefore
**cannot** result in an extra charge — it can only result in stopping the
booking and requiring a fresh checkout (new snapshot, new authorization).
This is a safe property already, not something the coupling slice needs to
add.

---

## Part 10. Multi-ticket outcome model

Aggregate outcome derivation is `JourneyBookingIntent.outcome`
(`models/booking.py:304-320`), **always derived from item states, never
independently asserted**:

| Scenario | Aggregate outcome | Preserved per-leg truth |
|---|---|---|
| All required legs succeed | `BookingPhase.COMPLETE` | every item `CONFIRMED` with its own `provider_order_id` |
| Some required legs fail | `BookingPhase.PARTIAL_FAILURE` | confirmed items keep `CONFIRMED` + order id; failed items keep their specific `BookingState` (`FAILED`/`UNAVAILABLE`/etc.) — never collapsed |
| Required outcome UNKNOWN | item stays `TIMEOUT`/`PROVIDER_FAILURE`; the loop's `stop=True` (since it's a required leg) prevents further issuance attempts (`booking_orchestrator.py:409-429`); aggregate becomes `PARTIAL_FAILURE` or `FAILED` depending on whether any other required leg already confirmed | per-item ambiguity is never resolved to a guess |
| Optional leg fails | `stop` is **not** set (`:409-429` — only `if item.required`) — the run continues attempting remaining legs; a successful required-only outcome is still `COMPLETE` | optional leg keeps its own failed state, visible per-leg |
| Duplicate provider response / "already-created" | Not explicitly modeled in `_issue_item`'s exception handling (no `DuffelOrderError` subtype named for this) — **a gap, not a contract**, see §22-4 |
| Recovery later discovers success | Handled entirely outside the orchestrator, via `services/ticket_operations.py`'s recovery functions (`start_recovery`, `record_recovery_candidate`, `approve_recovery`, `execute_recovery` — explicitly never auto-books a replacement, `ticket_operations.py:696-699,709-714`) — a human-driven correction path, not automatic |

**Provider order IDs and per-leg truth are never collapsed into a fake
journey-level CONFIRMED state** — `can_confirm` requires every *required*
item to be `is_settled` (`booking.py:287-302`), and `JourneyConfirmation`'s
own `ConfirmationStatus.CONFIRMED` is reachable only when
`booking_phase == COMPLETE` (Part 3), which itself requires 100% of
required legs — this already holds.

---

## Part 11. Customer-facing state contract (derived, not adopted wholesale)

The mega-prompt's example names are **not adopted verbatim** — derived
instead from what the existing backend truth actually supports, collapsing
`BookingPhase` × `PaymentStatus` × `ConfirmationStatus` into the minimum
states a consumer screen needs:

| Consumer state | Backend truth | Primary action | Forbidden action | Leave page safely? | My Trips? | Support escalation? |
|---|---|---|---|---|---|---|
| `READY_TO_PAY` | Snapshot frozen, no payment yet | Authorize payment | Confirm booking (no payment exists yet) | Yes | No (not yet a booking attempt) | No |
| `PAYMENT_PROCESSING` | Payment `CREATED`/`REQUIRES_CUSTOMER_ACTION` | Wait / complete 3DS | Confirm booking | With a warning (avoid abandoning mid-3DS) | No | No |
| `PAYMENT_UNKNOWN` | Payment `UNKNOWN` | Wait for reconciliation | Retry payment (could double-authorize) | Yes | Yes, marked pending | Only if prolonged |
| `READY_TO_CONFIRM` | Payment `AUTHORIZED`, booking not yet claimed | One final confirm | Re-authorize payment | Yes | Yes | No |
| `BOOKING_IN_PROGRESS` | `BookingPhase.REVALIDATING`/`ISSUING` | Wait | Re-confirm (claim already taken) | Yes — the process continues server-side | Yes | No |
| `PRICE_CHANGED` | `BookingPhase.RECONFIRM_REQUIRED` | Reconfirm at new price, or abandon | Assume old price still holds | Yes | Yes | No |
| `CONFIRMED` | `ConfirmationStatus.CONFIRMED` | View documents/Travel Pass | — | Yes | Yes | No |
| `RECOVERY_REQUIRED` | `ConfirmationStatus.PARTIAL_RECOVERY` | Wait for Ops / contact support | Assume booking failed or succeeded | Yes | **Yes — must never disappear** | **Yes** |
| `FAILED` | `BookingPhase.FAILED`, no money committed | Retry / new search | — | Yes | Shown as failed, not hidden | Only if repeated |

This is a contract for later UI implementation — no UI copy, no screen
redesign performed here.

---

## Part 12. Your Journey contract

| Field | Classification | Evidence |
|---|---|---|
| Route/legs | IMMUTABLE-SNAPSHOTTED | `SelectedOffer.origin/destination/leg_label` |
| Provider offer IDs | IMMUTABLE-SNAPSHOTTED, never sent to client | `SelectedOffer.offer_id`, module docstring `selection_store.py:1-7` |
| Travel dates | IMMUTABLE-SNAPSHOTTED; REVALIDATED-LATER for equality | `discovered_departure/arrival`; `offer_comparator.py:146-150` |
| Traveler count | IMMUTABLE-SNAPSHOTTED | `SelectedOffer.travelers` |
| Baggage | IMMUTABLE-SNAPSHOTTED; REVALIDATED-LATER, downgrade is BLOCKING | `discovered_baggage_cabin/checked`; `offer_comparator.py:111-129` |
| Fare | IMMUTABLE-SNAPSHOTTED (server's own record); REVALIDATED-LATER | `discovered_amount`; `selection_store.py:19-22` |
| Detoura fee | **Not present on Selection at all** — computed later | `CommercialPricingService.quote`, downstream of selection |
| Payable-now total | **Not present on Selection** — `discovered_total` is supplier-side only | `booking_flow.py:178-190` |
| Estimated accommodation | ESTIMATED-ONLY, always | `SyntheticAccommodationDataProvider`, confirmed permanent (Reality Audit §14) |
| Estimated ground transfer | ESTIMATED-ONLY, always | `SyntheticGroundTransferProvider`, confirmed permanent |
| Currency | IMMUTABLE-SNAPSHOTTED; REVALIDATED-LATER, change is BLOCKING | `offer_comparator.py:78-89` |
| Commercial tier | **Not present on Selection** — chosen later on `BookingRun` | `run.service_tier` |
| Selection expiry | Present, **store-level TTL (20 min)**, separate from the offer's own `discovered_expires_at` | `selection_store.py:39,133` |

**Can current `SelectionStore` represent this contract?** Partially. It
correctly holds the IMMUTABLE-SNAPSHOTTED fields. It does **not** hold
Detoura fee, payable-now total, or commercial tier — these only exist once
a `BookingRun` is created from the selection. **Gap, not a defect**: a
"Your Journey" screen that wants to show a payable-now total *before*
creating a booking intent cannot get one from `SelectionStore` alone; it
would need to call `price_run`-equivalent logic against the selection
directly, or accept that payable-now total only becomes known after
booking-intent creation. **Identified as a gap only — not implemented
here.**

**Durability gap**: `SelectionStore` is in-memory, single-process, 20-minute
TTL (`selection_store.py:14-17,39`). A "Your Journey" screen surviving a
backend restart or exceeding 20 minutes cannot recover its selection. This
is consistent with the Limited Beta Reality Audit's single-worker
deployment finding (§21 of that report) — not a new gap, restated here for
completeness.

---

## Part 13. Checkout contract

**What must be frozen before authorization** — already fully represented on
`CheckoutSnapshot` (`payment.py:214-247`): `service_tier`, the complete
`CommercialQuote` (which itself contains the complete `PriceBreakdown` —
every fare/fee/markup/discount/tax component, `customer_total`, currency),
`booking_id`, `journey_reference`, `revalidation_state`, `expires_at`.

**Verified**: the frontend/client **cannot** determine fare, Detoura fee,
payable total, currency, or commercial-tier economics — every one of these
is server-computed and frozen into the snapshot before the client ever
authorizes payment; `PaymentTransaction.customer_total` is copied from it,
never recomputed or accepted from the client (`payment.py:262-264`).

**Nothing is missing from the current `CheckoutSnapshot` for safe
coupling.** The only near-miss is the absent `selection_id` (Part 6),
which does not block coupling since `booking_id` already suffices as the
join key.

---

## Part 14. My Trips contract

**Traced actual query logic**: `list_my_trips` (`api/me_trips.py:66-75`) →
`list_trip_ids_for_user` (`persistence/accounts.py:209-213`) — **no
`WHERE phase=...` filter exists anywhere in this path.** My Trips already
shows every owned booking regardless of phase — selected-only through
failed, confirmed, or recovery-required. **This existing behavior already
satisfies the mega-prompt's explicit requirement** ("A journey in
RECOVERY_REQUIRED must not disappear merely because it is not CONFIRMED")
— confirmed as already-correct, not a gap to fix.

**When does a journey become visible?** At **booking-intent creation**
time, not confirm, not payment — `owner_user_id` is fixed once, then,
and `_claim_ownership` writes it on every `persist_run` call thereafter
(idempotent). So: a journey becomes visible in My Trips as soon as a
signed-in user creates a booking intent from a selection, **before** any
payment or confirmation happens — matching "selected → checkout started"
visibility from the mega-prompt's own list, already the existing behavior.

**Minimum backend fields the next frontend integration needs** (derivable
from what already exists, no new persistence): `booking_id`, `phase`
(`BookingPhase`), `confirmation.status` (`ConfirmationStatus`, once a
`JourneyConfirmation` exists — it may not yet if `try_finalize` hasn't run),
`payment.status` (via a booking→payment lookup), `recovery_state` (from the
`BookingRecord`), lead traveler name, trip label/route, `created_at`. All
of these are already readable via existing persistence functions; nothing
new needs to be built to expose them, only surfaced in a response DTO in
the coupling slice's own scope (frontend integration itself is a separate
slice, per the Roadmap Reset).

---

## Part 15. Document contract

| Payment/booking state | May a receipt/invoice be issued? | May a credit note be issued? |
|---|---|---|
| `AUTHORIZED` only | **No** — `issue_receipt_or_invoice` requires `captured > CENTS` (`financial_document_service.py:196-201`) | No |
| `CAPTURED` | **Yes**, but only once `ConfirmationStatus.CONFIRMED` is also true — `try_finalize` gates document issuance on confirmation status, not payment status alone (`post_booking_finalizer.py:130-151`) | N/A |
| `CONFIRMED` (booking) + `CAPTURED` (payment) | **Yes** — this is the only state combination that currently triggers automatic issuance | No (nothing to credit yet) |
| `RECOVERY_REQUIRED`/`PARTIAL_RECOVERY` | **No** — the finalizer only issues a receipt/invoice when `confirmation.status is CONFIRMED`; a `PARTIAL_RECOVERY` confirmation issues no document automatically | Only once a human resolves the recovery and any refund is real |
| `REFUNDED` | N/A (document already existed) | **Yes** — `issue_credit_note`, requires `captured > CENTS` and a real `original_document_id` (`financial_document_service.py:283-306`) |
| `PARTIALLY_REFUNDED` | N/A | **Yes**, partial credit note — leaves component breakdown `None` rather than inventing an apportionment (`financial_document_service.py:369-376`) |

**A supplier invoice is never the customer invoice** — structurally
enforced (Absolute Truth Rules, above); this contract does not change that.

---

## Part 16. Email contract

Business events that should produce communication, using the **existing**
trigger mechanism (`post_booking_finalizer._send_communication_safely`,
called for every confirmation record regardless of status):

| Event | Existing trigger | Notes |
|---|---|---|
| Payment authorized | **No existing trigger** — communication is only sent when a `JourneyConfirmation` is created/updated via `try_finalize`, which requires a `booking_phase` to evaluate at all; a bare authorization with no booking attempt yet produces no confirmation record | Gap if "payment authorized" email is desired — not built |
| Booking confirmed | Yes — `try_finalize` → `_send_communication_safely`, after document issuance | Existing |
| Recovery required | Yes — same `try_finalize` path fires regardless of `ConfirmationStatus`, including `PARTIAL_RECOVERY` | Existing, confirmed structurally |
| Refund | **No existing automatic trigger found** — `request_refund`/`_apply_refund_result` never calls `communication_service` | Gap, not built |
| Credit note | Same as refund — no automatic trigger | Gap, not built |
| Password/account events | Out of scope for this contract (account domain, not booking) | — |

**Preserved**: email transport failure never changes booking truth —
verified structurally this slice (Absolute Truth Rules). No production
provider is implemented (Reality Audit §12, unaffected by this slice) —
this contract does not implement one.

---

## Part 17. Ownership / anonymous contract

Traced and confirmed:

- **Authenticated booking**: `owner_user_id = session.user_id` at intent
  creation (`v1.py:974`).
- **Anonymous booking**: `owner_user_id = None`, remains valid, remains
  bookable end to end.
- **First owner claim**: `claim_trip` — idempotent for the same user
  (returns `True` without re-writing), refuses (returns `False`, never
  overwrites) for a different user (`accounts.py:169-201`).
- **Same-owner retry**: no-op, safe.
- **Cross-user access**: blocked by identical-404 anti-enumeration checks
  on every `me_trips.py` route (`:82-90` and repeated per route).
- **My Trips**: shown per Part 14.
- **Retroactive anonymous claiming**: confirmed **does not exist** — no
  `/claim` route anywhere, `owner_user_id` is fixed once and never
  reassigned. **This contract does not add one** — the mega-prompt's own
  instruction not to "silently implement retroactive anonymous claiming if
  it does not already exist" is honored by leaving this exactly as found.

**How the coupling slice must preserve these**: `run_paid_booking` (once
wired) must receive `run` (already carrying the correct, server-resolved
`owner_user_id`) rather than re-deriving ownership from any
payment-side field — `PaymentTransaction.user_id` already exists and
should be treated as informational/denormalized, never authoritative over
`BookingRun.owner_user_id`.

---

## Part 18. Idempotency contract

| Action | Mechanism | Classification |
|---|---|---|
| Payment authorization | deterministic provider idempotency key `sha256(payment_id:operation:version)` + CAS on `version` | **SAFE** |
| Payment capture | same key derivation + CAS; `AUTHORIZED`-only precondition | **SAFE** |
| Payment refund | client `idempotency_key` UNIQUE constraint + amount validation | **SAFE** |
| Booking confirmation claim | `claim_for_execution`, atomic phase check-and-set under one lock | **SAFE** |
| Provider ticket/order creation | **No Duffel-side idempotency key exists for Order creation** (confirmed absent, `booking_orchestrator.py:528-533`); protection is entirely Detoura's own single-execution claim upstream of it | **SAFE** in current practice (every path to `_issue_item` passes through the claim) but structurally **PARTIAL** — a process crash between the provider call succeeding and the local state write has no independent provider-side dedup, which is exactly why `TIMEOUT`/`PROVIDER_FAILURE` route to human recovery rather than auto-retry |
| Ticket cancellation/change execution | atomic DB claim (`from_state=APPROVED, to_state=EXECUTING`) + stale-claim reclaim after 120s | **SAFE** |
| Document issuance | caller-supplied idempotency key checked before any work | **SAFE** for "no duplicate document"; number-allocation step's own concurrency guard not independently re-verified this slice — flagged, not a blocker |
| Communication (email) send | atomic `INSERT...WHERE NOT EXISTS(SENDING attempt)` + deterministic per-attempt provider key | **SAFE** |
| Trip ownership claim | single transaction combining existence-check + insert | **SAFE** |

**A centralized guard already exists for the one thing the coupling slice
needs most** (booking execution) — `claim_for_execution`. **The coupling
slice must not introduce a second independent execution guard.** It should
call `run_paid_booking` (which itself calls `run_booking`, which itself
calls `claim_for_execution`) rather than re-implementing any claim logic.

---

## Part 19. Failure ownership

| Failure state | Owner |
|---|---|
| Payment `FAILED` (pre-booking) | Customer retry (new checkout) |
| Payment `UNKNOWN` | AUTOMATIC SYSTEM (`reconcile_payment`, provider-driven), escalates to Ops if unresolved |
| Payment `RECONCILIATION_REQUIRED` | OPS |
| Booking `FAILED` (no legs confirmed, no money captured) | Customer retry |
| Booking `RECOVERY_REQUIRED` / Confirmation `PARTIAL_RECOVERY` | **OPS**, mandatory — never automatic |
| `TIMEOUT`/`PROVIDER_FAILURE` (per-leg, uncertain) | OPS (routes toward `RECOVERY_REQUIRED`) — **never automatic retry**, since the outcome is genuinely unknown |
| Capture `UNKNOWN`/`FAILED` after booking `COMPLETE` | OPS, urgent (real financial exposure — Part 7 scenario K) |
| Refund `UNKNOWN` | EXTERNAL PAYMENT RECONCILIATION (`reconcile_payment` against the provider), then OPS if still unresolved |
| Email send `FAILED`/`UNKNOWN` | AUTOMATIC (customer-triggerable resend via `request_resend`), never escalates to booking/payment truth |
| Optional-leg failure | Currently: no explicit owner assigned — the journey still completes; product should decide whether this needs any disclosure/owner at all (flagged, not resolved here) |

---

## Part 20. Minimum API contract for Codex (frontend integration, not implemented)

| Consumer operation | Existing endpoint | Ownership | Response truth | Idempotency | Missing? |
|---|---|---|---|---|---|
| Select journey | none (search response IS the selection surface; `SelectionStore.record` happens server-side during `live_search`) | N/A | real | N/A | No new endpoint needed |
| Load current journey | `GET /api/v1/booking-intents/{id}` (existing) | ownership-checked | real | N/A | No |
| Create checkout | `POST /api/v1/booking-intents` + `POST /api/v1/payments` (existing, two calls) | server-resolved | real | Yes (client + server key) | No |
| Authorize payment | `POST /api/v1/payments/{id}/confirm` (existing) | ownership-checked | real | Yes | No |
| Confirm purchase (the coupling itself) | `POST /api/v1/booking-intents/{id}/confirm` (existing route, **currently payment-blind** — this is exactly what the next slice must fix) | ownership-checked | **currently incomplete** — doesn't check payment state | Yes (`claim_for_execution`) | **Yes — this route's behavior is the missing piece, not a missing route** |
| Poll/retrieve status | `GET /api/v1/booking-intents/{id}` (existing) | ownership-checked | real | N/A | No |
| Load My Trips | `GET /api/v1/me/trips` (existing) | ownership-checked | real, already shows all phases (Part 14) | N/A | No |
| Load trip detail | `GET /api/v1/me/trips/{id}` (existing) | ownership-checked | real | N/A | No |
| Retrieve documents | `GET /api/v1/me/trips/{id}/documents[/{doc_id}]` (existing, metadata only) | ownership-checked | real metadata; **PDF bytes not served** (Reality Audit §11) | N/A | **Yes — download route, already scoped as its own slice** |

**No new API needs to be designed merely for frontend convenience** — every
operation above already has a safe, correct, existing endpoint. The one
real gap (`confirm_booking` not checking payment state) is a **behavior**
gap inside an existing route, not a missing route.

---

## Part 21. Next slice specification: PAYMENT ↔ BOOKING COUPLING

**Not implemented in this task.** Bounded scope for the next slice:

- **Exact integration seam**: `api/v1.py::confirm_booking` — before calling
  `start_confirmation`/`prepare_journey`, look up the booking's payment (by
  `booking_id`), and for the `ALL_IN_ONE` tier route execution through
  `run_paid_booking`/`run_paid_booking_and_finalize` instead of calling
  `start_confirmation` directly and unconditionally. `BASIC`
  (self-service/guided) tier's relationship to payment needs an explicit
  product decision first (does Basic collect payment at all today? — out
  of this contract's scope to decide, flag for product).
- **Payment lookup rule — resolved here, per independent review, not left
  ambiguous**: `post_booking_finalizer._upsert_confirmation` today picks
  `max(payments, key=created_at)` when more than one payment row exists for
  a `booking_id` (e.g. an abandoned/failed first checkout followed by a
  retry). The coupling slice's `confirm_booking`-side lookup must **not**
  blindly reuse "most recent by `created_at`" for the *authorization*
  decision — it must select **the single payment currently in
  `AUTHORIZED` status for this `booking_id`**, and reject
  (`HTTPException(409, ...)`, no booking attempt) if zero or more than one
  such payment exists. Zero `AUTHORIZED` payments means "nothing to
  execute against" (the correct refusal, not a silent no-op); more than one
  is **possible today, not merely a defensive-programming hypothetical** —
  `create_payment` (`api/payments.py:67-104`) enforces no one-active-
  payment-per-booking invariant, so nothing stops a client calling it twice
  for the same `booking_id` with two different `idempotency_key` values,
  producing two separate `AUTHORIZED` rows. This is exactly why the lookup
  must reject rather than assume — this exact scenario (retried checkout
  after an abandoned payment) must be one of the required tests below, not
  just a defensive fallback for a case that can't occur.
- **Minor implementation note to verify before exercising Part 8's partial-
  capture path**: `SandboxPaymentProvider.capabilities()` currently declares
  `supports_partial_capture=False` while its own `capture()` method will in
  fact honor an explicit partial `amount` if one is ever passed
  (`providers/sandbox_payment.py:99,198`) — a latent inconsistency in the
  test double itself, found during independent review. Reconcile this
  (either the capability flag or the implementation) before writing tests
  that rely on partial capture against the sandbox provider specifically.
- **State transitions**: exactly Part 7's decision table — no new states
  invented.
- **Persistence relationship**: `booking_id` as the sole join key (Part 6)
  — no new identifier, no new table.
- **Idempotency**: reuse `claim_for_execution` (already inside
  `run_booking`, itself inside `run_paid_booking`) — do not add a second
  guard (Part 18).
- **Capture/refund policy**: Part 8's proposed contract (capture only on
  `COMPLETE`, never partial-auto, never pre-issuance).
- **UNKNOWN handling**: never proceed past an `UNKNOWN` authorization (Part
  7-B); never auto-resolve an `UNKNOWN` capture/refund (Part 7-G/J/M).
- **Tests required**: real end-to-end (not mocked) coverage of every Part 7
  scenario A–M via `TestClient` + offline Duffel/Stripe test doubles,
  matching this program's own established pattern; specific adversarial
  cases: duplicate confirm-click racing `claim_for_execution`, a payment
  authorized for booking X submitted against booking Y (must be rejected),
  a capture attempted before `COMPLETE` (must be rejected), a refund
  attempted before `CAPTURED` (already rejected, re-verify), an anonymous
  booking's payment (must still work, ownership rules unchanged), **a
  retried checkout leaving two payment rows for one `booking_id` — one
  `FAILED`/`CANCELLED` and one `AUTHORIZED` — must execute against the
  `AUTHORIZED` one and never the stale one** (the multi-payment lookup rule
  above), and **the specific pre-Beta defect named in Part 4 — an
  `ALL_IN_ONE` confirm with zero payment on record — must be rejected
  outright by the coupling slice, and a regression test locking this in is
  mandatory, not optional.**
- **Security/adversarial cases**: re-run this program's own established
  ownership/IDOR adversarial pattern against the new coupling seam
  specifically (payment for one user's booking must never be authorizable/
  capturable against another user's booking_id); confirm no new secret/PII
  exposure in the joined response DTO.
- **Explicit out-of-scope**: frontend integration (separate slice per
  Roadmap Reset), Basic-tier payment policy decision (needs product input
  first), document PDF download (separate slice), production email
  (separate slice), the optional-leg-failure disclosure question (Part 19),
  the price-decrease/minor-increase authorization-adjustment gap (§22-1/2,
  needs its own design decision).

---

## Part 22. Contradictions review

1. **Price decreases/minor increases within tolerance don't adjust the
   authorized amount.** The existing architecture authorizes exactly
   `customer_total` at authorization time and has no mechanism to
   re-authorize a different amount for the same booking without a fresh
   checkout. A price *decrease* discovered during revalidation is
   "disclosed only" (Part 9) — meaning the customer could be charged
   (captured) the *original*, higher, already-authorized amount even though
   the real fare came in lower, **unless capture amount is explicitly set
   to the lower revalidated total** (`request_capture`'s `amount` parameter
   already supports this, `payment_service.py:245`, but `run_paid_booking`
   never passes a non-`None` amount — it captures the full authorization
   unconditionally, Part 7 scenario E). **Impact**: a customer could be
   overcharged relative to the true final fare in a price-decrease
   scenario. **Required resolution in next slice**: `run_paid_booking`'s
   capture call should pass the actual final priced total (post-revalidation)
   as `amount`, not rely on capturing the full original authorization
   blindly.
2. **Small increases within tolerance have no authorization-adjustment
   path at all.** If the tolerance is non-zero (Beta may configure one) and
   a minor increase is accepted "disclosed, not blocking," the customer is
   still only authorized for the *original* amount — capturing the new,
   slightly higher total would exceed what was authorized (Stripe would
   reject an over-capture). **Impact**: either the tolerance-permitted
   increase is silently absorbed by Detoura (capture stays at the original
   authorized amount, Detoura's margin shrinks) or capture fails.
   **Required resolution in next slice**: explicitly decide and document
   which of these two behaviors is intended — this contract does not
   invent an answer, per instruction not to reorder/redesign payment
   behavior casually.
3. **Capture failure after `BookingPhase.COMPLETE`** (Part 7 scenario K) is
   a real financial-exposure state: tickets are already issued
   (irreversible, real cost incurred with the provider) but Detoura's own
   capture failed, meaning **no money was actually collected for a real,
   issued booking**. The existing code correctly flags this as
   `requires_ops_recovery=True`, but there is no automatic escalation
   priority/urgency signal distinguishing this from a routine reconciliation
   case. **Impact**: an Ops operator without additional tooling cannot
   easily distinguish "routine UNKNOWN, no rush" from "money never
   collected for issued tickets, urgent." **Required resolution**: the
   coupling slice or a follow-on Ops-visibility improvement should surface
   this specific combination (booking `COMPLETE` + payment not `CAPTURED`)
   with elevated priority.
4. **Duplicate/already-created provider order responses are not explicitly
   modeled.** `_issue_item`'s exception handling has no case for "the
   provider says this order already exists" (Part 10) — if a retry ever
   reaches the provider a second time for the same logical leg (which
   `claim_for_execution` should prevent, but the *provider-side* lack of an
   idempotency key means a network-level retry inside `RetryingHttpClient`
   pre-`_issue_item` could theoretically double-submit before the
   application-level claim even engages, if retried at the wrong layer).
   **Impact**: a genuinely rare (already-mitigated at the transport layer
   per Slice 1's own `retry=False` fix for exactly these mutating calls)
   but not fully impossible scenario. **Required resolution**: the coupling
   slice should explicitly re-verify (not merely assume) that
   `create_test_order`'s transport-level retry remains disabled for the
   code path `run_paid_booking` would actually exercise — this was fixed
   in the Network/SSRF slice for the general case; re-confirm it still
   applies once this exact call path becomes live.
5. **My Trips already correctly shows all phases (Part 14)** — checked
   explicitly for contradiction with the "must not hide RECOVERY_REQUIRED"
   requirement, and **none found**; documented here as a confirmed
   non-contradiction, not a gap.
6. **The mirror question to #5 was missed in this document's first draft,
   and is the most severe finding in it, per independent review: can My
   Trips/the confirmation email show `CONFIRMED` for a booking nobody paid
   for?** Traced end to end: **yes, today** — see Part 4's dedicated
   subsection above for the exact mechanism
   (`evaluate_confirmation_eligibility` treats "no payment record exists at
   all" as `payment_paid=True` vacuously, since nothing is "outstanding"
   against a debt of zero). **Impact**: a real customer can receive a real
   "your booking is confirmed" email and see `CONFIRMED` in My Trips for an
   `ALL_IN_ONE` booking that was never paid for — the single most severe
   consequence of the payment↔booking disconnect this whole document
   exists to describe. **Required resolution**: named explicitly as a
   **pre-Beta blocker** (Part 4), not filed as ordinary next-slice
   backlog — either gate `ALL_IN_ONE` traffic entirely until the coupling
   slice ships, or add a minimal interim guard rejecting `confirm_booking`
   for an `ALL_IN_ONE` booking with no `AUTHORIZED`/`CAPTURED` payment on
   record. This document does not choose between those two options
   (an implementation decision, out of scope for a documentation-only
   slice) but does not permit shipping `ALL_IN_ONE` to real users while
   this remains open.

No other contradictions were found between the payment state model,
booking state model, checkout snapshot, commercial pricing, document
rules, My Trips states, and recovery model — the domains are individually
well-designed and mutually consistent everywhere except the six points
above, all of which trace back to the same root cause: the coupling
between payment and booking was never built, so the edge cases *at* that
coupling were never worked through either.

---

## Part 23. Independent review

A fresh, adversarial reviewer (separate from this document's author)
independently re-derived roughly 40 of this document's code claims against
the actual source — including all 5 verbatim enum listings (`PaymentStatus`
13 members, `BookingState` 15 members, `ConfirmationStatus` 5 members,
`CommunicationStatus` 5 members, `BookingPhase` 10 members), the capture-
gating precondition, the CAS-on-`version` mechanism, `customer_total`'s
non-client-suppliability, `claim_for_execution`'s atomicity,
`JourneyBookingIntent.outcome`'s derivation, the required-vs-optional
`stop=True` logic, My Trips' absent phase filter, document-issuance
gating, and the structural email/booking decoupling — and found **no
factual misrepresentation in any of them**.

**Round 1 verdict: REJECTED**, on two grounds, neither a factual error:

1. The document correctly described the mechanics of the payment↔booking
   disconnect (Part 4) but did not explicitly name its single most severe
   consequence: traced end to end by the reviewer, **an `ALL_IN_ONE`
   booking can reach a customer-facing `CONFIRMED` status and a real
   "booking confirmed" email today, with zero payment ever created**
   (`evaluate_confirmation_eligibility` treats an empty payment list as
   "nothing was owed" and marks it vacuously paid). The reviewer also
   independently confirmed Part 22's contradictions #1 (price-decrease
   overcharge risk) and #3 (capture-failure-after-COMPLETE financial
   exposure) are real and accurately, not excessively, described.
2. Part 21's proposed "look up the booking's payment by `booking_id`" seam
   did not specify what happens when more than one payment row exists for
   one booking (a retried checkout after an abandoned/failed attempt) —
   an implementer could pick an unsafe resolution (e.g. blindly taking the
   newest row) without the spec ruling it out.

**Fixes made**: Part 4 gained an explicit subsection tracing the exact
live-defect mechanism and classifying it as a **pre-Beta blocker** (not
ordinary next-slice backlog), naming both interim mitigations without
choosing one (documentation-only slice, no implementation decision made);
Part 22 gained a sixth contradiction cross-referencing it as "the most
severe finding" in the document; Part 21 gained an explicit payment-lookup
rule (select the single `AUTHORIZED` payment for the `booking_id`; reject
with 409 on zero or more than one, never guess) plus two new mandatory
tests (the retried-checkout/two-payment-rows scenario, and a regression
test locking in the Part-4 defect's rejection once the coupling slice
ships); a minor `SandboxPaymentProvider.capabilities()`/`capture()`
partial-capture inconsistency the reviewer also found was noted for
reconciliation before Part 8's partial-capture path is exercised.

**Round 2 (the same reviewer, re-reading the edits fresh): APPROVED.**
Confirmed all three edits accurately and completely close both findings
without overstating or softening either one, and without the document
choosing an implementation it isn't positioned to choose. One trivial,
non-blocking wording nit was found (Part 21 claimed a second concurrent
`AUTHORIZED` payment "should be structurally impossible" — the reviewer
independently re-checked `create_payment` and found it enforces no
one-active-payment-per-booking invariant, so this is reachable today, not
merely defensive programming) — **fixed** (the wording now states this
plainly rather than understating it; the proposed 409-reject control and
its mandated test coverage were already correct either way, so this did
not require a third review round).

**VERDICT: CONTRACT APPROVED.**
