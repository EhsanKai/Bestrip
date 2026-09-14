"""Mirror a live ``BookingRun`` into the persistent ops booking record.

Called on every state-changing endpoint (each returns the intent DTO, which
persists) and once more when the background run finishes, so the ops console
sees the truth even for a booking whose customer has closed the tab.

Only minimal PII crosses into storage: the lead traveller's name and email.
Nothing else from the traveller party is persisted here.

V9 Phase 6: this is also the one place ``BookingRun.owner_user_id`` gets
acted on - see ``_claim_ownership`` below. It runs on every call here, not
just the first, which is what makes it safe: ``claim_trip`` is idempotent
for the same user and a no-op fact of the domain (never a mutation this
module has to gate on "have I already done this once").
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..models.booking import BookingState
from ..persistence import accounts as accounts_store
from ..persistence import bookings
from ..persistence.db import Database
from .booking_orchestrator import BookingPhase, BookingRun

logger = logging.getLogger(__name__)

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


def _claim_ownership(run: BookingRun, db: Database) -> None:
    """Records that ``run.owner_user_id`` owns this booking, if it is set.

    A no-op for an anonymous run (``owner_user_id is None``) - anonymous
    booking must keep working exactly as before, and no owner row is ever
    created for one. Safe to call every time ``persist_run`` is called (see
    module docstring): ``claim_trip`` is idempotent for the same user and
    silently refuses (never raises, never reassigns) if the booking is
    already claimed by a *different* user - which cannot legitimately
    happen here (one run has exactly one ``owner_user_id``, fixed at
    creation), but the persistence layer's own "first valid owner wins"
    guarantee is what actually enforces that, not an assumption here.
    """
    if not run.owner_user_id:
        return
    try:
        accounts_store.claim_trip(
            db, user_id=run.owner_user_id, booking_id=run.booking_id,
            journey_reference=run.journey_reference,
        )
    except Exception:
        # Additive metadata, never a precondition of the booking flow (see
        # persistence/accounts.py's module docstring) - a claim hiccup must
        # not break persistence any more than persistence itself does below.
        # Still logged, unlike the upsert above: a booking record failing to
        # persist tends to surface some other way (Ops console, payment
        # flow breaking); an ownership row silently never getting written
        # has no other observable symptom at all - My Trips just stays
        # empty for that user, forever, with no signal anywhere.
        logger.exception(
            "claim_trip failed for booking_id=%s owner_user_id=%s",
            run.booking_id, run.owner_user_id,
        )


def persist_run(run: BookingRun, db: Database) -> None:
    """Best-effort. A persistence hiccup must never break the booking flow."""
    try:
        bookings.upsert(db, _record(run))
    except Exception:
        pass
    _claim_ownership(run, db)
