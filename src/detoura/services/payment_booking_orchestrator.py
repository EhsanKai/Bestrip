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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..models.booking import BookingState
from ..models.commercial import CommercialQuote
from ..models.payment import PaymentStatus, PaymentTransaction
from ..payment_config import PaymentConfig, payment_config
from ..persistence.db import Database
from ..providers.payment_provider import PaymentProvider
from . import payment_service as ps
from .booking_orchestrator import BookingPhase, BookingRun, run_booking

#: Booking phases in which no supplier obligation was ever created - a
#: pre-booking failure. The authorization is released in full.
_PRE_BOOKING_FAILURE_PHASES = frozenset({
    BookingPhase.FAILED, BookingPhase.RECONFIRM_REQUIRED, BookingPhase.PRICE_INCONSISTENT,
})


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
    run_booking(run, duffel=duffel)

    if run.phase is BookingPhase.COMPLETE:
        captured = ps.request_capture(db, payment=payment, provider=provider, now=now)
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
