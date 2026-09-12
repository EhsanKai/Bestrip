"""Journey confirmation domain (V9 Phase 5 §A).

A **confirmation** is Detoura's own durable statement of what the traveller
actually got. It is not a booking, not a payment, and not a receipt - it is
the single record that says "this journey stands, in this shape, as of this
moment", and everything the traveller is later shown (receipt, invoice,
post-booking email) is derived from it rather than re-derived independently
from provider chatter.

Three separations this module exists to enforce:

**1. Confirmation is derived from booking truth + payment truth, never
re-computed.** A confirmation references a booking by ``booking_id`` and a
payment by ``payment_id``. It stores a *snapshot* of the booking phase and
payment status as they stood at eligibility-evaluation time - a frozen
answer to "why did we say this", not a live join that silently rewrites
history when the underlying row moves. The durable truth stays in
``persistence/bookings.py`` (``BookingRecord.phase``) and
``models/payment.py`` (``PaymentStatus``); this module never re-derives it.

**2. Recovery and indeterminacy are never conflated with success.** A
partially-booked journey, an unresolved provider timeout, or a payment whose
real state is unknown are all *not confirmed*. They get their own states
(:attr:`ConfirmationStatus.PARTIAL_RECOVERY`,
:attr:`ConfirmationStatus.PENDING_VERIFICATION`), and it is structurally
impossible for :func:`evaluate_confirmation_eligibility` to answer
``CONFIRMED`` from any of them. There is deliberately **no** ``is_success``
boolean anywhere on :class:`JourneyConfirmation`: a boolean invites callers
to collapse five materially different outcomes into two, which is exactly
the bug this domain exists to prevent. ``status`` is the only truth.

**3. Confirmation never touches the optimizer.** Nothing here imports the
Market Prior, opportunity scoring, beam search or the candidate funnel. What
a traveller is told they have must come from booking and payment truth, never
from an estimate that was only ever meant to rank candidates (§B/§X.3). This
is asserted statically by the Phase 5 domain tests, not merely promised here.

**Money**: this module holds no amounts. Amounts live in the payment
transaction and the economics ledger, in integer minor units, and are read
from there - duplicating a total here would create a second number that can
disagree with the first.

**PII**: ``party_size`` and ``lead_name`` only, matching ``BookingRecord``'s
own minimalism. Never a date of birth, passport/document number, phone
number or address.

**Travel pass**: :class:`~detoura.models.travel_pass.DetouraTravelPass` is
not a persisted entity - it is regenerated on demand from a ``BookingRun``
via ``build_travel_pass()``. So the "travel pass reference" for a
confirmation is simply its ``booking_id``; there is no separate pass id and
no pass table.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


# ======================================================================
# Booking-phase and payment-status vocabularies (decoupled by design)
# ======================================================================
# These mirror ``services.booking_orchestrator.BookingPhase`` and
# ``models.payment.PaymentStatus`` as plain strings ON PURPOSE:
#
#   * importing a *service* module from a *model* module would invert the
#     layering, and
#   * the confirmation domain must stay usable (and testable) without
#     dragging in the whole payment/booking machinery.
#
# The cost of duplication is drift, so drift is made a test failure instead
# of a silent divergence: ``tests/test_v9_phase5_confirmation_domain.py``
# asserts every constant below still matches the real enum member it
# mirrors, and that no enum member has been added without being classified
# here. Change one side without the other and the suite goes red.

PHASE_AWAITING_TRAVELERS = "awaiting_travelers"
PHASE_AWAITING_CONFIRMATION = "awaiting_confirmation"
PHASE_REVALIDATING = "revalidating"
PHASE_RECONFIRM_REQUIRED = "reconfirm_required"
PHASE_ISSUING = "issuing"
PHASE_COMPLETE = "complete"
PHASE_PARTIAL_FAILURE = "partial_failure"
PHASE_FAILED = "failed"
PHASE_GUIDED_BOOKING = "guided_booking"
PHASE_PRICE_INCONSISTENT = "price_inconsistent"

#: Phases in which Detoura has not yet committed the traveller to anything -
#: no provider order exists and none was attempted. A booking sitting here
#: has nothing to confirm, so no confirmation record is created at all.
#: ``price_inconsistent`` belongs here: it is a *refusal* to proceed, a
#: truthful stop before commitment, not a failed attempt.
PRE_COMMITMENT_PHASES = frozenset({
    PHASE_AWAITING_TRAVELERS,
    PHASE_AWAITING_CONFIRMATION,
    PHASE_REVALIDATING,
    PHASE_PRICE_INCONSISTENT,
})

#: Phases where a commitment exists but its outcome is not yet provable.
#: ``guided_booking`` is here rather than anywhere near "confirmed": in the
#: Basic/guided flow Detoura creates no order of its own, so it has no
#: evidence the tickets exist and must never assert that they do.
UNPROVEN_PHASES = frozenset({
    PHASE_ISSUING,
    PHASE_RECONFIRM_REQUIRED,
    PHASE_GUIDED_BOOKING,
})

# --- payment vocabulary (mirrors models.payment; drift-guarded by tests) ---

#: Money genuinely collected. A journey may be CONFIRMED only against one of
#: these. ``PARTIALLY_REFUNDED`` counts as collected - money *was* taken and
#: the journey *was* delivered; a later refund is a subsequent lifecycle
#: event that moves the confirmation on via ``.with_status()``, not evidence
#: that the booking never happened.
PAID_PAYMENT_STATUSES = frozenset({
    "CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED",
})

#: The provider's real answer is not known. NEVER treated as paid, never
#: treated as failed, and never a route to CONFIRMED (§K/§P). A human or a
#: reconciliation run resolves these; nothing here guesses.
INDETERMINATE_PAYMENT_STATUSES = frozenset({
    "UNKNOWN", "RECONCILIATION_REQUIRED",
})

#: Real customer money is, or may be, committed. Used for the one case where
#: a *failed* booking still needs a record: money moved and the journey did
#: not, which is precisely the situation that must never disappear quietly.
MONEY_COMMITTED_PAYMENT_STATUSES = frozenset({
    "AUTHORIZED", "CAPTURE_PENDING", "CAPTURED", "CANCEL_PENDING",
    "REFUND_PENDING", "PARTIALLY_REFUNDED", "UNKNOWN",
    "RECONCILIATION_REQUIRED",
})


# ======================================================================
# State machine
# ======================================================================
class ConfirmationStatus(str, Enum):
    """What Detoura is currently willing to tell the traveller they have.

    Deliberately five states, not a boolean. Terminal states (no outgoing
    transitions, see :data:`ALLOWED_TRANSITIONS`): ``CANCELLED`` and
    ``SUPERSEDED``.
    """

    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    """A commitment exists but its outcome is not yet provable - tickets are
    still issuing, the traveller books them themselves (guided flow), or the
    payment's real state is UNKNOWN / awaiting reconciliation. Not a
    success and not a failure: the honest "we are still checking". This is
    the only entry state that is neither a success nor a recovery case."""

    CONFIRMED = "CONFIRMED"
    """Every required item is booked AND either no payment was required or
    money was genuinely collected. The only state that means "you have this"."""

    PARTIAL_RECOVERY = "PARTIAL_RECOVERY"
    """Some of the journey exists and some does not, or money moved while the
    journey did not complete. A person has to resolve it. Never a success -
    reachable from CONFIRMED too, because an airline can cancel a leg after
    the fact and that must be able to demote a confirmation."""

    CANCELLED = "CANCELLED"
    """The journey is off. Terminal: a cancelled confirmation is never
    revived - a re-booked journey is a new booking with a new confirmation."""

    SUPERSEDED = "SUPERSEDED"
    """Replaced by a newer confirmation (the journey was changed/re-issued
    under a different booking). Terminal, and kept distinct from CANCELLED so
    "the traveller's trip was called off" is never confused with "the record
    they hold has been reissued"."""


#: Data, not scattered conditionals - so "can this confirmation become X?"
#: has exactly one answer a test can enumerate exhaustively. Mirrors
#: ``models.payment.ALLOWED_TRANSITIONS`` and ``models.booking``.
ALLOWED_TRANSITIONS: dict[ConfirmationStatus, frozenset[ConfirmationStatus]] = {
    ConfirmationStatus.PENDING_VERIFICATION: frozenset({
        # Verification resolved in Detoura's favour (reconciliation proved the
        # capture, the last ticket issued, the guided traveller reported back).
        ConfirmationStatus.CONFIRMED,
        # ...or against it.
        ConfirmationStatus.PARTIAL_RECOVERY,
        ConfirmationStatus.CANCELLED,
        ConfirmationStatus.SUPERSEDED,
    }),
    ConfirmationStatus.CONFIRMED: frozenset({
        # A leg the airline later cancels demotes a live confirmation. This
        # edge is the reason CONFIRMED is not terminal.
        ConfirmationStatus.PARTIAL_RECOVERY,
        ConfirmationStatus.CANCELLED,
        ConfirmationStatus.SUPERSEDED,
    }),
    ConfirmationStatus.PARTIAL_RECOVERY: frozenset({
        # Ops re-booked the missing leg: the journey really is whole again,
        # and the record must be able to say so.
        ConfirmationStatus.CONFIRMED,
        ConfirmationStatus.CANCELLED,
        ConfirmationStatus.SUPERSEDED,
    }),
    ConfirmationStatus.CANCELLED: frozenset(),
    ConfirmationStatus.SUPERSEDED: frozenset(),
}

#: Dead ends - no transition out, by anything.
TERMINAL_STATUSES = frozenset({
    ConfirmationStatus.CANCELLED, ConfirmationStatus.SUPERSEDED,
})

#: States in which the journey's outcome is settled and ``finalized_at`` is
#: stamped. ``PARTIAL_RECOVERY`` and ``PENDING_VERIFICATION`` are explicitly
#: NOT settled - something is still owed to the traveller.
FINALIZED_STATUSES = frozenset({
    ConfirmationStatus.CONFIRMED,
    ConfirmationStatus.CANCELLED,
    ConfirmationStatus.SUPERSEDED,
})


class InvalidConfirmationTransition(ValueError):
    """A confirmation state change the domain forbids. Raised, never merely
    logged - the thing this guards against is telling a traveller they hold a
    journey they do not hold."""


class NotConfirmable(Exception):
    """No confirmation record should exist for this booking at all.

    Raised only by :func:`require_confirmation_status`, the strict wrapper.
    :func:`evaluate_confirmation_eligibility` itself returns ``None`` for the
    same case, so a caller can branch without exception handling.
    """


def can_transition_confirmation(
    current: ConfirmationStatus, target: ConfirmationStatus,
) -> bool:
    if current == target:
        return True  # idempotent no-op, never an error (a re-run is not a bug)
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


# ======================================================================
# Eligibility - the one place "is this journey confirmed?" is decided
# ======================================================================
def _normalize_phase(booking_phase: str | None) -> str:
    return (booking_phase or "").strip().lower()


def _normalize_payment_status(payment_status: str | None) -> str | None:
    normalized = (payment_status or "").strip().upper()
    return normalized or None


def evaluate_confirmation_eligibility(
    *,
    booking_phase: str | None,
    payment_status: str | None = None,
    has_payment: bool = True,
    recovery_state: str | None = None,
) -> ConfirmationStatus | None:
    """Decide what, if anything, a booking's confirmation record should say.

    Pure: no database, no clock, no provider. Inputs are the booking phase
    (a ``BookingRecord.phase`` / ``BookingPhase`` value) and the payment
    status (a ``PaymentStatus`` value) as they stand right now; the answer is
    the :class:`ConfirmationStatus` to record, or ``None`` meaning **create
    no confirmation record at all**.

    ``has_payment=False`` means no payment was ever required (anonymous or
    demo flows). Supplying a ``payment_status`` while claiming
    ``has_payment=False`` is contradictory input and is resolved the safe
    way - the payment is treated as real and must prove itself.

    ``recovery_state`` is ``persistence.bookings.BookingRecord.recovery_state``
    - "" means healthy, anything else (``PRICE_CHANGED``,
    ``PARTIAL_FAILURE``, ``RECOVERY_REQUIRED``, ``UNAVAILABLE``,
    ``CANCELLATION_FAILED``, ``CHANGE_REQUIRES_ACTION``, ``FAILED``) is an
    explicit Ops/ticket-operations flag that a real, unresolved problem
    exists - set independently of ``booking_phase``, which can stay
    ``"complete"`` even after a *post*-confirmation failure (e.g. a
    cancellation attempt that itself failed). A booking's phase and
    payment status alone are not the whole truth once travel-servicing
    operations have begun (V9 Phase 5 QA finding #2: RECOVERY_REQUIRED
    must never read as CONFIRMED, and previously did, because this
    function never looked at ``recovery_state`` at all).

    The rules, in order:

    1. Nothing was ever committed (pre-commitment phase, no money moved) ->
       ``None``. There is nothing to confirm and nothing to recover.
    2. Booking ``failed``:
       - with money committed -> ``PARTIAL_RECOVERY``. Money moved and the
         journey did not; this must never vanish.
       - **before any commitment** -> ``None``. No success confirmation, and
         no record at all.
    3. Money committed against a pre-commitment phase -> ``PARTIAL_RECOVERY``
       (a charge with no journey behind it is an ops problem, not a nothing).
    4. An explicit ``recovery_state`` flag -> ``PARTIAL_RECOVERY``,
       unconditionally, regardless of what the phase/payment otherwise say.
       Checked before the partial-failure/indeterminate-payment/CONFIRMED
       rules below so nothing after it can ever produce ``CONFIRMED`` for a
       flagged booking (same discipline as rule 5's ordering).
    5. Booking ``partial_failure`` -> ``PARTIAL_RECOVERY``, never ``CONFIRMED``.
    6. Payment ``UNKNOWN`` / ``RECONCILIATION_REQUIRED``, or a payment claimed
       with no status at all -> ``PENDING_VERIFICATION``. Never ``CONFIRMED``.
    7. Booking ``complete`` AND (no payment required OR money genuinely
       collected) AND no recovery flag -> ``CONFIRMED``. This is the only
       path to ``CONFIRMED``.
    8. Anything else that got this far - still issuing, awaiting reconfirm,
       guided booking, authorized-but-not-captured, or a phase string this
       code has never heard of -> ``PENDING_VERIFICATION``. An unrecognised
       phase fails closed into a visible pending record rather than into
       ``CONFIRMED`` or into silence.
    """
    phase = _normalize_phase(booking_phase)
    status = _normalize_payment_status(payment_status)
    has_recovery_flag = bool((recovery_state or "").strip())

    # Contradictory input fails closed: a named status means a real payment.
    payment_required = has_payment or status is not None

    if not payment_required:
        payment_indeterminate = False
        payment_paid = True  # nothing was owed, so nothing is outstanding
        money_committed = False
    else:
        payment_indeterminate = (
            status is None or status in INDETERMINATE_PAYMENT_STATUSES
        )
        payment_paid = (
            not payment_indeterminate and status in PAID_PAYMENT_STATUSES
        )
        money_committed = status in MONEY_COMMITTED_PAYMENT_STATUSES

    # (2) A booking that failed. Whether a record exists at all depends
    # entirely on whether the traveller's money is involved.
    if phase == PHASE_FAILED:
        return ConfirmationStatus.PARTIAL_RECOVERY if money_committed else None

    # (1) + (3) Nothing committed yet.
    if phase in PRE_COMMITMENT_PHASES:
        return ConfirmationStatus.PARTIAL_RECOVERY if money_committed else None

    # (4) An explicit Ops/ticket-operations recovery flag overrides
    # everything below, unconditionally - a phase of "complete" does not
    # mean nothing went wrong AFTER confirmation (see the docstring).
    # Checked before partial-failure/indeterminate-payment/CONFIRMED so no
    # future reordering of those rules can ever let a flagged booking read
    # as CONFIRMED.
    if has_recovery_flag:
        return ConfirmationStatus.PARTIAL_RECOVERY

    # (5) Partially booked is never a success, regardless of the payment.
    if phase == PHASE_PARTIAL_FAILURE:
        return ConfirmationStatus.PARTIAL_RECOVERY

    # (6) Indeterminate money can never produce a success. Checked BEFORE the
    # CONFIRMED rule so no ordering change can ever let UNKNOWN through.
    if payment_indeterminate:
        return ConfirmationStatus.PENDING_VERIFICATION

    # (7) The only route to CONFIRMED.
    if phase == PHASE_COMPLETE and payment_paid:
        return ConfirmationStatus.CONFIRMED

    # (8) Committed, not yet provable - including unrecognised phases.
    return ConfirmationStatus.PENDING_VERIFICATION


def require_confirmation_status(
    *,
    booking_phase: str | None,
    payment_status: str | None = None,
    has_payment: bool = True,
    recovery_state: str | None = None,
) -> ConfirmationStatus:
    """:func:`evaluate_confirmation_eligibility`, but raising
    :class:`NotConfirmable` instead of returning ``None`` - for callers that
    have already decided a record must exist and want the contradiction to be
    loud rather than a ``None`` sliding into a constructor."""
    status = evaluate_confirmation_eligibility(
        booking_phase=booking_phase,
        payment_status=payment_status,
        has_payment=has_payment,
        recovery_state=recovery_state,
    )
    if status is None:
        raise NotConfirmable(
            f"booking phase {booking_phase!r} with payment status "
            f"{payment_status!r} warrants no confirmation record"
        )
    return status


# ======================================================================
# Records
# ======================================================================
class JourneyConfirmation(BaseModel):
    """Detoura's durable statement about one journey. One row per booking.

    Frozen. The identity fields (``confirmation_id``, ``booking_id``, and
    therefore ``journey_id``) are fixed at creation and cannot change; only
    ``status``, ``finalized_at`` and ``version`` evolve, and only through
    :meth:`with_status`, which validates the transition first.

    Note what is *absent*: no ``is_success``/``is_confirmed``/``ok`` boolean,
    and no boolean field at all. Five outcomes do not collapse into two, and
    a caller must not be able to shortcut past the state semantics.
    """

    model_config = ConfigDict(frozen=True)

    confirmation_id: str = Field(min_length=1, max_length=64)
    booking_id: str = Field(min_length=1, max_length=200)
    """The journey's identity, everywhere in this codebase. Also the handle
    used to regenerate the traveller's ``DetouraTravelPass`` on demand - see
    the module docstring; there is no separate pass id."""
    journey_reference: str = Field(min_length=1, max_length=200)
    user_id: str | None = None
    status: ConfirmationStatus
    service_tier: str = Field(default="", max_length=40)

    booking_phase: str = Field(min_length=1, max_length=64)
    """Snapshot of ``BookingRecord.phase`` at eligibility-evaluation time.
    Deliberately a copy, not a live join: this records *why this status was
    decided*, and must not silently rewrite itself when the booking moves."""
    payment_id: str | None = None
    """Reference to the payment transaction, if any. Amounts are read from
    there - never duplicated here."""
    payment_status: str | None = None
    """Snapshot of ``PaymentStatus.value`` at the same moment, or ``None``
    when no payment was required."""

    party_size: int = Field(default=1, ge=1, le=50)
    lead_name: str = Field(default="", max_length=200)
    """The only traveller PII kept here, matching ``BookingRecord``. Never a
    date of birth, document number, phone number or address."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finalized_at: datetime | None = None
    """When the journey's outcome became settled (see
    :data:`FINALIZED_STATUSES`). ``None`` while still pending or in recovery."""
    version: int = Field(default=1, ge=1)
    """Optimistic-concurrency token - a write must supply the version it
    read, or it is rejected (compare-and-swap, never read-then-write)."""

    @property
    def journey_id(self) -> str:
        """The journey identifier. Deliberately a read-only alias of
        ``booking_id`` rather than a second stored column: two columns that
        are supposed to hold the same value are two columns that can one day
        disagree."""
        return self.booking_id

    def with_status(
        self, target: ConfirmationStatus, *, now: datetime | None = None,
    ) -> "JourneyConfirmation":
        """Return a copy at ``target``, with the version bumped and
        ``finalized_at`` stamped on first reaching a settled outcome.

        Raises :class:`InvalidConfirmationTransition` if the domain forbids
        the move. A same-status call is a legal no-op (a replayed finalizer
        run is not an error) but still bumps the version, so a
        compare-and-swap over it behaves like any other write.
        """
        if not can_transition_confirmation(self.status, target):
            raise InvalidConfirmationTransition(
                f"{self.confirmation_id}: {self.status.value} -> "
                f"{target.value} is not allowed"
            )
        moment = now or datetime.now(timezone.utc)
        finalized_at = self.finalized_at
        if target in FINALIZED_STATUSES and finalized_at is None:
            finalized_at = moment
        return self.model_copy(update={
            "status": target,
            "finalized_at": finalized_at,
            "version": self.version + 1,
        })

    @property
    def is_terminal(self) -> bool:
        """State-machine dead end - not a success signal. Deliberately named
        for the state machine, not for the outcome."""
        return self.status in TERMINAL_STATUSES


class ConfirmationEvent(BaseModel):
    """One row of the append-only confirmation ledger.

    Never updated, never deleted - the history of how a confirmation reached
    its current status, so the current-state row can be audited (or rebuilt)
    if it is ever in doubt. Mirrors ``models.payment.PaymentEvent``.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=64)
    confirmation_id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=64)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    detail: str = Field(default="", max_length=500)
    data: dict = Field(default_factory=dict)
    """Structured, non-sensitive detail only - status transitions, phase
    snapshots, ops actor ids. Never traveller PII, never a payment
    credential."""
