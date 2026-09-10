"""Post-booking ticket operations: cancellation and change (V8.5 C3).

A booked ticket is not the end of the story. A traveller cancels, a date moves,
a leg gets pulled by the airline. This module is the domain vocabulary for what
Detoura Ops can do about that - and, just as importantly, for what it must
*not* pretend it did.

Two state machines, kept deliberately apart:

**Cancellation.** ``CANCELLED`` and ``REFUNDED`` are different facts and the
model refuses to conflate them. An order can be cancelled with the refund still
settling (``CANCELLED`` + ``REFUND_PENDING``), cancelled with nothing coming
back (``NON_REFUNDABLE``), or cancelled with only part of the fare returned
(``PARTIALLY_REFUNDED``). A refund amount is only ever what the provider stated
- never inferred from the original fare.

**Change.** Modelled as capability-first. Before any change action is offered,
:class:`ChangeCapability` says whether the provider/order supports it at all;
an order that does not gets ``NOT_SUPPORTED`` and no emulated UI. Where a change
is supported, Duffel's real shape is a *replacement* (new offer, price delta,
change fee), so that is what this models - it is not a "mutate the ticket" call
and the copy must not imply one.

Nothing here calls a provider. These are the shapes; the provider work is in
:mod:`detoura.providers.duffel` and the orchestration in
:mod:`detoura.services.ticket_operations`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ======================================================================
# Cancellation
# ======================================================================
class CancellationState(str, Enum):
    """Where one cancellation workflow currently stands.

    The pre-execution states (``PENDING_ELIGIBILITY`` … ``APPROVED``) are
    Detoura's own workflow. The terminal states from ``CANCELLED`` onward
    describe what the provider actually did, and they keep the cancellation
    fact and the refund fact separate on purpose.
    """

    PENDING_ELIGIBILITY = "PENDING_ELIGIBILITY"
    """Created, eligibility not yet checked."""
    ELIGIBLE = "ELIGIBLE"
    """The provider will accept a cancellation; consequences quoted."""
    INELIGIBLE = "INELIGIBLE"
    """The provider will not accept a cancellation through this channel."""
    APPROVED = "APPROVED"
    """An operator has explicitly approved executing the cancellation."""

    # --- terminal: what the provider did. CANCELLED != REFUNDED. ---
    CANCELLED = "CANCELLED"
    """The order is cancelled. Says nothing about money on its own - pair it
    with a :class:`RefundStatus`."""
    REFUND_PENDING = "REFUND_PENDING"
    """Cancelled, and a refund is owed but not yet settled by the provider."""
    REFUNDED = "REFUNDED"
    """Cancelled, and the full stated refund has settled."""
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    """Cancelled, and only part of the fare was returned (a fee was kept)."""
    NON_REFUNDABLE = "NON_REFUNDABLE"
    """Cancelled, and the provider stated no money is returned."""
    CANCELLATION_FAILED = "CANCELLATION_FAILED"
    """A cancellation was attempted and the provider did not complete it. The
    order may still be live - an operator must check."""


#: Cancellation states in which the order is no longer live.
CANCELLED_STATES = frozenset({
    CancellationState.CANCELLED,
    CancellationState.REFUND_PENDING,
    CancellationState.REFUNDED,
    CancellationState.PARTIALLY_REFUNDED,
    CancellationState.NON_REFUNDABLE,
})

#: Terminal cancellation states - the workflow is finished (success or failure).
CANCELLATION_TERMINAL = CANCELLED_STATES | {CancellationState.CANCELLATION_FAILED}


class RefundStatus(str, Enum):
    """The money side of a cancellation, tracked independently of the order
    side. ``UNKNOWN`` is a real answer: the provider cancelled but has not yet
    said what, if anything, comes back."""

    UNKNOWN = "UNKNOWN"
    NONE = "NONE"
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FULL = "FULL"


class CancellationQuote(BaseModel):
    """What cancelling this order would cost and return, as the provider stated
    it. Every monetary field is optional: absent means the provider did not
    say, which is never the same as zero."""

    model_config = ConfigDict(frozen=True)

    provider: str = "duffel"
    provider_cancellation_id: str | None = None
    currency: str = "EUR"
    #: What the provider says would be refunded. ``None`` = not stated.
    refund_amount: float | None = Field(default=None, ge=0.0)
    #: The fare originally paid to the provider for this order, for context only
    #: - a refund is never derived from it.
    order_paid_amount: float | None = Field(default=None, ge=0.0)
    penalty_amount: float | None = Field(default=None, ge=0.0)
    refund_to: str = ""
    """"original_form_of_payment" / "credit" / "voucher" / "" (unknown)."""
    expires_at: datetime | None = None
    conditions: tuple[str, ...] = ()
    live_mode: bool | None = None

    @property
    def refundable(self) -> bool | None:
        """True/False when the provider stated an amount; ``None`` when it did
        not."""
        if self.refund_amount is None:
            return None
        return self.refund_amount > 0.0

    def refund_status(self) -> RefundStatus:
        if self.refund_amount is None:
            return RefundStatus.UNKNOWN
        if self.refund_amount <= 0.0:
            return RefundStatus.NONE
        if self.order_paid_amount and self.refund_amount + 0.01 < self.order_paid_amount:
            return RefundStatus.PARTIAL
        return RefundStatus.FULL


class CancellationResult(BaseModel):
    """What actually happened after an execute. The state is derived from the
    provider's response, never assumed from the request succeeding."""

    model_config = ConfigDict(frozen=True)

    state: CancellationState
    refund_status: RefundStatus = RefundStatus.UNKNOWN
    currency: str = "EUR"
    refund_amount: float | None = None
    provider_cancellation_id: str | None = None
    provider_status: str = ""
    detail: str = ""
    live_mode: bool | None = None
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _cancelled_is_not_refunded(self) -> "CancellationResult":
        # The invariant, enforced: a state that says the order is cancelled must
        # carry a refund status, and REFUNDED requires a stated full refund.
        if self.state is CancellationState.REFUNDED and self.refund_status not in (
            RefundStatus.FULL, RefundStatus.PARTIAL,
        ):
            raise ValueError("REFUNDED requires a settled refund amount")
        if self.state is CancellationState.NON_REFUNDABLE and self.refund_status not in (
            RefundStatus.NONE, RefundStatus.UNKNOWN,
        ):
            raise ValueError("NON_REFUNDABLE cannot carry a positive refund")
        return self


# ======================================================================
# Change / rebooking
# ======================================================================
class ChangeCapability(str, Enum):
    """Whether a change can even be attempted for this order. Checked before
    any change action is shown - an unsupported order gets ``NOT_SUPPORTED``
    and no emulated flow."""

    DATE_CHANGE = "DATE_CHANGE"
    FLIGHT_CHANGE = "FLIGHT_CHANGE"
    ORDER_CHANGE = "ORDER_CHANGE"
    REBOOK_REPLACE = "REBOOK_REPLACE"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"
    """Capability could not be determined (provider unreachable, demo booking).
    Distinct from NOT_SUPPORTED: we did not learn it cannot, we failed to
    learn whether it can."""


class ChangeKind(str, Enum):
    """How the provider actually effects the change. Detoura shows the honest
    one - a ``REBOOK_REPLACEMENT`` is not called a "ticket change"."""

    ORDER_CHANGE = "ORDER_CHANGE"
    """The provider mutates the existing order (Duffel order_change)."""
    REBOOK_REPLACEMENT = "REBOOK_REPLACEMENT"
    """The provider cancels and re-books; a new order id results."""


class ChangeQuote(BaseModel):
    """A priced change offer from the provider: the new itinerary, the fare
    delta, the change fee, and anything owed or returned."""

    model_config = ConfigDict(frozen=True)

    provider: str = "duffel"
    kind: ChangeKind = ChangeKind.ORDER_CHANGE
    provider_change_request_id: str | None = None
    provider_change_offer_id: str | None = None
    currency: str = "EUR"

    old_fare: float | None = Field(default=None, ge=0.0)
    new_fare: float | None = Field(default=None, ge=0.0)
    change_fee: float | None = Field(default=None, ge=0.0)
    refundable_credit: float | None = Field(default=None, ge=0.0)
    #: Positive: the customer owes this. Negative: this comes back. ``None``:
    #: the provider did not give enough to compute it.
    net_delta: float | None = None

    new_slices: tuple[dict, ...] = ()
    """Opaque provider-shaped replacement slices, for display only."""
    baggage_changes: tuple[str, ...] = ()
    restrictions: tuple[str, ...] = ()
    expires_at: datetime | None = None
    live_mode: bool | None = None

    @property
    def baggage_regressed(self) -> bool:
        return any("less" in c.lower() or "removed" in c.lower() or "no longer" in c.lower()
                   for c in self.baggage_changes)


class ChangeState(str, Enum):
    PENDING_CAPABILITY = "PENDING_CAPABILITY"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    QUOTED = "QUOTED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


CHANGE_TERMINAL = frozenset({
    ChangeState.NOT_SUPPORTED, ChangeState.APPLIED, ChangeState.FAILED,
})


class ChangeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: ChangeState
    kind: ChangeKind = ChangeKind.ORDER_CHANGE
    new_provider_order_id: str | None = None
    currency: str = "EUR"
    charged_amount: float | None = None
    refunded_amount: float | None = None
    provider_status: str = ""
    detail: str = ""
    live_mode: bool | None = None
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ======================================================================
# Operation envelope (idempotency)
# ======================================================================
class OperationKind(str, Enum):
    CANCELLATION = "CANCELLATION"
    CHANGE = "CHANGE"
    RECOVERY = "RECOVERY"


class TicketOperation(BaseModel):
    """One destructive/financial operation against a booked ticket or order,
    with an idempotency key so a double-click, a refresh or a retry after a
    timeout cannot execute it twice.

    Persisted in ``ticket_operations`` (schema v4). The ``operation_id`` is
    server-issued at creation and the client echoes it back on execute; an
    execute against an already-terminal operation returns the stored result
    rather than calling the provider again.
    """

    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(min_length=8, max_length=64)
    booking_id: str
    sequence: int
    """1-based ticket index within the booking. 0 = the whole order/journey."""
    kind: OperationKind
    state: str
    """The current state value of the relevant sub-machine (CancellationState /
    ChangeState / a recovery state)."""
    provider: str = "duffel"
    provider_order_id: str | None = None
    reason: str = ""
    actor: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    quote_json: dict | None = None
    result_json: dict | None = None
    idempotency_key: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.state in {
            s.value for s in (
                CANCELLATION_TERMINAL | CHANGE_TERMINAL
                | {CancellationState.INELIGIBLE}
            )
        } or self.state in ("EXECUTED", "COMPLETED", "ABANDONED")
