"""The payment service (V9 Phase 4 §A-§K).

Every mutating operation here is idempotent by construction (§J):
creation dedups on ``idempotency_key`` (a UNIQUE-constraint INSERT, never a
read-then-write race - see ``persistence/payments.py``), and every state
change is a compare-and-swap on ``version``. A provider call whose outcome
cannot be determined (timeout, dropped connection) is recorded as
``UNKNOWN`` and never silently treated as success or failure (§K) - only
:func:`reconcile_payment` (calling the provider's own ``retrieve``) ever
resolves it.

**Money truth** (§B): nothing here computes a customer price. Every
transaction is created from a :class:`CheckoutSnapshot`, whose
``customer_total`` was frozen from a real
:class:`~detoura.models.commercial.CommercialQuote` before this module was
ever called - see :func:`freeze_checkout_snapshot`.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from ..models.booking import BookingState
from ..models.commercial import CommercialQuote
from ..models.payment import (
    AllocationComponent,
    CheckoutSnapshot,
    PaymentAllocation,
    PaymentEvent,
    PaymentStatus,
    PaymentTransaction,
    ReconciliationClassification,
    ReconciliationFinding,
    Refund,
    RefundStatus,
)
from ..observability import log_event
from ..observability import metrics as _metrics
from ..payment_config import PaymentConfig, payment_config
from ..persistence import payments as store
from ..persistence.db import Database
from ..providers.payment_provider import PaymentProvider, ProviderResult

logger = logging.getLogger(__name__)

CENTS = 0.005


#: Internal ``PaymentEvent.event_type`` (already persisted to the
#: ``payment_events`` table) -> the stable application-log event name this
#: baseline emits for it (V9 Limited Beta observability contract §9). Kept
#: as an explicit map rather than a ``.lower()`` transform so the log schema
#: names match the contract exactly, independent of how the persistence
#: layer happens to spell its own event_type strings.
_PAYMENT_LOG_EVENTS: dict[str, str] = {
    "PAYMENT_CREATED": "payment_created",
    "AUTHORIZATION_REQUESTED": "payment_authorization_started",
    "AUTHORIZED": "payment_authorized",
    "REQUIRES_CUSTOMER_ACTION": "payment_requires_customer_action",
    "AUTHORIZATION_FAILED": "payment_failed",
    "RECONCILIATION_REQUIRED": "payment_reconciliation_required",
    "CAPTURE_REQUESTED": "payment_capture_started",
    "CAPTURED": "payment_captured",
    "CAPTURE_FAILED": "payment_capture_failed",
    "AUTHORIZATION_CANCELLED": "payment_authorization_cancelled",
    "REFUND_REQUESTED": "payment_refund_started",
    "REFUND_SUCCEEDED": "payment_refunded",
    "REFUND_FAILED": "payment_refund_failed",
    "RECONCILED": "payment_reconciled",
}


def _log_transition(event_type: str, *, payment_id: str, booking_id: str | None = None) -> None:
    """The one place a payment state transition becomes a structured,
    machine-parseable log event - safe identifiers only (payment_id,
    booking_id), never an amount, card detail, or provider secret (V9
    Limited Beta observability contract §9). ``event_type`` is the SAME
    value already passed to ``store.record_event`` at each call site, so the
    application log and the persisted ``payment_events`` row are never able
    to drift onto different taxonomies."""
    event = _PAYMENT_LOG_EVENTS.get(event_type, event_type.lower())
    log_event(logger, event, payment_id=payment_id, booking_id=booking_id)
    _metrics.observe_payment_transition(event_type=event_type)


def _log_reconciliation_finding(finding: ReconciliationFinding) -> None:
    """A reconciliation finding (V9 Limited Beta observability contract §11:
    "these are especially important for Beta") - classification and status
    strings only, never the provider's raw response the finding was built
    from."""
    log_event(
        logger, "payment_reconciliation_finding",
        payment_id=finding.payment_id, classification=finding.classification.value,
        local_status=finding.local_status, provider_status=finding.provider_status,
    )


class QuoteExpired(Exception):
    """The checkout snapshot has expired - the caller must obtain a fresh
    one and, if the amount changed, ask the customer to reconfirm (§C).
    Never silently charged at the old or a recomputed amount."""


class PaymentNotFound(Exception):
    pass


class InvalidRefundAmount(ValueError):
    pass


def _event_id() -> str:
    return store.new_id("pevt")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ======================================================================
# §C - the immutable checkout snapshot
# ======================================================================
def freeze_checkout_snapshot(
    db: Database, *, booking_id: str, journey_reference: str, user_id: str | None,
    service_tier: str, quote: CommercialQuote, revalidation_state: dict | None = None,
    cfg: PaymentConfig | None = None, now: datetime | None = None,
) -> CheckoutSnapshot:
    """Freeze exactly what a payment will refer to. Never called with a
    recomputed price - ``quote`` must be the same
    :class:`~detoura.models.commercial.CommercialQuote` the booking run
    already priced with (``booking_commercial.price_run``'s result), never
    a fresh computation inside this module."""
    cfg = cfg or payment_config()
    now = now or _now()
    snapshot = CheckoutSnapshot(
        snapshot_id=store.new_id("snap"), booking_id=booking_id,
        journey_reference=journey_reference, user_id=user_id,
        service_tier=service_tier, quote=quote,
        revalidation_state=dict(revalidation_state or {}),
        created_at=now, expires_at=now + timedelta(seconds=cfg.checkout_snapshot_ttl_seconds),
    )
    store.create_snapshot(db, snapshot)
    return snapshot


# ======================================================================
# §A/§J - payment creation
# ======================================================================
def create_payment(
    db: Database, *, snapshot: CheckoutSnapshot, provider_name: str,
    idempotency_key: str, now: datetime | None = None,
) -> tuple[PaymentTransaction, bool]:
    """Create a payment bound to ``snapshot``, or return the existing row
    for a retried ``idempotency_key`` (§J). Refuses an expired snapshot
    (§C) - a client must obtain a fresh one, never charged against a stale
    price."""
    now = now or _now()
    if snapshot.is_expired(now=now):
        raise QuoteExpired(f"checkout snapshot {snapshot.snapshot_id} expired at {snapshot.expires_at}")

    candidate = PaymentTransaction(
        payment_id=store.new_id("pay"), journey_reference=snapshot.journey_reference,
        booking_id=snapshot.booking_id, user_id=snapshot.user_id,
        checkout_snapshot_id=snapshot.snapshot_id, currency=snapshot.currency,
        customer_total=snapshot.customer_total, status=PaymentStatus.CREATED,
        provider=provider_name, idempotency_key=idempotency_key,
        created_at=now, updated_at=now,
    )
    payment, created = store.create_payment(db, payment=candidate)
    if created:
        store.record_event(db, PaymentEvent(
            event_id=_event_id(), payment_id=payment.payment_id,
            event_type="PAYMENT_CREATED", occurred_at=now,
            amount=payment.customer_total,
            detail=f"checkout_snapshot={snapshot.snapshot_id}",
        ))
        _log_transition("PAYMENT_CREATED", payment_id=payment.payment_id, booking_id=payment.booking_id)
        _write_allocations(db, payment=payment, quote=snapshot.quote, now=now)
    return payment, created


def _write_allocations(db: Database, *, payment: PaymentTransaction, quote: CommercialQuote, now: datetime) -> None:
    """§H: record what this payment economically covers, from the SAME
    breakdown the quote already has - never a recomputation."""
    b = quote.breakdown
    rows = []

    def _add(component: AllocationComponent, amount: float, label: str = "") -> None:
        if amount <= 0:
            return
        rows.append(PaymentAllocation(
            allocation_id=store.new_id("alloc"), payment_id=payment.payment_id,
            component=component, label=label, amount=round(amount, 2),
            currency=payment.currency, created_at=now,
        ))

    _add(AllocationComponent.SUPPLIER_COST, b.supplier_total, "supplier fare + baggage + fees")
    _add(AllocationComponent.DETOURA_SERVICE_FEE, b.detoura_service_fee)
    _add(AllocationComponent.DETOURA_MARKUP, b.detoura_markup)
    _add(AllocationComponent.TAX, b.tax)
    _add(AllocationComponent.DISCOUNT, b.discount, quote.promo_code or "")
    store.write_allocations(db, rows)


# ======================================================================
# §D/§F - authorization
# ======================================================================
def _provider_idempotency_key(payment_id: str, operation: str, version: int) -> str:
    """Deterministic per-(payment, operation, version) key so a retried call
    - same process or a fresh one after a restart - always presents the SAME
    key to the provider as long as nothing about the payment's own state has
    actually changed, never a fresh one that could double-execute (§J).

    ``version`` is included deliberately, not just ``payment_id``: if a call
    (e.g. capture) returns UNKNOWN and :func:`reconcile_payment` later proves
    against real provider truth that it never actually executed, that
    reconciliation bumps the payment's version via its own compare-and-swap.
    A subsequent, legitimate retry then naturally gets a *different* key -
    otherwise the sandbox's (and a real provider's) own idempotency-key cache
    would replay the stale UNKNOWN result forever and the payment could never
    be captured/cancelled at all (§X.18: reconciliation must be able to
    recover timeout ambiguity, not just relabel it). Two calls racing at the
    *same* version (a genuine concurrent duplicate, §X.7) still collapse onto
    one provider call, since neither has caused a state transition yet."""
    return hashlib.sha256(f"{payment_id}:{operation}:{version}".encode()).hexdigest()[:40]


def authorize_payment(
    db: Database, *, payment: PaymentTransaction, provider: PaymentProvider,
    snapshot: CheckoutSnapshot | None = None, now: datetime | None = None,
    payment_method: str | None = None,
) -> PaymentTransaction:
    """Idempotent: if ``payment`` is already past CREATED/REQUIRES_CUSTOMER_ACTION,
    returns it unchanged rather than calling the provider again."""
    now = now or _now()
    if payment.status not in (PaymentStatus.CREATED, PaymentStatus.REQUIRES_CUSTOMER_ACTION):
        return payment  # already resolved one way or another - idempotent no-op

    if snapshot is not None and snapshot.is_expired(now=now):
        raise QuoteExpired(f"checkout snapshot {snapshot.snapshot_id} expired before authorization")

    store.record_event(db, PaymentEvent(
        event_id=_event_id(), payment_id=payment.payment_id,
        event_type="AUTHORIZATION_REQUESTED", occurred_at=now, amount=payment.customer_total,
    ))
    _log_transition("AUTHORIZATION_REQUESTED", payment_id=payment.payment_id, booking_id=payment.booking_id)
    idem = _provider_idempotency_key(payment.payment_id, "authorize", payment.version)
    result = provider.authorize(
        idempotency_key=idem, amount=payment.customer_total, currency=payment.currency,
        reference=payment.payment_id, payment_method=payment_method,
    )
    return _apply_authorize_result(db, payment=payment, result=result, now=now)


def _apply_authorize_result(
    db: Database, *, payment: PaymentTransaction, result: ProviderResult, now: datetime,
) -> PaymentTransaction:
    if result.unknown:
        target = PaymentStatus.UNKNOWN
        event_type, detail = "RECONCILIATION_REQUIRED", "authorize outcome unknown (timeout)"
    elif result.ok:
        target = PaymentStatus.AUTHORIZED
        event_type, detail = "AUTHORIZED", result.detail
    elif result.status == "requires_action":
        target = PaymentStatus.REQUIRES_CUSTOMER_ACTION
        event_type, detail = "REQUIRES_CUSTOMER_ACTION", result.detail
    else:
        target = PaymentStatus.FAILED
        event_type, detail = "AUTHORIZATION_FAILED", result.detail

    updated = payment.with_status(target, now=now).model_copy(update={
        "provider_payment_reference": result.provider_reference or payment.provider_payment_reference,
        "authorized_amount": result.authorized_amount or payment.authorized_amount,
    })
    try:
        stored = store.compare_and_swap_payment(db, payment=updated, expected_version=payment.version)
    except store.StaleVersion:
        # A genuinely concurrent authorize_payment call for this SAME
        # payment (both threads read the identical pre-authorize version,
        # both reached the provider - which the idempotency-key cache
        # already collapsed into one real execution, §J) raced on this
        # final write. The loser never double-executed anything; it is
        # simply not the thread whose write landed. Re-read and return the
        # real current state rather than raising for it - a caller of
        # authorize_payment must never see an exception from a race that
        # produced no double authorization at all (§X.7).
        current = store.get_payment(db, payment.payment_id)
        return current if current is not None else payment
    store.record_event(db, PaymentEvent(
        event_id=_event_id(), payment_id=payment.payment_id, event_type=event_type,
        occurred_at=now, amount=result.authorized_amount, detail=detail,
        data={"provider_reference": result.provider_reference} if result.provider_reference else {},
    ))
    _log_transition(event_type, payment_id=payment.payment_id, booking_id=payment.booking_id)
    return stored


# ======================================================================
# §D/§F - capture
# ======================================================================
def request_capture(
    db: Database, *, payment: PaymentTransaction, provider: PaymentProvider,
    amount: float | None = None, now: datetime | None = None,
) -> PaymentTransaction:
    now = now or _now()
    if payment.status == PaymentStatus.CAPTURED:
        return payment  # idempotent no-op (§J, §X.7)
    if payment.status != PaymentStatus.AUTHORIZED:
        raise ValueError(
            f"{payment.payment_id}: cannot capture from status {payment.status.value}"
        )
    if payment.provider_payment_reference is None:
        raise ValueError(f"{payment.payment_id}: no provider reference to capture")

    pending = payment.with_status(PaymentStatus.CAPTURE_PENDING, now=now)
    pending = store.compare_and_swap_payment(db, payment=pending, expected_version=payment.version)
    store.record_event(db, PaymentEvent(
        event_id=_event_id(), payment_id=payment.payment_id, event_type="CAPTURE_REQUESTED",
        occurred_at=now, amount=amount or payment.authorized_amount,
    ))
    _log_transition("CAPTURE_REQUESTED", payment_id=payment.payment_id, booking_id=payment.booking_id)

    idem = _provider_idempotency_key(payment.payment_id, "capture", payment.version)
    result = provider.capture(
        idempotency_key=idem, provider_reference=payment.provider_payment_reference, amount=amount,
    )
    if result.unknown:
        target, event_type, detail = PaymentStatus.UNKNOWN, "RECONCILIATION_REQUIRED", "capture outcome unknown (timeout)"
    elif result.ok:
        target, event_type, detail = PaymentStatus.CAPTURED, "CAPTURED", result.detail
    else:
        target, event_type, detail = PaymentStatus.FAILED, "CAPTURE_FAILED", result.detail

    updated = pending.with_status(target, now=now).model_copy(update={
        "captured_amount": result.captured_amount if result.captured_amount is not None else pending.captured_amount,
    })
    stored = store.compare_and_swap_payment(db, payment=updated, expected_version=pending.version)
    store.record_event(db, PaymentEvent(
        event_id=_event_id(), payment_id=payment.payment_id, event_type=event_type,
        occurred_at=now, amount=result.captured_amount, detail=detail,
    ))
    _log_transition(event_type, payment_id=payment.payment_id, booking_id=payment.booking_id)
    return stored


def cancel_authorization(
    db: Database, *, payment: PaymentTransaction, provider: PaymentProvider, now: datetime | None = None,
) -> PaymentTransaction:
    """§F/§G: release an authorization before capture - the correct
    response to a booking failure that never reached a safe capture
    point."""
    now = now or _now()
    if payment.status == PaymentStatus.CANCELLED:
        return payment
    if payment.status not in (PaymentStatus.AUTHORIZED, PaymentStatus.CREATED, PaymentStatus.REQUIRES_CUSTOMER_ACTION):
        raise ValueError(f"{payment.payment_id}: cannot cancel from status {payment.status.value}")

    if payment.provider_payment_reference is None:
        # Never sent to the provider at all - nothing to release there.
        updated = payment.with_status(PaymentStatus.CANCELLED, now=now)
        stored = store.compare_and_swap_payment(db, payment=updated, expected_version=payment.version)
        store.record_event(db, PaymentEvent(
            event_id=_event_id(), payment_id=payment.payment_id,
            event_type="AUTHORIZATION_CANCELLED", occurred_at=now,
            detail="cancelled before any provider authorization existed",
        ))
        _log_transition("AUTHORIZATION_CANCELLED", payment_id=payment.payment_id, booking_id=payment.booking_id)
        return stored

    pending = payment.with_status(PaymentStatus.CANCEL_PENDING, now=now)
    pending = store.compare_and_swap_payment(db, payment=pending, expected_version=payment.version)
    idem = _provider_idempotency_key(payment.payment_id, "cancel", payment.version)
    result = provider.cancel_authorization(
        idempotency_key=idem, provider_reference=payment.provider_payment_reference,
    )
    if result.unknown:
        target, event_type, detail = PaymentStatus.UNKNOWN, "RECONCILIATION_REQUIRED", "cancel outcome unknown (timeout)"
    elif result.ok:
        target, event_type, detail = PaymentStatus.CANCELLED, "AUTHORIZATION_CANCELLED", result.detail
    elif result.status == "already_captured":
        # Lost the race to a capture already in flight - the authorization
        # is (or will be) captured; go back to AUTHORIZED so the caller
        # re-reads real state rather than believing a cancel that did not
        # happen.
        target, event_type, detail = PaymentStatus.AUTHORIZED, "RECONCILIATION_REQUIRED", (
            "cancel lost a race with capture; authorization is not cancellable"
        )
    else:
        target, event_type, detail = PaymentStatus.RECONCILIATION_REQUIRED, "RECONCILIATION_REQUIRED", result.detail

    updated = pending.with_status(target, now=now)
    stored = store.compare_and_swap_payment(db, payment=updated, expected_version=pending.version)
    store.record_event(db, PaymentEvent(
        event_id=_event_id(), payment_id=payment.payment_id, event_type=event_type,
        occurred_at=now, detail=detail,
    ))
    _log_transition(event_type, payment_id=payment.payment_id, booking_id=payment.booking_id)
    return stored


# ======================================================================
# §I - refunds
# ======================================================================
def request_refund(
    db: Database, *, payment: PaymentTransaction, provider: PaymentProvider,
    amount: float, reason: str, idempotency_key: str, now: datetime | None = None,
) -> tuple[PaymentTransaction, Refund]:
    now = now or _now()
    if payment.status not in (
        PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUND_PENDING,
    ):
        raise ValueError(f"{payment.payment_id}: cannot refund from status {payment.status.value}")
    already_refunded = payment.refunded_amount
    if amount <= 0 or amount - (payment.captured_amount - already_refunded) > CENTS:
        raise InvalidRefundAmount(
            f"refund of {amount} exceeds remaining refundable amount "
            f"({payment.captured_amount - already_refunded})"
        )

    candidate = Refund(
        refund_id=store.new_id("rfnd"), payment_id=payment.payment_id, amount=amount,
        currency=payment.currency, status=RefundStatus.PENDING, reason=reason,
        idempotency_key=idempotency_key, created_at=now, updated_at=now,
    )
    refund, created = store.create_refund(db, refund=candidate)
    if not created:
        # Same idempotency key - the refund this call would have created
        # already exists. Return current state, never create a second one
        # (§I, §J).
        return store.get_payment(db, payment.payment_id) or payment, refund

    if payment.status == PaymentStatus.CAPTURED:
        pending_payment = payment.with_status(PaymentStatus.REFUND_PENDING, now=now)
        pending_payment = store.compare_and_swap_payment(
            db, payment=pending_payment, expected_version=payment.version,
        )
    else:
        pending_payment = payment

    store.record_event(db, PaymentEvent(
        event_id=_event_id(), payment_id=payment.payment_id, event_type="REFUND_REQUESTED",
        occurred_at=now, amount=amount, detail=reason, data={"refund_id": refund.refund_id},
    ))
    _log_transition("REFUND_REQUESTED", payment_id=payment.payment_id, booking_id=payment.booking_id)

    idem = _provider_idempotency_key(refund.refund_id, "refund", refund.version)
    result = provider.refund(
        idempotency_key=idem, provider_reference=payment.provider_payment_reference or "",
        amount=amount, reason=reason,
    )
    return _apply_refund_result(db, payment=pending_payment, refund=refund, result=result, now=now)


def _apply_refund_result(
    db: Database, *, payment: PaymentTransaction, refund: Refund, result: ProviderResult, now: datetime,
) -> tuple[PaymentTransaction, Refund]:
    if result.unknown:
        refund_target, event_type, detail = RefundStatus.UNKNOWN, "RECONCILIATION_REQUIRED", "refund outcome unknown (timeout)"
        payment_target = PaymentStatus.RECONCILIATION_REQUIRED
    elif result.ok:
        refund_target, event_type, detail = RefundStatus.SUCCEEDED, "REFUND_SUCCEEDED", result.detail
        new_refunded = round(payment.refunded_amount + refund.amount, 2)
        # §I: a partial refund must never be labelled full, and vice versa -
        # derived strictly from the arithmetic, never asserted.
        payment_target = (
            PaymentStatus.REFUNDED if abs(new_refunded - payment.captured_amount) < CENTS
            else PaymentStatus.PARTIALLY_REFUNDED
        )
    else:
        refund_target, event_type, detail = RefundStatus.FAILED, "REFUND_FAILED", result.detail
        # The refund attempt failed; the payment's own captured/refunded
        # state has not changed, so it goes back to whatever it truthfully
        # still is.
        payment_target = (
            PaymentStatus.PARTIALLY_REFUNDED if payment.refunded_amount > 0
            else PaymentStatus.CAPTURED
        )

    updated_refund = refund.with_status(refund_target, now=now).model_copy(update={
        "provider_refund_reference": result.provider_reference or refund.provider_refund_reference,
    })
    stored_refund = store.compare_and_swap_refund(db, refund=updated_refund, expected_version=refund.version)

    new_refunded_amount = (
        round(payment.refunded_amount + refund.amount, 2) if refund_target is RefundStatus.SUCCEEDED
        else payment.refunded_amount
    )
    updated_payment = payment.with_status(payment_target, now=now).model_copy(update={
        "refunded_amount": new_refunded_amount,
    })
    stored_payment = store.compare_and_swap_payment(db, payment=updated_payment, expected_version=payment.version)

    store.record_event(db, PaymentEvent(
        event_id=_event_id(), payment_id=payment.payment_id, event_type=event_type,
        occurred_at=now, amount=refund.amount, detail=detail,
        data={"refund_id": refund.refund_id},
    ))
    _log_transition(event_type, payment_id=payment.payment_id, booking_id=payment.booking_id)
    return stored_payment, stored_refund


# ======================================================================
# §G - explicit hold for a human decision (never a silent auto-resolution)
# ======================================================================
def mark_reconciliation_required(
    db: Database, *, payment: PaymentTransaction, detail: str, data: dict | None = None,
    now: datetime | None = None,
) -> PaymentTransaction:
    """Move a payment into the one state that must never resolve itself
    (§G) - used both for provider/local mismatches (§P) and for a partial
    booking failure where capture-vs-release is a human product decision,
    not something either side's state machine can decide alone (§E)."""
    now = now or _now()
    updated = payment.with_status(PaymentStatus.RECONCILIATION_REQUIRED, now=now)
    stored = store.compare_and_swap_payment(db, payment=updated, expected_version=payment.version)
    store.record_event(db, PaymentEvent(
        event_id=_event_id(), payment_id=payment.payment_id,
        event_type="RECONCILIATION_REQUIRED", occurred_at=now, detail=detail, data=data or {},
    ))
    _log_transition("RECONCILIATION_REQUIRED", payment_id=payment.payment_id, booking_id=payment.booking_id)
    return stored


# ======================================================================
# §K/§P - reconciliation
# ======================================================================
def reconcile_payment(
    db: Database, *, payment: PaymentTransaction, provider: PaymentProvider, now: datetime | None = None,
) -> tuple[PaymentTransaction, ReconciliationFinding | None]:
    """The ONLY path that resolves UNKNOWN/RECONCILIATION_REQUIRED - always
    by asking the provider for real truth (never by trusting a caller's
    claim, including an Ops caller's, §P/§U). Safe, unambiguous mismatches
    are synced automatically; anything else becomes a classified finding a
    human reviews."""
    now = now or _now()
    if payment.provider_payment_reference is None:
        finding = ReconciliationFinding(
            finding_id=store.new_id("recf"), payment_id=payment.payment_id,
            local_status=payment.status.value, provider_status="no_reference",
            classification=ReconciliationClassification.REVIEW_REQUIRED,
            detail="no provider reference recorded to reconcile against", created_at=now,
        )
        store.create_finding(db, finding)
        _log_reconciliation_finding(finding)
        return payment, finding

    result = provider.retrieve(provider_reference=payment.provider_payment_reference)
    if result.unknown or not result.ok:
        finding = ReconciliationFinding(
            finding_id=store.new_id("recf"), payment_id=payment.payment_id,
            local_status=payment.status.value, provider_status=result.status,
            classification=ReconciliationClassification.CRITICAL,
            detail=f"provider retrieve itself failed/unknown: {result.detail}", created_at=now,
        )
        store.create_finding(db, finding)
        _log_reconciliation_finding(finding)
        if payment.status is not PaymentStatus.RECONCILIATION_REQUIRED:
            escalated = payment.with_status(PaymentStatus.RECONCILIATION_REQUIRED, now=now)
            payment = store.compare_and_swap_payment(db, payment=escalated, expected_version=payment.version)
        return payment, finding

    resolved_status, classification = _classify(payment.status, result.status)
    if classification is ReconciliationClassification.SAFE_TO_SYNC and resolved_status is not None:
        updated = payment.with_status(resolved_status, now=now).model_copy(update={
            "authorized_amount": result.authorized_amount if result.authorized_amount is not None else payment.authorized_amount,
            "captured_amount": result.captured_amount if result.captured_amount is not None else payment.captured_amount,
            "refunded_amount": result.refunded_amount if result.refunded_amount is not None else payment.refunded_amount,
        })
        stored = store.compare_and_swap_payment(db, payment=updated, expected_version=payment.version)
        store.record_event(db, PaymentEvent(
            event_id=_event_id(), payment_id=payment.payment_id, event_type="RECONCILED",
            occurred_at=now, detail=f"synced to provider truth: {result.status}",
        ))
        _log_transition("RECONCILED", payment_id=payment.payment_id, booking_id=payment.booking_id)
        return stored, None

    finding = ReconciliationFinding(
        finding_id=store.new_id("recf"), payment_id=payment.payment_id,
        local_status=payment.status.value, provider_status=result.status,
        classification=classification, created_at=now,
        detail=f"local={payment.status.value} provider={result.status}",
    )
    store.create_finding(db, finding)
    _log_reconciliation_finding(finding)
    if payment.status is not PaymentStatus.RECONCILIATION_REQUIRED:
        escalated = payment.with_status(PaymentStatus.RECONCILIATION_REQUIRED, now=now)
        payment = store.compare_and_swap_payment(db, payment=escalated, expected_version=payment.version)
    return payment, finding


#: local status -> {provider status -> (resolved local status, classification)}
#: Only combinations that are genuinely unambiguous are SAFE_TO_SYNC -
#: everything else is a finding for a human, never auto-corrected (§P).
_SAFE_SYNC_MAP: dict[PaymentStatus, dict[str, PaymentStatus]] = {
    PaymentStatus.UNKNOWN: {
        "authorized": PaymentStatus.AUTHORIZED,
        "captured": PaymentStatus.CAPTURED,
        "failed": PaymentStatus.FAILED,
        "cancelled": PaymentStatus.CANCELLED,
    },
    PaymentStatus.CAPTURE_PENDING: {"captured": PaymentStatus.CAPTURED},
    PaymentStatus.AUTHORIZED: {"captured": PaymentStatus.CAPTURED},
    PaymentStatus.CANCEL_PENDING: {"cancelled": PaymentStatus.CANCELLED},
    # RECONCILIATION_REQUIRED is deliberately narrow, not absent: an
    # ambiguous (UNKNOWN) refund/cancel attempt on an already-captured
    # payment escalates straight to RECONCILIATION_REQUIRED (§I), and provider
    # truth showing the capture is simply still intact - nothing lost, nothing
    # extra taken - is as safe to sync as any other UNKNOWN resolution.
    # Deliberately excludes "authorized"/"cancelled": those are exactly what
    # a §G partial-booking-failure hold's own provider state looks like (an
    # authorization nothing was ever captured against), and auto-syncing
    # those would silently resolve a hold that is supposed to require an
    # explicit human capture/cancel decision, not a reconciliation sync.
    PaymentStatus.RECONCILIATION_REQUIRED: {
        "captured": PaymentStatus.CAPTURED,
        "refunded": PaymentStatus.REFUNDED,
        "partially_refunded": PaymentStatus.PARTIALLY_REFUNDED,
    },
}


#: provider status string -> the PaymentStatus it represents, for settled/
#: stable states a provider can actually report on `retrieve`. Used only to
#: detect an exact match between local and provider truth (see `_classify`)
#: - never as a general mapping, since "what a fresh transition should be
#: called" and "what an already-true state is called" are different
#: questions (RECONCILIATION_REQUIRED, for instance, has no provider-side
#: equivalent at all - a provider never reports "reconciliation_required").
_PROVIDER_STATUS_ALIASES: dict[str, PaymentStatus] = {
    "authorized": PaymentStatus.AUTHORIZED,
    "captured": PaymentStatus.CAPTURED,
    "failed": PaymentStatus.FAILED,
    "cancelled": PaymentStatus.CANCELLED,
    "refunded": PaymentStatus.REFUNDED,
    "partially_refunded": PaymentStatus.PARTIALLY_REFUNDED,
}


def _classify(
    local: PaymentStatus, provider_status: str,
) -> tuple[PaymentStatus | None, ReconciliationClassification]:
    # An exact match is always safe, for ANY local status: provider truth
    # already equals what we believe, so reconciling is a confirmation, not
    # a correction - the same-status transition is always allowed (§D).
    # Without this, reconciling a perfectly healthy CAPTURED payment whose
    # provider agrees it is "captured" (e.g. a routine Ops health-check, or
    # a stale/out-of-order webhook that merely reports what is already
    # true) was incorrectly flagged REVIEW_REQUIRED and escalated to
    # RECONCILIATION_REQUIRED for no reason - a real bug found by testing
    # exactly this "does an out-of-order webhook ever downgrade a
    # more-advanced state" scenario (§O).
    if _PROVIDER_STATUS_ALIASES.get(provider_status) is local:
        return local, ReconciliationClassification.SAFE_TO_SYNC
    safe_for_local = _SAFE_SYNC_MAP.get(local, {})
    if provider_status in safe_for_local:
        return safe_for_local[provider_status], ReconciliationClassification.SAFE_TO_SYNC
    if local in (PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED) and provider_status == "failed":
        # Local says money was captured, provider says it was not (or no
        # longer is) - money-at-risk mismatch.
        return None, ReconciliationClassification.CRITICAL
    return None, ReconciliationClassification.REVIEW_REQUIRED
