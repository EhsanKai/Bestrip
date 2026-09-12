"""Payment domain (V9 Phase 4 §A/§D).

CUSTOMER PAYMENT != SUPPLIER BOOKING, and PAYMENT STATE != BOOKING STATE.
This module is the payment half only; :mod:`detoura.models.booking` remains
the booking half, untouched. The two are coordinated, never merged - see
:mod:`detoura.services.payment_booking_orchestrator`.

Mirrors the explicit-transition-table style of ``models/booking.py``
deliberately: the same "can this become CAPTURED?" question needs exactly
one, testable answer, and impossible transitions must fail closed rather
than be silently accepted.

**Money truth**: nothing here computes a customer price. Every amount
traces back to a :class:`~detoura.models.commercial.CommercialQuote` frozen
into a :class:`CheckoutSnapshot` before payment begins (§C) - never
recomputed by payment code, never satisfied by a Bootstrap Market Prior or
an optimizer estimate (§B, §X.3). Persisted amounts are always integer minor
units (``to_minor_units``/``from_minor_units``, :mod:`detoura.models.money`)
- never a float column.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .commercial import CommercialQuote


class PaymentStatus(str, Enum):
    """Where one payment transaction currently stands.

    Terminal states (no outgoing transitions, see ``ALLOWED_TRANSITIONS``):
    ``CAPTURED``, ``FAILED``, ``CANCELLED``, ``REFUNDED``. ``PARTIALLY_REFUNDED``
    is deliberately NOT terminal - a partial refund can still be topped up to
    a full refund. ``RECONCILIATION_REQUIRED`` is a holding state a human
    resolves, never auto-cleared (§P).
    """

    CREATED = "CREATED"
    """A transaction row exists; no provider call has been made yet."""
    REQUIRES_CUSTOMER_ACTION = "REQUIRES_CUSTOMER_ACTION"
    """SCA/3DS or an equivalent customer step is outstanding (§N). Not a
    failure - the customer has not yet been asked, or has not yet answered."""
    AUTHORIZED = "AUTHORIZED"
    CAPTURE_PENDING = "CAPTURE_PENDING"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REFUND_PENDING = "REFUND_PENDING"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    UNKNOWN = "UNKNOWN"
    """A provider call's outcome could not be determined (timeout, dropped
    connection) - never converted to FAILED or to success by assumption
    (§K). Resolved only by retrieving provider truth (reconciliation)."""
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    """Local and provider truth disagree, or UNKNOWN could not be resolved
    automatically. A human decides; nothing here auto-corrects it (§P)."""


#: Same pattern as ``models.booking.ALLOWED_TRANSITIONS`` - data, not
#: scattered conditionals, so "can this transition happen" has one answer a
#: test can enumerate exhaustively.
ALLOWED_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset({
        PaymentStatus.REQUIRES_CUSTOMER_ACTION, PaymentStatus.AUTHORIZED,
        PaymentStatus.FAILED, PaymentStatus.UNKNOWN,
        # A payment that was created but never confirmed/authorized (the
        # customer abandoned checkout, or Ops explicitly voids it) is
        # cancellable directly - nothing was ever sent to a provider, so
        # there is nothing to release, only a local state to close. This
        # is exactly what `payment_service.cancel_authorization`'s own
        # guard already assumes is legal; it belongs here too, or that
        # guard's promise is false and the call crashes instead.
        PaymentStatus.CANCELLED,
    }),
    PaymentStatus.REQUIRES_CUSTOMER_ACTION: frozenset({
        PaymentStatus.AUTHORIZED, PaymentStatus.FAILED, PaymentStatus.UNKNOWN,
        PaymentStatus.CANCELLED,
    }),
    PaymentStatus.AUTHORIZED: frozenset({
        PaymentStatus.CAPTURE_PENDING, PaymentStatus.CAPTURED,
        PaymentStatus.CANCEL_PENDING, PaymentStatus.CANCELLED,
        PaymentStatus.UNKNOWN, PaymentStatus.RECONCILIATION_REQUIRED,
    }),
    PaymentStatus.CAPTURE_PENDING: frozenset({
        PaymentStatus.CAPTURED, PaymentStatus.FAILED, PaymentStatus.UNKNOWN,
        PaymentStatus.RECONCILIATION_REQUIRED,
    }),
    PaymentStatus.CANCEL_PENDING: frozenset({
        PaymentStatus.CANCELLED, PaymentStatus.UNKNOWN,
        PaymentStatus.RECONCILIATION_REQUIRED,
        # A cancel that lost the race to a capture already in flight - the
        # authorization is still live and capturable, not cancelled.
        PaymentStatus.AUTHORIZED,
    }),
    PaymentStatus.CANCELLED: frozenset(),
    PaymentStatus.CAPTURED: frozenset({
        PaymentStatus.REFUND_PENDING, PaymentStatus.PARTIALLY_REFUNDED,
        PaymentStatus.REFUNDED, PaymentStatus.RECONCILIATION_REQUIRED,
    }),
    PaymentStatus.REFUND_PENDING: frozenset({
        PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED,
        PaymentStatus.CAPTURED,  # the refund attempt failed; still fully captured
        PaymentStatus.UNKNOWN, PaymentStatus.RECONCILIATION_REQUIRED,
    }),
    PaymentStatus.PARTIALLY_REFUNDED: frozenset({
        PaymentStatus.REFUND_PENDING, PaymentStatus.REFUNDED,
        PaymentStatus.RECONCILIATION_REQUIRED,
    }),
    PaymentStatus.REFUNDED: frozenset(),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.UNKNOWN: frozenset({
        # UNKNOWN only ever resolves through reconciliation discovering the
        # provider's real state - never a blind retry (§K).
        PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED, PaymentStatus.FAILED,
        PaymentStatus.CANCELLED, PaymentStatus.RECONCILIATION_REQUIRED,
    }),
    PaymentStatus.RECONCILIATION_REQUIRED: frozenset({
        # A human resolves a reconciliation finding by recording what is
        # actually true, in any direction reconciliation can prove.
        PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED, PaymentStatus.FAILED,
        PaymentStatus.CANCELLED, PaymentStatus.REFUNDED,
        PaymentStatus.PARTIALLY_REFUNDED,
    }),
}

#: A payment in one of these states will never transition again, by
#: anything - the true dead ends of the state machine. Note ``CAPTURED`` is
#: NOT here: it is a successful, settled outcome, but a captured payment can
#: still move to a refund state, so it is not a dead end.
TERMINAL_STATUSES = frozenset({
    PaymentStatus.CANCELLED, PaymentStatus.FAILED, PaymentStatus.REFUNDED,
})

#: Successful, money-collected outcomes - not necessarily state-machine
#: terminal (CAPTURED can still be refunded), but the "this went right"
#: set Ops/reporting cares about.
SETTLED_STATUSES = frozenset({PaymentStatus.CAPTURED, PaymentStatus.REFUNDED})

#: States in which real customer money is or may be committed - the set
#: reconciliation and Ops treat as "this needs to be right".
MONEY_AT_RISK_STATUSES = frozenset({
    PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURE_PENDING,
    PaymentStatus.CAPTURED, PaymentStatus.CANCEL_PENDING,
    PaymentStatus.REFUND_PENDING, PaymentStatus.PARTIALLY_REFUNDED,
    PaymentStatus.UNKNOWN, PaymentStatus.RECONCILIATION_REQUIRED,
})


class InvalidPaymentTransition(ValueError):
    """A payment state change the domain forbids. Raised, never merely
    logged - the transition this guards against is money moving twice."""


def can_transition_payment(current: PaymentStatus, target: PaymentStatus) -> bool:
    if current == target:
        return True  # idempotent no-op, never an error (§J)
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


class RefundStatus(str, Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


REFUND_TRANSITIONS: dict[RefundStatus, frozenset[RefundStatus]] = {
    RefundStatus.PENDING: frozenset({
        RefundStatus.SUCCEEDED, RefundStatus.FAILED, RefundStatus.UNKNOWN,
    }),
    RefundStatus.UNKNOWN: frozenset({
        RefundStatus.SUCCEEDED, RefundStatus.FAILED,
    }),
    RefundStatus.SUCCEEDED: frozenset(),
    RefundStatus.FAILED: frozenset(),
}


def can_transition_refund(current: RefundStatus, target: RefundStatus) -> bool:
    if current == target:
        return True
    return target in REFUND_TRANSITIONS.get(current, frozenset())


class AllocationComponent(str, Enum):
    """What one slice of a captured payment corresponds to (§H). Kept
    distinguishable so supplier cost is never confused with Detoura's own
    revenue - the exact separation §B requires."""

    SUPPLIER_COST = "SUPPLIER_COST"
    DETOURA_MARKUP = "DETOURA_MARKUP"
    DETOURA_SERVICE_FEE = "DETOURA_SERVICE_FEE"
    DISCOUNT = "DISCOUNT"
    TAX = "TAX"


class ReconciliationClassification(str, Enum):
    SAFE_TO_SYNC = "SAFE_TO_SYNC"
    """Local state is stale but the correct resolution is unambiguous
    (e.g. local CAPTURE_PENDING, provider CAPTURED - just record it)."""
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    """A human should look, but nothing dangerous has necessarily happened."""
    CRITICAL = "CRITICAL"
    """Customer money may be at risk of being wrong (captured with no
    completed booking, a refund the provider does not confirm, etc.)."""


class CheckoutSnapshot(BaseModel):
    """The immutable price/identity freeze a payment attempt refers to (§C).

    Written once, at the moment payment begins, and never updated. If the
    commercial quote expires or a revalidation changes the payable amount,
    a NEW snapshot is created (or none is, and the customer is asked to
    reconfirm) - this row itself never changes underneath a live payment.
    """

    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(min_length=1, max_length=64)
    booking_id: str = Field(min_length=1, max_length=200)
    journey_reference: str = Field(min_length=1, max_length=200)
    user_id: str | None = None
    service_tier: str
    quote: CommercialQuote
    revalidation_state: dict = Field(default_factory=dict)
    """Whatever the last revalidation found (item availability/price deltas)
    at the moment of freezing - display/audit only, never re-derived here."""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime

    @property
    def currency(self) -> str:
        return self.quote.currency

    @property
    def customer_total(self) -> float:
        return self.quote.customer_total

    def is_expired(self, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at


class PaymentTransaction(BaseModel):
    """One customer payment attempt/lifecycle. Provider-neutral - nothing
    here is a Stripe (or any other provider's) object."""

    model_config = ConfigDict(frozen=True)

    payment_id: str = Field(min_length=1, max_length=64)
    journey_reference: str = Field(min_length=1, max_length=200)
    booking_id: str = Field(min_length=1, max_length=200)
    user_id: str | None = None
    checkout_snapshot_id: str = Field(min_length=1, max_length=64)
    currency: str = Field(min_length=3, max_length=3)
    customer_total: float = Field(ge=0.0)
    """The amount this payment is FOR - always copied from the checkout
    snapshot's quote, never client-supplied and never recomputed here."""
    status: PaymentStatus = PaymentStatus.CREATED
    provider: str = Field(min_length=1, max_length=40)
    provider_payment_reference: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=200)
    authorized_amount: float = Field(default=0.0, ge=0.0)
    captured_amount: float = Field(default=0.0, ge=0.0)
    refunded_amount: float = Field(default=0.0, ge=0.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = Field(default=1, ge=1)
    """Optimistic-concurrency token - a write must supply the version it
    read, or it is rejected (compare-and-swap, never read-then-write)."""

    @model_validator(mode="after")
    def _check_amounts(self) -> "PaymentTransaction":
        if self.captured_amount - self.authorized_amount > 0.005 and self.authorized_amount > 0:
            raise ValueError("captured_amount exceeds authorized_amount")
        if self.refunded_amount - self.captured_amount > 0.005:
            raise ValueError("refunded_amount exceeds captured_amount")
        return self

    def with_status(self, target: PaymentStatus, *, now: datetime | None = None) -> "PaymentTransaction":
        if not can_transition_payment(self.status, target):
            raise InvalidPaymentTransition(
                f"{self.payment_id}: {self.status.value} -> {target.value} is not allowed"
            )
        return self.model_copy(update={
            "status": target, "updated_at": now or datetime.now(timezone.utc),
            "version": self.version + 1,
        })

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def fully_refunded(self) -> bool:
        return self.captured_amount > 0 and abs(self.refunded_amount - self.captured_amount) < 0.005


class Refund(BaseModel):
    model_config = ConfigDict(frozen=True)

    refund_id: str = Field(min_length=1, max_length=64)
    payment_id: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0.0)
    currency: str = Field(min_length=3, max_length=3)
    status: RefundStatus = RefundStatus.PENDING
    reason: str = Field(default="", max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=200)
    provider_refund_reference: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = Field(default=1, ge=1)

    def with_status(self, target: RefundStatus, *, now: datetime | None = None) -> "Refund":
        if not can_transition_refund(self.status, target):
            raise InvalidPaymentTransition(
                f"{self.refund_id}: {self.status.value} -> {target.value} is not allowed"
            )
        return self.model_copy(update={
            "status": target, "updated_at": now or datetime.now(timezone.utc),
            "version": self.version + 1,
        })


class PaymentEvent(BaseModel):
    """One row of the append-only financial ledger (§Q)."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=64)
    payment_id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=64)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    amount: float | None = None
    detail: str = Field(default="", max_length=500)
    data: dict = Field(default_factory=dict)
    """Structured, non-sensitive detail only - never a card number, CVC, or
    full payment credential (§Q, §Y)."""


class PaymentAllocation(BaseModel):
    """One slice of what a payment economically covers (§H)."""

    model_config = ConfigDict(frozen=True)

    allocation_id: str = Field(min_length=1, max_length=64)
    payment_id: str = Field(min_length=1, max_length=64)
    component: AllocationComponent
    label: str = Field(default="", max_length=200)
    amount: float = Field(ge=0.0)
    currency: str = Field(min_length=3, max_length=3)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReconciliationFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: str = Field(min_length=1, max_length=64)
    payment_id: str = Field(min_length=1, max_length=64)
    local_status: str
    provider_status: str
    classification: ReconciliationClassification
    detail: str = Field(default="", max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved: bool = False
    resolved_at: datetime | None = None
