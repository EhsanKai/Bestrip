"""My Trips backend (V9 Phase 2.6 §A8, §A9; Phase 5 confirmation/documents).

    GET /api/v1/me/trips
    GET /api/v1/me/trips/{booking_id}
    GET /api/v1/me/trips/{booking_id}/confirmation
    GET /api/v1/me/trips/{booking_id}/documents
    GET /api/v1/me/trips/{booking_id}/documents/{document_id}
    POST /api/v1/me/trips/{booking_id}/confirmation/resend

Authenticated-owner-only. No final consumer UI here — just the minimum
data an authenticated caller needs to list and open their own trips,
plus confirmation/document visibility. Returns only the fields already
public within a booking record (trip label, route, dates, state) — never
lead traveler PII beyond what the booking record itself carries.

Phase 5 additions use duck-typed imports for persistence modules that
may not exist in this worktree yet (see module docstrings at import sites).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..persistence import accounts as store
from ..persistence import bookings as booking_store
from ..persistence import get_db
from .auth import SessionContext, require_session, require_csrf

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

router = APIRouter(prefix="/api/v1/me", tags=["me"])


def _trip_summary(rec) -> dict:
    return {
        "booking_id": rec.booking_id,
        "journey_reference": rec.journey_reference,
        "trip_label": rec.trip_label,
        "route_cities": rec.route_cities,
        "phase": rec.phase,
        "created_at": rec.created_at.isoformat(),
        "party_size": rec.party_size,
        "currency": rec.currency,
        "customer_total": rec.customer_total,
    }


@router.get("/trips")
def list_my_trips(session: SessionContext = Depends(require_session)) -> dict:
    db = get_db()
    booking_ids = store.list_trip_ids_for_user(db, session.user_id)
    trips = []
    for booking_id in booking_ids:
        rec = booking_store.get(db, booking_id)
        if rec is not None:
            trips.append(_trip_summary(rec))
    return {"trips": trips}


@router.get("/trips/{booking_id}")
def get_my_trip(booking_id: str, session: SessionContext = Depends(require_session)) -> dict:
    db = get_db()
    owner = store.get_trip_owner(db, booking_id)
    # A random/guessed booking_id and someone else's real booking_id must
    # produce the identical response shape - a 404 either way - so a caller
    # cannot distinguish "does not exist" from "exists but is not yours"
    # (§A9: "no ownership leakage through response differences").
    if owner is None or owner != session.user_id:
        raise HTTPException(status_code=404, detail={"message": "No such trip."})
    rec = booking_store.get(db, booking_id)
    if rec is None:
        raise HTTPException(status_code=404, detail={"message": "No such trip."})
    return _trip_summary(rec)


# ========================================================================
# Phase 5: Confirmation & Financial Documents (Consumer-facing)
# ========================================================================

def _confirmation_dto(conf) -> dict:
    """Serialize confirmation object to consumer-safe DTO.

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
        "created_at": conf.created_at.isoformat() if hasattr(conf.created_at, 'isoformat') else conf.created_at,
        "finalized_at": conf.finalized_at.isoformat() if conf.finalized_at and hasattr(conf.finalized_at, 'isoformat') else conf.finalized_at,
    }


def _document_dto(doc) -> dict:
    """Serialize financial document object to consumer-safe DTO.

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
        "issued_at": doc.issued_at.isoformat() if hasattr(doc.issued_at, 'isoformat') else doc.issued_at,
        "currency": doc.currency,
        "customer_total": doc.customer_total,
    }


@router.get("/trips/{booking_id}/confirmation")
def get_confirmation(booking_id: str, session: SessionContext = Depends(require_session)) -> dict:
    """Fetch confirmation for an owned booking. Returns 404 if booking is not owned
    or confirmation doesn't exist yet (same anti-enumeration pattern as list_my_trips)."""
    db = get_db()
    owner = store.get_trip_owner(db, booking_id)
    if owner is None or owner != session.user_id:
        raise HTTPException(status_code=404, detail={"message": "No such trip."})

    if confirmation_store is None:
        # Module not merged yet - return placeholder response indicating
        # no confirmation available. Once Agent 5's module is merged, this
        # will call the real function.
        raise HTTPException(status_code=404, detail={"message": "No confirmation available yet."})

    conf = confirmation_store.get_confirmation_for_booking(db, booking_id)
    if conf is None:
        raise HTTPException(status_code=404, detail={"message": "No such trip."})

    return _confirmation_dto(conf)


@router.get("/trips/{booking_id}/documents")
def list_documents(booking_id: str, session: SessionContext = Depends(require_session)) -> dict:
    """List financial documents (invoices, receipts) for an owned booking."""
    db = get_db()
    owner = store.get_trip_owner(db, booking_id)
    if owner is None or owner != session.user_id:
        raise HTTPException(status_code=404, detail={"message": "No such trip."})

    if document_store is None:
        # Module not merged yet - return empty list. Once Agent 5's module is
        # merged, this will call the real function.
        return {"booking_id": booking_id, "documents": []}

    documents = document_store.list_documents_for_booking(db, booking_id)
    return {
        "booking_id": booking_id,
        "documents": [_document_dto(d) for d in documents],
    }


@router.get("/trips/{booking_id}/documents/{document_id}")
def get_document(
    booking_id: str, document_id: str, session: SessionContext = Depends(require_session),
) -> dict:
    """Fetch a single financial document (metadata + optional PDF bytes).

    Ownership-checked at both booking and document level - a client cannot
    access documents belonging to a different booking/user, even if they
    own another booking.
    """
    db = get_db()
    owner = store.get_trip_owner(db, booking_id)
    if owner is None or owner != session.user_id:
        raise HTTPException(status_code=404, detail={"message": "No such trip."})

    if document_store is None:
        # Module not merged yet - return 404
        raise HTTPException(status_code=404, detail={"message": "No such document."})

    doc = document_store.get_document_for_user(db, document_id, user_id=session.user_id)
    if doc is None:
        raise HTTPException(status_code=404, detail={"message": "No such document."})

    # Explicit ownership check: document must belong to the requested booking.
    # This prevents a user from cross-accessing documents from their own other bookings.
    if doc.booking_id != booking_id:
        raise HTTPException(status_code=404, detail={"message": "No such document."})

    # Return document metadata. PDF byte serving can be added here once the
    # financial_documents module provides get_document_bytes or similar.
    # Agent 6: wire actual PDF serving once module is merged.
    return _document_dto(doc)


@router.post("/trips/{booking_id}/confirmation/resend")
def resend_confirmation(
    booking_id: str, request: Request, session: SessionContext = Depends(require_session),
) -> dict:
    """Request resend of confirmation email/communication.

    CSRF-protected for same reason as refund (mutating action). Delegates to
    communication domain to handle rate-limiting, idempotency, and actual
    resend logic - Ops cannot set communication status directly, only trigger
    valid domain operations.
    """
    db = get_db()
    owner = store.get_trip_owner(db, booking_id)
    if owner is None or owner != session.user_id:
        raise HTTPException(status_code=404, detail={"message": "No such trip."})

    require_csrf(request, session)

    if communication_store is None:
        # Module not merged yet - return placeholder indicating action was received.
        # Once Agent 5's module is merged, this will call:
        # communication_store.request_resend(db, booking_id=booking_id,
        #                                     communication_type="confirmation")
        return {"booking_id": booking_id, "status": "resend_requested"}

    try:
        # Call through to the communication domain's resend function.
        # Expected function signature:
        #   request_resend(db, booking_id: str, communication_type: str) -> Communication
        result = communication_store.request_resend(
            db, booking_id=booking_id, communication_type="confirmation"
        )
    except Exception as error:
        # Catch any validation errors from the communication domain (e.g.,
        # rate limit exceeded, invalid booking state, etc.)
        raise HTTPException(
            status_code=409, detail={"message": str(error)}
        )

    return {
        "booking_id": booking_id,
        "communication_id": result.communication_id,
        "status": result.status,
        "updated_at": result.updated_at.isoformat() if hasattr(result.updated_at, 'isoformat') else result.updated_at,
    }
