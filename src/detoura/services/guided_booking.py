"""The Basic / guided booking flow (V8.5 Phase C).

Basic is a polished product, not a downgrade. Detoura discovers and optimises
the journey, prices it transparently, re-checks the fares, takes the traveller's
details **once for the whole journey**, and then guides them through booking
each underlying ticket in one coherent workflow.

What Basic does *not* do: run the managed multi-ticket orchestrator or create a
Detoura-managed Duffel Order. The traveller completes each ticket purchase; the
extra All-in-One fee is what pays for Detoura doing that work instead.

Because there is no Detoura-side order for a Basic ticket, Detoura never asserts
``CONFIRMED`` from its own knowledge - that state is only ever set from the
traveller's own report, and stored/shown as traveller-reported.
"""

from __future__ import annotations

from ..models.booking import BookingState, GuidedBookingState
from ..models.commercial import ServiceTier
from .booking_orchestrator import BookingPhase, BookingRun, _revalidate_item

_GUIDANCE = (
    "Book the {origin} → {destination} leg on {date} directly with "
    "{carrier} (or a travel agent you trust). Match the fare, times and "
    "baggage shown here — Detoura re-checked them for you. Your traveller "
    "details are saved in Detoura for this journey; an external site may still "
    "ask you to enter them there, which is its requirement, not Detoura's."
)


def prepare_journey(run: BookingRun) -> None:
    """Re-check fares and open the guided booking workflow. Synchronous, in
    place. Requires the one-per-journey traveller party. Raises for a managed
    (All-in-One) booking."""
    if run.service_tier is not ServiceTier.BASIC:
        raise ValueError("prepare_journey is for Basic bookings only")
    if run.party is None:
        raise ValueError("traveller details are required first")

    with run._lock:
        run.phase = BookingPhase.REVALIDATING
        for item in run.items:
            item.state = BookingState.REVALIDATING

    changed: list[str] = []
    for item in run.items:
        ok, note = _revalidate_item(run, item, duffel=None)
        with run._lock:
            if ok:
                item.state = BookingState.READY
                item.guided_state = GuidedBookingState.READY_TO_BOOK
                item.guided_reported_by = "detoura"
                if note:
                    changed.append(
                        f"{item.origin_city} → {item.destination_city}: {note}"
                    )
            else:
                item.state = BookingState.UNAVAILABLE
                item.guided_state = GuidedBookingState.UNKNOWN
                item.guided_reported_by = "detoura"
                item.detail = note

    with run._lock:
        run.reconfirm_note = "; ".join(changed)
        run.phase = BookingPhase.GUIDED_BOOKING


def _item(run: BookingRun, sequence: int):
    if sequence < 1 or sequence > len(run.items):
        raise KeyError(sequence)
    return run.items[sequence - 1]


def start_ticket(run: BookingRun, sequence: int) -> None:
    """The traveller has gone to book this ticket externally."""
    _require_guided(run)
    item = _item(run, sequence)
    with run._lock:
        item.guided_state = GuidedBookingState.EXTERNAL_BOOKING_STARTED
        item.guided_reported_by = "traveller"


def mark_ticket(
    run: BookingRun,
    sequence: int,
    state: GuidedBookingState,
    *,
    reference: str = "",
) -> None:
    """The traveller reports where this ticket now stands. Detoura records it
    as traveller-reported and never upgrades it to verified."""
    _require_guided(run)
    item = _item(run, sequence)
    with run._lock:
        item.guided_state = state
        item.guided_reported_by = "traveller"
        # A short confirmation code is fine to keep; never store PII here.
        item.guided_ref = (reference or "").strip()[:40]
        if state is GuidedBookingState.CONFIRMED:
            item.detail = "Marked as booked by the traveller (not verified by Detoura)."
        elif state is GuidedBookingState.BOOKING_CONFIRMATION_REQUIRED:
            item.detail = "Traveller is waiting for the airline's confirmation."
        else:
            item.detail = ""


def guidance(item) -> str:
    return _GUIDANCE.format(
        carrier=item.carrier or "the operating airline",
        origin=item.origin_city,
        destination=item.destination_city,
        date=item.departure.strftime("%d %b %Y") if item.departure else "the shown date",
    )


def progress(run: BookingRun) -> tuple[int, int]:
    booked = sum(
        1
        for i in run.items
        if i.guided_state is GuidedBookingState.CONFIRMED
    )
    return booked, len(run.items)


def _require_guided(run: BookingRun) -> None:
    if run.phase is not BookingPhase.GUIDED_BOOKING:
        raise ValueError("this journey is not in the guided booking stage")
