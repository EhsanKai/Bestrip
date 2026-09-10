"""Mirror a live ``BookingRun`` into the persistent ops booking record.

Called on every state-changing endpoint (each returns the intent DTO, which
persists) and once more when the background run finishes, so the ops console
sees the truth even for a booking whose customer has closed the tab.

Only minimal PII crosses into storage: the lead traveller's name and email.
Nothing else from the traveller party is persisted here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models.booking import BookingState
from ..persistence import bookings
from ..persistence.db import Database
from .booking_orchestrator import BookingPhase, BookingRun

_DEAD_LEG = {
    BookingState.UNAVAILABLE,
    BookingState.EXPIRED,
    BookingState.PRICE_CHANGED,
}


def recovery_state(run: BookingRun) -> str:
    if run.phase is BookingPhase.RECONFIRM_REQUIRED:
        return "PRICE_CHANGED"
    if run.phase is BookingPhase.PARTIAL_FAILURE:
        return "PARTIAL_FAILURE"
    if run.phase is BookingPhase.FAILED:
        confirmed = any(i.state is BookingState.CONFIRMED for i in run.items)
        return "PARTIAL_FAILURE" if confirmed else "FAILED"
    if any(i.state in _DEAD_LEG for i in run.items):
        return "UNAVAILABLE"
    return ""


def _record(run: BookingRun) -> bookings.BookingRecord:
    now = datetime.now(timezone.utc)
    lead_name = lead_email = ""
    if run.party is not None:
        lead = run.party.lead
        lead_name = getattr(lead, "full_name", "") or (
            f"{lead.given_name} {lead.family_name}".strip()
        )
        lead_email = getattr(lead, "email", "") or ""

    items = [
        bookings.BookingItemRecord(
            sequence=idx,
            origin_city=i.origin_city, origin_airport=i.origin_airport,
            destination_city=i.destination_city,
            destination_airport=i.destination_airport,
            departure=i.departure, arrival=i.arrival,
            carrier=i.carrier, flight_number=i.flight_number,
            operating_carrier=i.operating_carrier,
            operating_flight_number=i.operating_flight_number,
            carrier_name=i.carrier_name,
            offer_id=i.offer_id, provider=i.provider,
            quoted_price=i.quoted_price, current_price=i.current_price,
            booked_price=(
                i.current_price or i.quoted_price
                if i.state is BookingState.CONFIRMED
                else None
            ),
            currency=i.currency,
            cabin_baggage=i.cabin_baggage, checked_baggage=i.checked_baggage,
            required=i.required, state=i.state.value, detail=i.detail,
            provider_order_id=i.provider_order_id,
        )
        for idx, i in enumerate(run.items, start=1)
    ]

    return bookings.BookingRecord(
        booking_id=run.booking_id,
        session_ref=run.session_ref,
        journey_reference=run.journey_reference,
        created_at=now,  # upsert keeps the stored created_at on conflict
        updated_at=now,
        mode=run.mode.value,
        phase=run.phase.value,
        trip_label=run.trip_label,
        route_cities=list(run.route_cities),
        party_size=run.party.size if run.party else max(
            (i.travelers for i in run.items), default=1
        ),
        lead_name=lead_name,
        lead_email=lead_email,
        currency=run.currency,
        service_tier=run.service_tier.value,
        discovered_total=run.discovered_total,
        current_total=run.current_total,
        customer_total=run.quote.customer_total if run.quote else None,
        recovery_state=recovery_state(run),
        reconfirm_note=run.reconfirm_note,
        items=items,
    )


def persist_run(run: BookingRun, db: Database) -> None:
    """Best-effort. A persistence hiccup must never break the booking flow."""
    try:
        bookings.upsert(db, _record(run))
    except Exception:
        pass
