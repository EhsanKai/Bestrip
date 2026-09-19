"""My Trips backend (V9 Phase 2.6 §A8, §A9; Phase 5 confirmation/documents).

    GET /api/v1/me/trips
    GET /api/v1/me/trips/{booking_id}
    GET /api/v1/me/trips/{booking_id}/confirmation
    GET /api/v1/me/trips/{booking_id}/documents
    GET /api/v1/me/trips/{booking_id}/documents/{document_id}
    GET /api/v1/me/trips/{booking_id}/documents/{document_id}/download
    POST /api/v1/me/trips/{booking_id}/confirmation/resend

Authenticated-owner-only. No final consumer UI here — just the minimum
data an authenticated caller needs to list and open their own trips,
plus confirmation/document visibility. Returns only the fields already
public within a booking record (trip label, route, dates, state) — never
lead traveler PII beyond what the booking record itself carries.

Phase 5 additions use duck-typed imports for persistence modules that
may not exist in this worktree yet (see module docstrings at import sites).

V9 Financial Document Download API slice: the ``/download`` route serves
the immutable PDF bytes already generated and persisted by
``services/financial_document_service.py`` (see
``persistence/financial_documents.py::get_document_pdf_for_user``). It
never regenerates, recomputes or mutates a document — it is a read of an
already-issued artifact, gated by the same two-level ownership check
(booking ownership, then document-belongs-to-booking) as the existing
metadata route below.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response

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
    from ..services import communication_service
except (ImportError, ModuleNotFoundError):
    communication_store = None  # type: ignore
    communication_service = None  # type: ignore

router = APIRouter(prefix="/api/v1/me", tags=["me"])

#: Financial documents contain customer financial information; never let an
#: intermediary or the browser cache the response. Mirrors the exact
#: convention already used for sensitive HTML in ``api/static.py``.
_NO_STORE = "no-cache, no-store, must-revalidate"

#: Characters allowed in a generated Content-Disposition filename. The
#: document type and number are both server-generated (see
#: ``persistence/financial_documents.py::allocate_document_number``), but
#: this is stripped defensively anyway: a filename ends up in an HTTP header,
#: and nothing client-observable should ever decide what goes in there
#: unsanitized.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename_component(value: str) -> str:
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", value)
    return cleaned[:80] or "document"


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

    Deliberately excluded: filesystem/storage paths, supplier order/offer
    ids, provider payloads, and any other internal reconciliation metadata —
    none of it is on the ``FinancialDocument`` model in the first place (see
    ``models/financial_document.py``'s own privacy boundary), so there is
    nothing here to accidentally forward.
    """
    document_type = getattr(doc.document_type, "value", doc.document_type)
    return {
        "document_id": doc.document_id,
        "document_type": document_type,
        "document_number": doc.document_number,
        "issued_at": doc.issued_at.isoformat() if hasattr(doc.issued_at, 'isoformat') else doc.issued_at,
        "currency": doc.currency,
        "customer_total": doc.customer_total,
        # Every document issued through financial_document_service always
        # has a stored PDF (see its module docstring) — this is surfaced as
        # data rather than assumed by the frontend, so a future document
        # type or a corrupt row can honestly report unavailable instead.
        "download_available": True,
        "download_url": (
            f"/api/v1/me/trips/{doc.booking_id}/documents/{doc.document_id}/download"
        ),
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

    return _document_dto(doc)


@router.get("/trips/{booking_id}/documents/{document_id}/download")
def download_document(
    booking_id: str, document_id: str, session: SessionContext = Depends(require_session),
) -> Response:
    """Download the immutable PDF bytes of an owned financial document.

    Same two-level ownership check as :func:`get_document` above (booking
    ownership, then document-belongs-to-booking), applied before the PDF
    blob is ever read — a cross-booking or cross-user id substitution never
    reaches ``get_document_pdf_for_user``. Serves the exact bytes stored at
    issuance; this route computes nothing and never regenerates a document
    from newer data.
    """
    db = get_db()
    owner = store.get_trip_owner(db, booking_id)
    if owner is None or owner != session.user_id:
        raise HTTPException(status_code=404, detail={"message": "No such trip."})

    if document_store is None:
        raise HTTPException(status_code=404, detail={"message": "No such document."})

    doc = document_store.get_document_for_user(db, document_id, user_id=session.user_id)
    if doc is None or doc.booking_id != booking_id:
        raise HTTPException(status_code=404, detail={"message": "No such document."})

    pdf_bytes = document_store.get_document_pdf_for_user(
        db, document_id, user_id=session.user_id
    )
    if pdf_bytes is None:
        # Metadata exists but no artifact is stored against it. Every
        # document issued through financial_document_service always has one
        # (see its own module docstring); reaching this means a corrupt or
        # incomplete row, not a normal state — fail safely, never a 500 with
        # a stack trace or a path.
        raise HTTPException(
            status_code=404, detail={"message": "No document artifact available."}
        )

    filename = (
        f"detoura-{_safe_filename_component(doc.document_type.value.lower())}-"
        f"{_safe_filename_component(doc.document_number)}.pdf"
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": _NO_STORE,
        },
    )


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

    if communication_service is None:
        # Module not merged yet - return placeholder indicating action was received.
        return {"booking_id": booking_id, "status": "resend_requested"}

    try:
        # request_resend re-renders and re-sends the SAME logical
        # booking-confirmation communication - a new attempt, never a new
        # communication row (see services/communication_service.py).
        result = communication_service.request_resend(db, booking_id=booking_id)
    except (
        communication_service.NoSuchCommunication,
        communication_service.CommunicationAlreadyInFlight,
    ) as error:
        # Only these two known, safe-to-echo domain errors surface with
        # their own message - no communication exists yet, or a send is
        # already in flight (a concurrent resend loses this race cleanly;
        # see persistence.communications.claim_send_slot, V9 Phase 5 QA
        # finding #1). Anything else (an unexpected internal exception)
        # must never echo str(error) to a caller - that leaked a raw
        # sqlite3.IntegrityError, including real table/column names,
        # before this fix - so it falls through to FastAPI's own 500.
        raise HTTPException(status_code=409, detail={"message": str(error)})

    return {
        "booking_id": booking_id,
        "communication_id": result.communication_id,
        "status": result.status,
        "updated_at": result.updated_at.isoformat() if hasattr(result.updated_at, 'isoformat') else result.updated_at,
    }
