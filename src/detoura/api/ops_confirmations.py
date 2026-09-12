"""Ops-authenticated visibility + safe recovery actions for confirmations (V9 Phase 5).

    GET /api/v1/ops/confirmations/{booking_id}
    POST /api/v1/ops/confirmations/{booking_id}/communication/retry

Full visibility into booking state, payments, confirmation, documents,
and communication status for operational inspection and recovery. Like
``ops_payments.py``, this router implements READ visibility + ONE safe
recovery action (retry communication), never a generic status-setter.

No Ops endpoint may set a status field directly on any domain (confirmation,
documents, communications). Ops here is purely a READ + narrow retry-trigger
surface, exactly like the payment recovery discipline (§U).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..persistence import bookings as booking_store
from ..persistence import get_db
from ..persistence import payments as payments_store
from .ops_auth import require_ops

# Duck-typed imports for modules that may not exist yet in this worktree.
# These will be replaced by real imports once Agent 5's modules are merged.
# Expected shapes are documented in each import comment.
try:
    from ..persistence import confirmations as confirmation_store
except (ImportError, ModuleNotFoundError):
    confirmation_store = None  # type: ignore

try:
    from ..persistence import financial_documents as document_store
except (ImportError, ModuleNotFoundError):
    document_store = None  # type: ignore

try:
    from ..persistence import communications as communication_store
except (ImportError, ModuleNotFoundError):
    communication_store = None  # type: ignore

router = APIRouter(prefix="/api/v1/ops/confirmations", tags=["ops", "confirmations"])


def _confirmation_dto(conf) -> dict:
    """Serialize confirmation to Ops visibility DTO.

    Expected confirmation_store.get_confirmation_for_booking shape:
    {
        confirmation_id, status (str), service_tier (str), booking_id,
        journey_reference, created_at, finalized_at
    }
    """
    return {
        "confirmation_id": conf.confirmation_id,
        "status": conf.status,
        "service_tier": conf.service_tier,
        "booking_id": conf.booking_id,
        "journey_reference": conf.journey_reference,
        "created_at": conf.created_at.isoformat() if hasattr(conf.created_at, 'isoformat') else conf.created_at,
        "finalized_at": conf.finalized_at.isoformat() if conf.finalized_at and hasattr(conf.finalized_at, 'isoformat') else conf.finalized_at,
    }


def _payment_dto(payment) -> dict:
    """Serialize payment for Ops visibility (mirrors ops_payments.py pattern)."""
    return {
        "payment_id": payment.payment_id,
        "booking_id": payment.booking_id,
        "journey_reference": payment.journey_reference,
        "user_id": payment.user_id,
        "currency": payment.currency,
        "customer_total": payment.customer_total,
        "status": payment.status.value,
        "provider": payment.provider,
        "authorized_amount": payment.authorized_amount,
        "captured_amount": payment.captured_amount,
        "refunded_amount": payment.refunded_amount,
        "created_at": payment.created_at.isoformat(),
        "updated_at": payment.updated_at.isoformat(),
    }


def _document_dto(doc) -> dict:
    """Serialize financial document for Ops visibility.

    Expected document_store document shape:
    {
        document_id, document_type (str), document_number (str), booking_id,
        issued_at, currency, customer_total
    }
    """
    return {
        "document_id": doc.document_id,
        "document_type": doc.document_type,
        "document_number": doc.document_number,
        "booking_id": doc.booking_id,
        "issued_at": doc.issued_at.isoformat() if hasattr(doc.issued_at, 'isoformat') else doc.issued_at,
        "currency": doc.currency,
        "customer_total": doc.customer_total,
    }


def _communication_dto(comm) -> dict:
    """Serialize communication/attempt for Ops visibility.

    Expected communication_store shape:
    {
        communication_id, status (str), communication_type (str),
        booking_id, created_at, updated_at
    }
    Expected attempt shape:
    {
        attempt_number (int), status (str), created_at, completed_at,
        error_detail (str | None)
    }
    """
    return {
        "communication_id": comm.communication_id,
        "status": comm.status,
        "communication_type": comm.communication_type,
        "booking_id": comm.booking_id,
        "created_at": comm.created_at.isoformat() if hasattr(comm.created_at, 'isoformat') else comm.created_at,
        "updated_at": comm.updated_at.isoformat() if hasattr(comm.updated_at, 'isoformat') else comm.updated_at,
    }


def _attempt_dto(attempt) -> dict:
    """Serialize communication attempt for Ops visibility."""
    return {
        "attempt_number": attempt.attempt_number,
        "status": attempt.status,
        "created_at": attempt.created_at.isoformat() if hasattr(attempt.created_at, 'isoformat') else attempt.created_at,
        "completed_at": attempt.completed_at.isoformat() if attempt.completed_at and hasattr(attempt.completed_at, 'isoformat') else attempt.completed_at,
        "error_detail": getattr(attempt, 'error_detail', None),
    }


@router.get("/{booking_id}")
def get_booking_confirmation_state(booking_id: str, actor: str = Depends(require_ops)) -> dict:
    """Full Ops visibility: booking state + confirmation + payments + documents +
    communication status + attempts.

    This is the confirmation/document recovery center view - a single endpoint
    showing everything an operator needs to see to understand and troubleshoot
    a booking's post-payment lifecycle.
    """
    db = get_db()
    booking = booking_store.get(db, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail={"message": "No such booking."})

    result = {
        "booking_id": booking_id,
        "journey_reference": booking.journey_reference,
        "phase": booking.phase,
        "recovery_state": booking.recovery_state,
        "created_at": booking.created_at.isoformat(),
        "updated_at": booking.updated_at.isoformat(),
        "confirmation": None,
        "payments": [],
        "documents": [],
        "communications": [],
    }

    # Fetch confirmation state if module is merged
    if confirmation_store is not None:
        try:
            conf = confirmation_store.get_confirmation_for_booking(db, booking_id)
            if conf is not None:
                result["confirmation"] = _confirmation_dto(conf)
        except Exception:
            # Module exists but call failed - include error signal
            result["confirmation_error"] = "Failed to fetch confirmation state"

    # Fetch payments (this module exists)
    try:
        payments = payments_store.list_payments_for_booking(db, booking_id)
        result["payments"] = [_payment_dto(p) for p in payments]
    except Exception:
        result["payments_error"] = "Failed to fetch payments"

    # Fetch documents if module is merged
    if document_store is not None:
        try:
            documents = document_store.list_documents_for_booking(db, booking_id)
            result["documents"] = [_document_dto(d) for d in documents]
        except Exception:
            result["documents_error"] = "Failed to fetch documents"

    # Fetch communications if module is merged
    if communication_store is not None:
        try:
            # Collect all communication types (e.g., confirmation email, receipt email, etc.)
            # Expected API: list_communications_for_booking(db, booking_id) -> list[Communication]
            comms = communication_store.list_communications_for_booking(db, booking_id)
            comm_list = []
            for comm in comms:
                comm_dto = _communication_dto(comm)
                # Fetch attempts for this communication
                try:
                    attempts = communication_store.list_attempts_for_communication(
                        db, comm.communication_id
                    )
                    comm_dto["attempts"] = [_attempt_dto(a) for a in attempts]
                except Exception:
                    comm_dto["attempts_error"] = "Failed to fetch attempts"
                comm_list.append(comm_dto)
            result["communications"] = comm_list
        except Exception:
            result["communications_error"] = "Failed to fetch communications"

    return result


@router.post("/{booking_id}/communication/retry")
def retry_communication(booking_id: str, actor: str = Depends(require_ops)) -> dict:
    """The ONE safe Ops action: retry a failed communication.

    This goes through the communication domain's own retry/resend function,
    never allowing Ops to set a status directly. If the communication is not
    in a retriable state, the domain function must reject the call, and Ops
    sees that error - Ops cannot override domain state machine invariants.

    Expected function signature:
        communication_store.request_resend(db, booking_id: str, communication_type: str = None)
            (if communication_type is None, retry the most recent/primary one)
    """
    db = get_db()
    booking = booking_store.get(db, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail={"message": "No such booking."})

    if communication_store is None:
        # Module not merged yet - return placeholder indicating action was received.
        # Once Agent 5's module is merged, this will call the real function.
        return {
            "booking_id": booking_id,
            "status": "retry_requested",
            "note": "communication persistence module not yet available",
        }

    try:
        # Call the communication domain's resend/retry function.
        # This function must validate that the communication is in a retriable state
        # and enforce whatever rate limits / retry policies it defines.
        result = communication_store.request_resend(db, booking_id=booking_id)
    except Exception as error:
        # Any validation/policy error from the communication domain propagates.
        raise HTTPException(
            status_code=409, detail={"message": str(error)}
        )

    return {
        "booking_id": booking_id,
        "communication_id": result.communication_id,
        "status": result.status,
        "updated_at": result.updated_at.isoformat() if hasattr(result.updated_at, 'isoformat') else result.updated_at,
    }
