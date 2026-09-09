"""One confirmation, several tickets - executed (V8 Phase 4).

V7.5 built the booking domain (`models/booking.py`): the state machine, the
`JourneyBookingIntent` whose outcome is *derived* from its items, the rule that
a journey cannot reach CONFIRMED while one required leg has not. This module
runs a journey through that domain against the real provider, one leg at a
time, and exposes the per-leg state as it goes so a progress screen can show
the truth rather than a spinner.

Two booking modes:

* ``SANDBOX_BOOKED`` - a Duffel **Test Mode** Order is created for each leg,
  after a fresh revalidation and a price-tolerance check. No card, no real
  money: payment is against the test account balance.
* ``DEMO_ONLY`` - no Duffel Order. The legs are marked confirmed from the
  selected/revalidated data so the flow stays testable when a real Order
  cannot be created, and the resulting pass says so plainly.

The hard rule, from the domain and restated here: the run **stops at the first
failed required leg**. Remaining legs stay NOT_ATTEMPTED. The journey outcome
is then PARTIAL_FAILURE (some legs confirmed) or FAILED (none), never CONFIRMED.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from ..models.booking import (
    BookingItem,
    BookingState,
    JourneyBookingIntent,
    PriceTolerance,
)
from ..models.commercial import CommercialQuote, ServiceTier
from ..models.travel_pass import PassMode
from ..models.traveler import TravelerParty
from ..providers.duffel import (
    DuffelOfferGone,
    DuffelOrderError,
    DuffelTransportProvider,
    duffel_passengers_from,
)
from ..providers.http import ProviderHttpError
from .selection_store import SelectedOffer


class BookingPhase(str, Enum):
    """Where the whole booking run currently is - for the progress screen."""

    AWAITING_TRAVELERS = "awaiting_travelers"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    REVALIDATING = "revalidating"
    RECONFIRM_REQUIRED = "reconfirm_required"
    ISSUING = "issuing"
    COMPLETE = "complete"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"


@dataclass(slots=True)
class ItemProgress:
    """One leg's live state, mutated as the run proceeds."""

    item_id: str
    origin_city: str
    origin_airport: str
    destination_city: str
    destination_airport: str
    departure: datetime
    arrival: datetime
    carrier: str
    flight_number: str
    offer_id: str
    provider: str
    travelers: int
    quoted_price: float
    currency: str
    cabin_baggage: str = "unknown"
    checked_baggage: str = "unknown"
    required: bool = True
    state: BookingState = BookingState.DRAFT
    detail: str = ""
    current_price: float | None = None
    provider_order_id: str | None = None

    def to_item(self) -> BookingItem:
        from ..models.provider_reference import ProviderOfferReference

        return BookingItem(
            item_id=self.item_id,
            provider_ref=ProviderOfferReference(
                provider=self.provider, offer_id=self.offer_id,
            ),
            origin=self.origin_city, destination=self.destination_city,
            departure=self.departure, arrival=self.arrival,
            travelers=self.travelers, quoted_price=self.quoted_price,
            currency=self.currency,
            state=self.state if self.state is not BookingState.NOT_ATTEMPTED
            else BookingState.NOT_ATTEMPTED,
            required=self.required,
        )


@dataclass(slots=True)
class BookingRun:
    """The whole booking, keyed by one id and polled while it runs."""

    booking_id: str
    journey_reference: str
    mode: PassMode
    trip_label: str
    route_cities: tuple[str, ...]
    currency: str
    discovered_total: float
    tolerance: PriceTolerance
    items: list[ItemProgress]
    selection_id: str | None = None
    party: TravelerParty | None = None
    phase: BookingPhase = BookingPhase.AWAITING_TRAVELERS
    reconfirm_note: str = ""
    created_at: float = field(default_factory=time.monotonic)
    # V8.5 commercial layer: the chosen Detoura service tier, the priced quote
    # the customer sees and confirms, and a stable key for per-user promo
    # limits. ``economics_written`` guards the one-time ledger write.
    service_tier: ServiceTier = ServiceTier.BASIC
    requested_promo: str | None = None
    quote: CommercialQuote | None = None
    user_key: str = "anonymous"
    economics_written: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def required_items(self) -> list[ItemProgress]:
        return [i for i in self.items if i.required]

    def journey_intent(self) -> JourneyBookingIntent:
        """A domain intent reflecting the current item states, for outcome
        derivation. Items still in DRAFT/NOT_ATTEMPTED map straight through."""
        return JourneyBookingIntent(
            journey_id=self.booking_id,
            items=tuple(i.to_item() for i in self.items),
            quoted_total=self.discovered_total,
            currency=self.currency,
            travelers=self.items[0].travelers if self.items else 1,
        )

    @property
    def current_total(self) -> float | None:
        if any(i.current_price is None for i in self.items):
            return None
        delta = sum((i.current_price - i.quoted_price) for i in self.items)
        return round(self.discovered_total + delta, 2)


def item_from_selected(seq: int, offer: SelectedOffer) -> ItemProgress:
    return ItemProgress(
        item_id=f"item-{seq}",
        origin_city=offer.origin, origin_airport=offer.origin,
        destination_city=offer.destination, destination_airport=offer.destination,
        departure=offer.discovered_departure or datetime.now(timezone.utc),
        arrival=offer.discovered_arrival or datetime.now(timezone.utc),
        carrier="", flight_number="",
        offer_id=offer.offer_id, provider=offer.provider,
        travelers=offer.travelers,
        quoted_price=offer.discovered_amount, currency=offer.discovered_currency,
        cabin_baggage=(offer.discovered_baggage_cabin.value
                       if offer.discovered_baggage_cabin else "unknown"),
        checked_baggage=(offer.discovered_baggage_checked.value
                         if offer.discovered_baggage_checked else "unknown"),
        required=offer.required,
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
#: Deliberate per-leg pacing so the progress screen is actually watchable.
#: SANDBOX_BOOKED gets its pacing from real Duffel latency (1-3s per call), so
#: it only needs a small settle; DEMO_ONLY has no network wait at all, so
#: without this the revalidation and per-leg issuance screens flash past before
#: anyone can read them.
_PACE = {
    PassMode.DEMO_ONLY: {"revalidate": 0.7, "issue_pre": 0.45, "issue_mid": 0.9, "issue_post": 0.45},
    PassMode.SANDBOX_BOOKED: {"revalidate": 0.15, "issue_pre": 0.2, "issue_mid": 0.3, "issue_post": 0.2},
}


def run_booking(
    run: BookingRun,
    *,
    duffel: DuffelTransportProvider | None,
    sleep=time.sleep,
) -> None:
    """Execute ``run`` in place: revalidate, then issue leg by leg.

    Blocking. The caller runs this on a background thread and polls ``run``.
    ``duffel`` is required for ``SANDBOX_BOOKED`` and unused for ``DEMO_ONLY``.
    Every state change takes the run's lock so a poll never sees a torn state.
    """
    pace = _PACE[run.mode]
    with run._lock:
        run.phase = BookingPhase.REVALIDATING
        for item in run.items:
            item.state = BookingState.REVALIDATING

    changed: list[str] = []
    for item in run.items:
        ok, note = _revalidate_item(run, item, duffel=duffel)
        with run._lock:
            if not ok:
                item.state = BookingState.FAILED if item.state is BookingState.REVALIDATING else item.state
                item.detail = note
            else:
                item.state = BookingState.READY
                if note:
                    changed.append(f"{item.origin_city} → {item.destination_city}: {note}")
        sleep(pace["revalidate"])

    with run._lock:
        failed_reval = [i for i in run.required_items if i.state is BookingState.FAILED]
        if failed_reval:
            run.phase = BookingPhase.FAILED
            _mark_unattempted(run)
            return
        if changed and _breaches_tolerance(run):
            run.phase = BookingPhase.RECONFIRM_REQUIRED
            run.reconfirm_note = "; ".join(changed)
            for i in run.items:
                if i.state is BookingState.READY:
                    pass  # stays READY, awaiting a fresh confirm
            return
        run.phase = BookingPhase.ISSUING

    # Issue, leg by leg, stopping at the first required failure.
    stop = False
    for item in run.items:
        if stop:
            with run._lock:
                if item.state not in (BookingState.CONFIRMED, BookingState.FAILED):
                    item.state = BookingState.NOT_ATTEMPTED
                    item.detail = "not attempted - an earlier leg could not be booked"
            continue
        with run._lock:
            item.state = BookingState.USER_CONFIRMED
        sleep(pace["issue_pre"])
        with run._lock:
            item.state = BookingState.BOOKING
        sleep(pace["issue_mid"])
        ok, note, order_id = _issue_item(run, item, duffel=duffel)
        with run._lock:
            if ok:
                item.state = BookingState.CONFIRMED
                item.provider_order_id = order_id
                item.detail = note
            else:
                item.state = BookingState.FAILED
                item.detail = note
                if item.required:
                    stop = True
        sleep(pace["issue_post"])

    with run._lock:
        outcome = run.journey_intent().outcome
        if outcome is BookingState.CONFIRMED:
            run.phase = BookingPhase.COMPLETE
        elif outcome is BookingState.PARTIAL_FAILURE:
            run.phase = BookingPhase.PARTIAL_FAILURE
        else:
            run.phase = BookingPhase.FAILED


def _mark_unattempted(run: BookingRun) -> None:
    for i in run.items:
        if i.state not in (BookingState.CONFIRMED, BookingState.FAILED):
            i.state = BookingState.NOT_ATTEMPTED


def _breaches_tolerance(run: BookingRun) -> bool:
    if any(i.current_price is None for i in run.items):
        return True
    current = run.current_total
    if current is None:
        return True
    return not run.tolerance.accepts(run.discovered_total, current)


def _revalidate_item(
    run: BookingRun, item: ItemProgress, *, duffel: DuffelTransportProvider | None,
) -> tuple[bool, str]:
    """Returns (ok, note). ``ok`` False means this leg cannot be booked."""
    if run.mode is PassMode.DEMO_ONLY or duffel is None or item.provider != "duffel":
        item.current_price = item.quoted_price
        return True, ""
    try:
        current = duffel.revalidate_offer(
            item.offer_id, item.origin_airport, item.destination_airport,
            travelers=item.travelers,
        )
    except DuffelOfferGone:
        return False, "the fare is no longer available"
    except (DuffelOrderError, ProviderHttpError, TimeoutError, OSError) as error:
        return False, f"could not re-check the fare ({type(error).__name__})"
    ref = current.provider_ref
    if ref and ref.is_expired_at():
        return False, "the fare expired before it could be booked"
    item.current_price = round(current.price_per_person * max(item.travelers, 1), 2)
    if item.carrier == "" and current.operator:
        parts = current.operator.split()
        item.carrier = parts[0] if parts else ""
        item.flight_number = parts[1] if len(parts) > 1 else ""
    delta = item.current_price - item.quoted_price
    if abs(delta) < 0.01:
        return True, ""
    return True, f"fare now {item.current_price:.2f} {item.currency} ({delta:+.2f})"


def _issue_item(
    run: BookingRun, item: ItemProgress, *, duffel: DuffelTransportProvider | None,
) -> tuple[bool, str, str | None]:
    """Returns (ok, note, provider_order_id)."""
    if run.mode is PassMode.DEMO_ONLY or duffel is None or item.provider != "duffel":
        return True, "test booking item prepared (demo mode - no Duffel Order)", None
    if run.party is None:
        return False, "no traveller details", None
    try:
        offer = duffel.get_offer(item.offer_id)
        passengers = duffel_passengers_from(offer, run.party.travelers[: item.travelers])
        amount = str(offer.get("total_amount"))
        currency = offer.get("total_currency") or item.currency
        order = duffel.create_test_order(
            item.offer_id, passengers=passengers,
            expected_amount=amount, expected_currency=currency,
        )
    except DuffelOfferGone:
        return False, "the fare was gone by the time we tried to book it", None
    except DuffelOrderError as error:
        return False, f"the provider refused the sandbox order ({error.code or 'order_failed'})", None
    except (ProviderHttpError, TimeoutError, OSError) as error:
        return False, f"the provider could not be reached ({type(error).__name__})", None
    return True, "Duffel Test Mode Order created", str(order.get("id"))
