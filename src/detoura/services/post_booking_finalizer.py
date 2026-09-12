"""The post-booking finalizer (V9 Phase 5 §Agent 6 - cross-domain orchestration).

    BookingPaymentOutcome
        v
    Eligibility evaluation      (models.confirmation.evaluate_confirmation_eligibility)
        v
    JourneyConfirmation         (Agent 1: models/persistence.confirmation)
        v
    FinancialDocument issuance  (Agent 2: services.financial_document_service)
        v
    Communication creation/send (Agent 3's domain + this module's own
                                  services.communication_service, since Agent 3
                                  was not asked to build the send orchestration)

**Deterministic, idempotent, restart-safe, concurrency-safe.** ``try_finalize``
reads ONLY durable state - ``persistence.bookings.BookingRecord``,
``persistence.payments.PaymentTransaction``, ``persistence.economics.EconomicsRow``
- never an in-memory ``BookingRun``/``CommercialQuote`` object. This is what
makes it safe to call from multiple trigger points (a booking reaching a
terminal phase, a payment settling after the booking already did, a process
restart replaying the same call) without knowing which one drove the booking
to its current state, and without ever double-issuing a confirmation,
document, or communication - the underlying domain layers each own their own
idempotent-creation guarantee (a UNIQUE constraint, never a lock).

**Do not rebuild commercial truth.** The immutable ``booking_economics``
ledger row is written exactly where it always was
(``services.booking_commercial.finalize_economics``, called from
``booking_flow.py``'s worker and ``api/v1.py``'s poll handler - both
unmodified). This module only reads it.

**EMAIL FAILURE MUST NEVER TURN A SUCCESSFUL BOOKING INTO A FAILED BOOKING**,
and a document-render failure must never either: every step past
confirmation-eligibility is wrapped so its own failure is recorded and
returned, never raised past this function and never allowed to touch the
booking/payment rows this function only reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ..models.confirmation import (
    ConfirmationEvent,
    ConfirmationStatus,
    FINALIZED_STATUSES,
    InvalidConfirmationTransition,
    JourneyConfirmation,
    evaluate_confirmation_eligibility,
)
from ..models.financial_document import FinancialDocument, FinancialDocumentType
from ..models.communication import CustomerCommunication, render_booking_confirmation_email
from ..persistence import accounts as account_store
from ..persistence import bookings as booking_store
from ..persistence import confirmations as confirmation_store
from ..persistence import economics
from ..persistence import payments as payment_store
from ..persistence.db import Database
from . import communication_service
from . import financial_document_service as document_service

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FinalizationOutcome:
    booking_id: str
    ready: bool
    """False only when there is not yet enough durable state to evaluate
    anything (the booking record itself does not exist) - a legitimate
    "try again later", never an error."""
    confirmation: JourneyConfirmation | None
    document: FinancialDocument | None
    communication: CustomerCommunication | None
    document_error: str = ""
    communication_error: str = ""
    summary: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_id() -> str:
    return confirmation_store.new_confirmation_event_id()


def try_finalize(db: Database, *, booking_id: str, now: datetime | None = None) -> FinalizationOutcome:
    """The single entry point every trigger calls. Safe to call as many
    times as anyone likes, from as many threads/processes as exist, for a
    booking at any stage - it always re-derives what SHOULD be true from
    current durable state and only ever moves forward (or holds), never
    fabricates, never double-executes an irreversible step."""
    now = now or _now()
    booking = booking_store.get(db, booking_id)
    if booking is None:
        return FinalizationOutcome(
            booking_id, ready=False, confirmation=None, document=None, communication=None,
            summary="no booking record yet - not ready",
        )

    payments = payment_store.list_payments_for_booking(db, booking_id)
    has_payment = len(payments) > 0
    # The most recently created payment is the relevant one - Phase 4/5 do
    # not model more than one live payment per booking at a time (a
    # superseding payment after a failed one would be a future phase's
    # concern; today's payment API dedupes by booking + idempotency_key).
    payment = max(payments, key=lambda p: p.created_at) if payments else None
    payment_status = payment.status.value if payment is not None else None

    eligibility = evaluate_confirmation_eligibility(
        booking_phase=booking.phase, payment_status=payment_status, has_payment=has_payment,
        recovery_state=booking.recovery_state,
    )
    if eligibility is None:
        # FAILED before any commitment - no confirmation record at all
        # (§ "FAILED before commitment -> no success confirmation").
        return FinalizationOutcome(
            booking_id, ready=True, confirmation=None, document=None, communication=None,
            summary="booking failed before any commitment - no confirmation record created",
        )

    confirmation = _upsert_confirmation(
        db, booking=booking, payment_id=(payment.payment_id if payment else None),
        payment_status=payment_status, eligibility=eligibility, now=now,
    )

    document: FinancialDocument | None = None
    document_error = ""
    communication: CustomerCommunication | None = None
    communication_error = ""

    if confirmation.status is ConfirmationStatus.CONFIRMED:
        document, document_error = _issue_document_safely(db, booking_id=booking_id, now=now)
        if document is not None:
            _record_confirmation_event(
                db, confirmation_id=confirmation.confirmation_id, event_type="FINANCIAL_DOCUMENT_ISSUED",
                occurred_at=now, detail=document.document_number, data={"document_id": document.document_id},
            )

    # A truthful communication is attempted for every confirmation record
    # that exists at all (CONFIRMED and PARTIAL_RECOVERY/PENDING_VERIFICATION
    # alike) - the renderer (Agent 3) is explicitly built to describe a
    # non-CONFIRMED status honestly, never as a success. This never blocks
    # or reverses anything above: a communication failure/UNKNOWN here
    # changes nothing about the confirmation or document already recorded.
    communication, communication_error = _send_communication_safely(
        db, booking=booking, confirmation=confirmation, now=now,
    )

    return FinalizationOutcome(
        booking_id, ready=True, confirmation=confirmation, document=document,
        communication=communication, document_error=document_error,
        communication_error=communication_error,
        summary=f"confirmation={confirmation.status.value}",
    )


def _upsert_confirmation(
    db: Database, *, booking, payment_id: str | None, payment_status: str | None,
    eligibility: ConfirmationStatus, now: datetime,
) -> JourneyConfirmation:
    user_id = account_store.get_trip_owner(db, booking.booking_id)
    candidate = JourneyConfirmation(
        confirmation_id=confirmation_store.new_confirmation_id(), booking_id=booking.booking_id,
        journey_reference=booking.journey_reference, user_id=user_id, status=eligibility,
        service_tier=booking.service_tier, booking_phase=booking.phase, payment_id=payment_id,
        payment_status=payment_status, party_size=booking.party_size, lead_name=booking.lead_name,
        created_at=now,
    )
    confirmation, created = confirmation_store.create_confirmation(db, confirmation=candidate)
    if created:
        _record_confirmation_event(
            db, confirmation_id=confirmation.confirmation_id, event_type="CONFIRMATION_CREATED",
            occurred_at=now, detail=f"eligibility={eligibility.value}",
        )
        if confirmation.status in FINALIZED_STATUSES:
            _record_confirmation_event(
                db, confirmation_id=confirmation.confirmation_id, event_type="CONFIRMATION_FINALIZED",
                occurred_at=now,
            )
        return confirmation

    if confirmation.status == eligibility:
        return confirmation  # idempotent replay, nothing changed

    # Re-evaluation on a later call (a payment settled after the booking
    # did, an airline change demoted a CONFIRMED leg, Ops recovered a
    # missing one) - promote or demote via the SAME validated transition
    # every other caller uses. A transition the domain forbids (e.g. moving
    # off an already-terminal CANCELLED/SUPERSEDED row) is not an error
    # here - the confirmation simply stays as it is, truthfully.
    try:
        updated = confirmation.with_status(eligibility, now=now)
        stored = confirmation_store.compare_and_swap_confirmation(
            db, confirmation=updated, expected_version=confirmation.version,
        )
    except InvalidConfirmationTransition:
        return confirmation
    except confirmation_store.StaleConfirmationVersion:
        # Someone else already moved it - re-read and trust that instead of
        # retrying blindly against a version we know is stale.
        fresh = confirmation_store.get_confirmation(db, confirmation.confirmation_id)
        return fresh if fresh is not None else confirmation

    _record_confirmation_event(
        db, confirmation_id=stored.confirmation_id, event_type="CONFIRMATION_CREATED",
        occurred_at=now, detail=f"status re-evaluated to {eligibility.value}",
    )
    if stored.status in FINALIZED_STATUSES and confirmation.status not in FINALIZED_STATUSES:
        _record_confirmation_event(
            db, confirmation_id=stored.confirmation_id, event_type="CONFIRMATION_FINALIZED", occurred_at=now,
        )
    return stored


def _record_confirmation_event(
    db: Database, *, confirmation_id: str, event_type: str, occurred_at: datetime,
    detail: str = "", data: dict | None = None,
) -> None:
    confirmation_store.record_event(db, ConfirmationEvent(
        event_id=_event_id(), confirmation_id=confirmation_id, event_type=event_type,
        occurred_at=occurred_at, detail=detail, data=data or {},
    ))


def _issue_document_safely(
    db: Database, *, booking_id: str, now: datetime,
) -> tuple[FinancialDocument | None, str]:
    """Never lets a document-render/issuance failure propagate - booking
    and payment truth are untouched either way (§ DOCUMENT FAILURE)."""
    idempotency_key = f"findoc:{booking_id}:receipt"
    try:
        document = document_service.issue_receipt_or_invoice(
            db, booking_id=booking_id, document_type=FinancialDocumentType.RECEIPT,
            idempotency_key=idempotency_key,
        )
        return document, ""
    except Exception as error:  # noqa: BLE001 - deliberately broad, see docstring
        logger.warning("financial document issuance failed for booking %s: %s", booking_id, error)
        return None, str(error)


def _send_communication_safely(
    db: Database, *, booking, confirmation: JourneyConfirmation, now: datetime,
) -> tuple[CustomerCommunication | None, str]:
    """Never lets a send failure/timeout propagate past this point, and
    never mutates booking/payment truth (§ EMAIL FAILURE)."""
    recipient = (booking.lead_email or "").strip()
    if not recipient:
        return None, "no recipient email on file"
    try:
        itinerary_lines = [
            f"{item.origin_city} -> {item.destination_city}"
            + (f", {item.carrier}{item.flight_number}" if item.carrier else "")
            for item in booking.items
        ]
        traveler_names = [booking.lead_name] if booking.lead_name else []
        subject, body_text = render_booking_confirmation_email(
            journey_reference=booking.journey_reference, traveler_names=traveler_names,
            party_size=booking.party_size, itinerary_lines=itinerary_lines,
            customer_total=booking.customer_total or 0.0, currency=booking.currency,
            service_tier=booking.service_tier, confirmation_status=confirmation.status.value,
        )
        idempotency_key = f"comm:{booking.booking_id}:confirmation"
        comm = communication_service.create_and_send_communication(
            db, booking_id=booking.booking_id, journey_reference=booking.journey_reference,
            user_id=confirmation.user_id, recipient_address=recipient, subject=subject,
            body_text=body_text, idempotency_key=idempotency_key,
        )
        return comm, ""
    except Exception as error:  # noqa: BLE001 - deliberately broad, see docstring
        logger.warning("communication send failed for booking %s: %s", booking.booking_id, error)
        return None, str(error)
