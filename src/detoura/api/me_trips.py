"""My Trips backend (V9 Phase 2.6 §A8, §A9).

    GET /api/v1/me/trips
    GET /api/v1/me/trips/{booking_id}

Authenticated-owner-only. No final consumer UI here — just the minimum
data an authenticated caller needs to list and open their own trips.
Returns only the fields already public within a booking record (trip
label, route, dates, state) — never lead traveler PII beyond what the
booking record itself carries, and the booking record's own PII minimalism
(see :mod:`detoura.persistence.bookings`) is unchanged by this router.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..persistence import accounts as store
from ..persistence import bookings as booking_store
from ..persistence import get_db
from .auth import SessionContext, require_session

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
