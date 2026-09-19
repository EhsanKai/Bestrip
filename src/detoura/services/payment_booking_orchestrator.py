"""Booking + Payment coordination (V9 Phase 4 §E/§F/§G).

PAYMENT, BOOKING and RECOVERY are reasoned about independently and never
merged into one state machine (§E). This module is the coordinator, not a
third state machine: it calls :mod:`detoura.services.payment_service` for
every money action and the EXISTING, untouched
:func:`detoura.services.booking_orchestrator.run_booking` for every supplier
action, and decides what one implies for the other.

**Selected strategy (§F): authorization-first.**

    1. revalidate + issue (existing `run_booking`, synchronous, untouched)
       is run only AFTER payment authorization succeeds
    2. capture happens only once every required leg is CONFIRMED
    3. a pre-booking failure (nothing sent to a supplier) releases the
       authorization in full - no captured customer money
    4. a PARTIAL failure (some legs booked, at least one required leg is
       not) is never auto-resolved: the authorized amount is neither
       captured nor released automatically, because both are a guess about
       what the traveler should pay for a trip that did not complete as
       booked. It becomes a payment `RECONCILIATION_REQUIRED` (a human
       decision, exactly like the booking side's own `RECOVERY_REQUIRED`,
       §G) - never a silent "PAYMENT FAILED" or "BOOKING FAILED" label that
       throws away what actually happened.

This is the honest limit of what can be automated safely: Detoura's sandbox
provider (and most real providers) has no way to know, from the payment
side alone, how much of a partially-delivered trip a customer should
actually be charged for - that is a human, product/policy decision, and
§G explicitly forbids guessing it via a speculative refund.

**Reachability (V9 Payment <-> Booking Coupling slice)**: :func:`run_paid_booking`/
:func:`run_paid_booking_and_finalize` remain what they always were - a
"pay and book in one call" entry point, still with no production caller
(the two-step consumer API, ``POST /api/v1/payments`` then
``POST /api/v1/payments/{id}/confirm``, is the one actually reachable from
a real route, and it creates a payment as a step separate from booking
confirmation). The wiring this slice adds is
:func:`resolve_eligible_payment_for_booking` +
:func:`execute_paid_booking`, which reuse this module's own post-
authorization decision tree (``run_booking`` -> capture/release/
reconciliation) against a payment the caller already resolved and
authorized, rather than creating one here. Both entry points share the
same single-execution guarantee and the same capture/release/
reconciliation policy - see :func:`_execute_after_authorization`, the
private function both now delegate to.

**The production integration seam**: ``api/v1.py``'s ``confirm_booking``
resolves the eligible payment (refusing with a 409 - Part 21's payment
lookup rule - before anything is claimed if none exists) and passes it to
``services.booking_flow.start_confirmation``, which claims the run
synchronously (unchanged) and then, in its worker thread, calls
:func:`execute_paid_booking` instead of ``run_booking`` directly - this is
what closes the historical gap where an ALL_IN_ONE booking could reach
``COMPLETE`` with no payment involved at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..models.booking import BookingState
from ..models.commercial import CommercialQuote
from ..models.payment import PaymentStatus, PaymentTransaction
from ..payment_config import PaymentConfig, payment_config
from ..persistence import payments as payment_store
from ..persistence.db import Database
from ..providers.payment_provider import PaymentProvider
from . import payment_service as ps
from .booking_commercial import price_run
from .booking_orchestrator import BookingPhase, BookingRun, run_booking

#: Booking phases in which no supplier obligation was ever created - a
#: pre-booking failure. The authorization is released in full.
_PRE_BOOKING_FAILURE_PHASES = frozenset({
    BookingPhase.FAILED, BookingPhase.RECONFIRM_REQUIRED, BookingPhase.PRICE_INCONSISTENT,
})

#: The one currency/amount rounding tolerance used everywhere money is
#: compared in this module - matches ``payment_service``'s own cents guard.
_CENTS = 0.005


class PaymentEligibilityError(Exception):
    """The ALL_IN_ONE payment gate (V9 Beta Contract Part 21) refused to let
    booking execution proceed. Carries a stable ``.code`` (never a raw
    provider/internal message) so the API layer can map it to a truthful,
    bounded consumer-facing status - the same ``.code``-not-``str(error)``
    discipline already established for provider errors elsewhere in this
    codebase."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def resolve_eligible_payment_for_booking(db: Database, *, run: BookingRun) -> PaymentTransaction:
    """The ALL_IN_ONE payment gate itself: the single payment eligible to
    authorize execution of ``run``, or a :class:`PaymentEligibilityError`.

    Never guesses (V9 Beta Contract Part 21's payment lookup rule):

    * Zero payments, or zero in ``AUTHORIZED`` status -> refused
      (``PAYMENT_REQUIRED``/``PAYMENT_UNRESOLVED``) - booking execution
      must never begin without proof of an authorized payment bound to
      this exact ``booking_id``.
    * More than one payment in ``AUTHORIZED`` status for this ``booking_id``
      -> refused (``PAYMENT_AMBIGUOUS``), never "most recent" or "first
      row" - a real, reachable scenario (``create_payment`` enforces no
      one-active-payment-per-booking invariant, so a retried checkout can
      genuinely leave two authorized payments behind), and picking one
      arbitrarily would let a stale or wrong authorization execute a
      booking silently.
    * The one eligible payment's ``booking_id``/owner/amount/currency are
      re-checked against ``run`` directly from persisted, server-owned
      state - never assumed merely because the query filtered on
      ``booking_id`` already (defense in depth against a future query
      change, and the explicit binding proof Part 6 requires).
    """
    payments = payment_store.list_payments_for_booking(db, run.booking_id)
    if not payments:
        raise PaymentEligibilityError(
            "PAYMENT_REQUIRED",
            "This booking has no payment yet. Authorize a payment for this "
            "booking before confirming.",
        )

    authorized = [p for p in payments if p.status is PaymentStatus.AUTHORIZED]
    if not authorized:
        # Distinguish "still resolving" from "definitely not usable" for the
        # customer-facing message only - the refusal itself is identical
        # either way (never proceed past an unresolved payment).
        if any(p.status is PaymentStatus.UNKNOWN for p in payments):
            raise PaymentEligibilityError(
                "PAYMENT_UNRESOLVED",
                "Your payment is still being verified. Try again shortly, or "
                "contact support if this persists.",
            )
        raise PaymentEligibilityError(
            "PAYMENT_REQUIRED",
            "This booking has no authorized payment. Authorize a payment for "
            "this booking before confirming.",
        )

    if len(authorized) > 1:
        raise PaymentEligibilityError(
            "PAYMENT_AMBIGUOUS",
            "More than one authorized payment exists for this booking. "
            "Contact support before confirming.",
        )

    payment = authorized[0]

    # --- explicit binding proof (Part 6): never trust the query alone ---
    if payment.booking_id != run.booking_id:  # pragma: no cover - defensive, should be unreachable
        raise PaymentEligibilityError(
            "PAYMENT_MISMATCH", "This payment does not belong to this booking.",
        )
    if (
        run.owner_user_id is not None
        and payment.user_id is not None
        and payment.user_id != run.owner_user_id
    ):
        raise PaymentEligibilityError(
            "PAYMENT_MISMATCH", "This payment does not belong to this booking.",
        )
    if run.quote is None:
        raise PaymentEligibilityError(
            "PRICE_CHANGED",
            "This booking has not been priced. Reprice before confirming.",
        )
    if payment.currency != run.quote.currency:
        raise PaymentEligibilityError(
            "PRICE_CHANGED",
            "The authorized payment's currency no longer matches this "
            "booking's commercial quote. Start a new checkout to reconfirm.",
        )
    if abs(payment.customer_total - run.quote.customer_total) > _CENTS:
        # The authorization is stale relative to the current, reconciled
        # commercial truth (confirm_booking's own price-provenance check
        # already ran and passed before this function is ever called, so
        # this specifically means the authorized amount itself does not
        # match - e.g. the quote moved between authorization and this
        # confirm attempt). Part 9: never proceed on a mismatched authorization;
        # a fresh checkout is required, never a silent over/under-capture.
        raise PaymentEligibilityError(
            "PRICE_CHANGED",
            "The authorized amount no longer matches this booking's current "
            "price. Start a new checkout to reconfirm.",
        )

    return payment


@dataclass(slots=True)
class PaidBookingOutcome:
    payment: PaymentTransaction
    booking_phase: BookingPhase
    requires_ops_recovery: bool
    requires_customer_reconfirmation: bool
    summary: str


def run_paid_booking(
    db: Database,
    *,
    run: BookingRun,
    quote: CommercialQuote,
    provider: PaymentProvider,
    provider_name: str,
    idempotency_key: str,
    user_id: str | None = None,
    duffel=None,
    cfg: PaymentConfig | None = None,
    now: datetime | None = None,
) -> PaidBookingOutcome:
    """The whole authorization-first flow for one journey, synchronously.

    Blocking, like ``run_booking`` itself - a caller wanting async behavior
    wraps this call in a thread/task exactly as
    ``booking_flow.start_confirmation`` already does for booking alone. Kept
    synchronous here on purpose: every failure-injection scenario in the
    test suite needs a deterministic, directly-assertable return value, not
    a polled background state.

    **Single-execution guarantee** (V9 Phase 6 Payment Security slice): this
    function calls ``run_booking`` with no pre-claim, so ``run_booking``
    itself takes the atomic single-execution claim
    (``booking_orchestrator.claim_for_execution``) before touching any
    provider - the same guard ``booking_flow.start_confirmation`` takes
    explicitly for its own call. Two concurrent ``run_paid_booking`` calls
    for the same ``run`` therefore still result in exactly one
    ``run_booking`` execution; the loser raises ``ValueError`` from the
    claim before authorizing a second time or touching a supplier. This
    function has no production caller today (see module docstring) - the
    guarantee is proven directly against ``run_booking``/``run_paid_booking``
    in ``tests/test_v9_phase6_payment_security.py``, in advance of this
    module being wired to an endpoint.
    """
    cfg = cfg or payment_config()
    now = now or datetime.now(timezone.utc)

    snapshot = ps.freeze_checkout_snapshot(
        db, booking_id=run.booking_id, journey_reference=run.journey_reference,
        user_id=user_id, service_tier=run.service_tier.value, quote=quote, cfg=cfg, now=now,
    )
    payment, _ = ps.create_payment(
        db, snapshot=snapshot, provider_name=provider_name,
        idempotency_key=idempotency_key, now=now,
    )
    payment = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snapshot, now=now)

    if payment.status is PaymentStatus.REQUIRES_CUSTOMER_ACTION:
        return PaidBookingOutcome(
            payment=payment, booking_phase=BookingPhase.AWAITING_CONFIRMATION,
            requires_ops_recovery=False, requires_customer_reconfirmation=False,
            summary="customer action required before payment can proceed; booking not attempted",
        )
    if payment.status in (PaymentStatus.FAILED, PaymentStatus.UNKNOWN):
        # UNKNOWN here means authorization's OWN outcome is unresolved -
        # never proceed to book against unresolved money (§K). A FAILED
        # authorization means no money is at risk at all; either way,
        # booking is never attempted.
        return PaidBookingOutcome(
            payment=payment, booking_phase=BookingPhase.AWAITING_CONFIRMATION,
            requires_ops_recovery=payment.status is PaymentStatus.UNKNOWN,
            requires_customer_reconfirmation=False,
            summary=(
                "authorization failed; no booking attempted, no money captured"
                if payment.status is PaymentStatus.FAILED else
                "authorization outcome unknown; reconcile before proceeding, no booking attempted"
            ),
        )
    if payment.status is not PaymentStatus.AUTHORIZED:
        # Defensive: any other status here is a programming error, not a
        # product outcome - fail loudly rather than silently booking.
        raise RuntimeError(f"unexpected payment status before booking: {payment.status.value}")

    # --- payment authorized: now, and only now, touch the supplier side ---
    try:
        run_booking(run, duffel=duffel)
    except ValueError:
        # V9 Phase 6 Payment Security: the single-execution claim
        # (`booking_orchestrator.claim_for_execution`) rejected this call -
        # a genuinely concurrent duplicate `run_paid_booking`/
        # `start_confirmation` for the SAME run already claimed it, or this
        # run is not in a claimable phase at all. Exactly one execution
        # still happened (proven in
        # tests/test_v9_phase6_payment_security.py); this call is simply
        # not it.
        #
        # Deliberately does NOT write to the payment here (no
        # `mark_reconciliation_required`/capture/cancel call): `payment` in
        # this function's local scope was read before the race, and the
        # call that DID win the claim is, right now, concurrently working
        # towards its own capture/cancel of this exact row. A write from
        # this losing call - even a well-intentioned "hold for review" one -
        # would compete with the winner's own compare-and-swap for the same
        # payment and could turn a clean win into an unhandled
        # `StaleVersion` for the winner. Re-read only, and describe the
        # authorization truthfully as still-open money-at-risk that this
        # call did not (and must not) resolve, never silently discarded and
        # never retried blindly (§K) - the WINNER's own outcome is what
        # ultimately decides this payment's fate.
        current = ps.store.get_payment(db, payment.payment_id) or payment
        return PaidBookingOutcome(
            payment=current, booking_phase=run.phase, requires_ops_recovery=True,
            requires_customer_reconfirmation=False,
            summary=(
                "payment authorized, but booking execution was already claimed by a "
                "concurrent call for this run; this call made no further payment or "
                "booking state change - check current status before assuming anything "
                "needs recovery, the concurrent call that won the claim owns the outcome"
            ),
        )

    return _execute_after_authorization(db, run=run, payment=payment, provider=provider, now=now)


def _execute_after_authorization(
    db: Database, *, run: BookingRun, payment: PaymentTransaction,
    provider: PaymentProvider, now: datetime,
) -> PaidBookingOutcome:
    """The one post-authorization decision tree (§F/§G), shared by every
    entry point that has already run ``run_booking`` to completion against
    an ``AUTHORIZED`` payment for this exact ``run`` - :func:`run_paid_booking`
    (self-claiming) and :func:`execute_paid_booking` (caller already claimed
    via ``claim_for_execution``, e.g. ``booking_flow.start_confirmation``)
    alike. Never called before ``run_booking`` has actually settled ``run``
    into a terminal (or reconfirm-required) phase."""
    if run.phase is BookingPhase.COMPLETE:
        # The final, server-owned payable amount - recomputed from the
        # REVALIDATED per-leg fares (``price_run`` uses
        # ``supplier_transport_current`` once every leg has one), not the
        # pre-booking estimate the payment was authorized against. A fare
        # that moved down within tolerance must capture the lower true
        # amount (Part 9), one that moved up within tolerance is capped at
        # what was actually authorized - `request_capture`/the payment model
        # itself both refuse to capture above `authorized_amount` regardless,
        # this `min` just avoids relying on that refusal to do the job.
        final = price_run(run, db)
        payable = round(final.quote.customer_total, 2)
        capture_amount = min(payable, payment.authorized_amount)
        captured = ps.request_capture(db, payment=payment, provider=provider, amount=capture_amount, now=now)
        ops_recovery = captured.status not in (PaymentStatus.CAPTURED,)
        return PaidBookingOutcome(
            payment=captured, booking_phase=run.phase,
            requires_ops_recovery=ops_recovery, requires_customer_reconfirmation=False,
            summary=(
                "booking complete, payment captured" if not ops_recovery else
                f"booking complete but capture did not cleanly succeed ({captured.status.value}); needs reconciliation"
            ),
        )

    if run.phase in _PRE_BOOKING_FAILURE_PHASES:
        # Nothing was booked (or a price change means nothing SHOULD have
        # been booked) - release the hold in full. §F item 6.
        released = ps.cancel_authorization(db, payment=payment, provider=provider, now=now)
        needs_reconfirm = run.phase in (BookingPhase.RECONFIRM_REQUIRED, BookingPhase.PRICE_INCONSISTENT)
        return PaidBookingOutcome(
            payment=released, booking_phase=run.phase,
            requires_ops_recovery=released.status not in (PaymentStatus.CANCELLED,),
            requires_customer_reconfirmation=needs_reconfirm,
            summary=(
                "booking could not proceed before any supplier commitment; "
                + ("authorization released, no money captured"
                   if released.status is PaymentStatus.CANCELLED
                   else f"authorization release did not cleanly succeed ({released.status.value}); needs reconciliation")
            ),
        )

    if run.phase is BookingPhase.PARTIAL_FAILURE:
        # §G, the hardest case: some legs are real, paid-for supplier
        # obligations; at least one required leg is not. Capturing the full
        # amount would charge for a trip that did not happen as booked;
        # cancelling the authorization would leave Detoura exposed for the
        # legs it DID successfully commit to. Neither is decided here - a
        # human resolves it, with the full truth (which legs, how much
        # authorized, still-cancellable authorization) in front of them.
        held = ps.mark_reconciliation_required(
            db, payment=payment,
            detail="partial booking failure - capture/release decision requires Ops review",
            data={
                "confirmed_items": [i.item_id for i in run.items if i.state is BookingState.CONFIRMED],
                "failed_items": [i.item_id for i in run.items if i.state is BookingState.FAILED],
                "unattempted_items": [i.item_id for i in run.items if i.state is BookingState.NOT_ATTEMPTED],
            },
            now=now,
        )
        return PaidBookingOutcome(
            payment=held, booking_phase=run.phase,
            requires_ops_recovery=True, requires_customer_reconfirmation=False,
            summary=(
                "partial booking failure: some legs confirmed, some did not - "
                "authorization held pending an explicit Ops capture/release decision"
            ),
        )

    # Any other phase (e.g. GUIDED_BOOKING - not reachable in this
    # orchestrated flow, defensive only) - fail closed rather than guess.
    held = ps.mark_reconciliation_required(
        db, payment=payment,
        detail=f"unexpected booking phase {run.phase.value} after an authorized payment", now=now,
    )
    return PaidBookingOutcome(
        payment=held, booking_phase=run.phase, requires_ops_recovery=True,
        requires_customer_reconfirmation=False,
        summary=f"unexpected booking phase {run.phase.value} after an authorized payment; needs Ops review",
    )


def execute_paid_booking(
    db: Database, *, run: BookingRun, payment: PaymentTransaction,
    provider: PaymentProvider, duffel=None, now: datetime | None = None,
) -> PaidBookingOutcome:
    """The ALL_IN_ONE production integration seam (V9 Payment <-> Booking
    Coupling slice): run ``run_booking`` against a payment the caller has
    ALREADY resolved (:func:`resolve_eligible_payment_for_booking`) and
    already claimed for execution (``booking_orchestrator.claim_for_execution``
    - ``run.phase`` must already be ``REVALIDATING``, exactly the contract
    ``run_booking(..., already_claimed=True)`` itself verifies), then run the
    same capture/release/reconciliation decision tree
    :func:`run_paid_booking` uses.

    This is the function ``api/v1.py``'s ``confirm_booking`` /
    ``services/booking_flow.start_confirmation`` call for every non-BASIC
    (ALL_IN_ONE) run, from the synchronous request thread's `already_claimed`
    contract onward - closing the historical gap where `start_confirmation`
    called `run_booking` with no payment involved at all. `payment` itself
    must already be `AUTHORIZED`; resolving it is the caller's job
    (`resolve_eligible_payment_for_booking`), done BEFORE the claim so an
    ineligible payment never even reaches the claim, let alone a supplier.

    **Re-reads ``payment`` fresh from ``db`` before touching the supplier**
    (independent review finding): the object the caller hands in was read
    in the request thread, before ``booking_flow.start_confirmation``
    scheduled a worker thread - a real window (thread scheduling delay,
    this run's own revalidation pacing) in which an Ops cancel/capture, a
    webhook-driven reconciliation, or another process could change this
    exact payment's status. Re-fetching immediately before
    ``run_booking`` closes that gap, and the fresh row (not the caller's
    stale one) is what ``_execute_after_authorization`` then acts on, so
    its own compare-and-swap writes start from the real current version
    instead of racing a known-stale one.
    """
    now = now or datetime.now(timezone.utc)
    current = ps.store.get_payment(db, payment.payment_id) or payment
    if current.status is not PaymentStatus.AUTHORIZED:
        # Defensive AND a real guard now: the caller's job
        # (resolve_eligible_payment_for_booking) is to never hand this
        # function anything but an AUTHORIZED payment, but this is no
        # longer just trusting that - it is re-verified against the
        # current row, immediately before any supplier is touched.
        raise RuntimeError(f"unexpected payment status before booking: {current.status.value}")

    run_booking(run, duffel=duffel, already_claimed=True)
    return _execute_after_authorization(db, run=run, payment=current, provider=provider, now=now)


def run_paid_booking_and_finalize(db: Database, **kwargs) -> PaidBookingOutcome:
    """V9 Phase 5 integration seam, added ADDITIVELY - :func:`run_paid_booking`
    itself (Phase 4, closed/approved) is untouched above. This is the literal
    ``BookingPaymentOutcome -> Eligibility evaluation`` starting point the
    Phase 5 post-booking finalizer pipeline is drawn from: once payment,
    booking and recovery have all been reasoned about (whatever
    ``run_paid_booking`` decided), run the finalizer against the durable
    state it just wrote (the booking record + payment transaction), so a
    caller of this combined entry point gets confirmation/document/
    communication handling for free, without this module needing to import
    anything from those domains at its own top level. A finalizer failure
    is swallowed here exactly as it is at every other trigger point - it
    must never turn this function's own, already-decided outcome into
    something else."""
    outcome = run_paid_booking(db, **kwargs)
    try:
        from .post_booking_finalizer import try_finalize

        try_finalize(db, booking_id=outcome.payment.booking_id)
    except Exception:
        pass
    return outcome
