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
    GuidedBookingState,
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
    #: Basic / guided: Detoura prepared and re-checked the journey and now
    #: guides the traveller through booking each ticket. No managed
    #: orchestration runs and Detoura creates no Duffel Order in this flow.
    GUIDED_BOOKING = "guided_booking"
    #: The priced supplier transport does not reconcile with the sum of the
    #: bookable ticket fares. Confirmation is refused - a truthful stop rather
    #: than charging against a figure that cannot be explained.
    PRICE_INCONSISTENT = "price_inconsistent"


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
    #: Carrier identity, marketing/operating kept apart (V8.5 C3). ``carrier``
    #: is the marketing (ticketed) code; ``operating_carrier`` is the carrier
    #: that actually flies it, "" when the provider did not say or it is the
    #: same. ``carrier_name`` is the marketing carrier's name where known.
    carrier_name: str = ""
    operating_carrier: str = ""
    operating_flight_number: str = ""
    cabin_baggage: str = "unknown"
    checked_baggage: str = "unknown"
    required: bool = True
    state: BookingState = BookingState.DRAFT
    detail: str = ""
    current_price: float | None = None
    provider_order_id: str | None = None
    #: Basic / guided flow only. Set once the journey is prepared; advanced by
    #: the traveller as they book each ticket. ``guided_reported_by`` records
    #: who last set it ("traveller" | "detoura" | "provider") so a
    #: traveller-reported CONFIRMED is never shown as Detoura-verified.
    guided_state: "GuidedBookingState | None" = None
    guided_reported_by: str = ""
    guided_ref: str = ""

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
    """The **bookable supplier transport subtotal**, whole party, at discovery.
    This is the flights Detoura can actually book - NOT the optimizer's
    whole-trip estimate (which also models accommodation and transfers). It is
    the base the Detoura fee is computed on and the base a revalidated price is
    compared against."""
    tolerance: PriceTolerance
    items: list[ItemProgress]
    #: Display-only optimizer figures: what the whole trip is estimated to cost
    #: including things Detoura is not booking (accommodation, transfers). Never
    #: used in the commercial calculation.
    trip_estimate: dict = field(default_factory=dict)
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
    session_ref: str = ""
    economics_written: bool = False
    owner_user_id: str | None = None
    """The authenticated account that created this journey, or ``None`` for
    an anonymous one (V9 Phase 6). Deliberately separate from ``user_key``
    above, which is an unrelated per-key promo-redemption-limit scope, not
    an authorization identity - conflating them would let a change to
    promo-limiting semantics silently change who owns/can pay for a
    booking, or vice versa. Set exactly once, from the server-resolved
    session at booking-intent creation (api/v1.py); never accepted from a
    client-supplied field, and never reassigned afterwards - see
    services/booking_persistence.py, which is the only place this is acted
    on (claims the trip for this account on every persist), and
    persistence/accounts.claim_trip, which itself refuses to move an
    existing different owner's claim."""
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
    def supplier_transport_at_discovery(self) -> float:
        """Party subtotal of the quoted per-person leg fares."""
        return round(
            sum(i.quoted_price * max(i.travelers, 1) for i in self.items), 2
        )

    @property
    def supplier_transport_current(self) -> float | None:
        """Party subtotal at the revalidated per-person fares, or ``None`` if a
        leg has not been re-checked."""
        if any(i.current_price is None for i in self.items):
            return None
        return round(
            sum(
                (i.current_price if i.current_price is not None else i.quoted_price)
                * max(i.travelers, 1)
                for i in self.items
            ),
            2,
        )

    @property
    def current_total(self) -> float | None:
        """The revalidated bookable supplier transport subtotal (party)."""
        return self.supplier_transport_current


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
        # `discovered_amount` is the party amount for the leg; store it
        # per person, so `quoted_price * travelers` is the leg's party total
        # and nothing multiplies the party size in twice.
        quoted_price=round(
            offer.discovered_amount / max(offer.travelers, 1), 2
        ),
        currency=offer.discovered_currency,
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
        new_state, note = _revalidate_item(run, item, duffel=duffel)
        with run._lock:
            if item.state is BookingState.REVALIDATING:  # don't clobber a state set elsewhere meanwhile
                item.state = new_state
            item.detail = note
            if new_state is BookingState.READY and note:
                changed.append(f"{item.origin_city} → {item.destination_city}: {note}")
        sleep(pace["revalidate"])

    with run._lock:
        # Any required item that did not reach READY stops the run - whether
        # it is a definite FAILED, a fare that's UNAVAILABLE, or an uncertain
        # TIMEOUT/PROVIDER_FAILURE (V9 Phase 6: previously collapsed to a
        # blind FAILED here, losing exactly the distinction the domain model
        # carries these states to make - see _revalidate_item/_issue_item).
        failed_reval = [i for i in run.required_items if i.state is not BookingState.READY]
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
                # Only a still-READY item (never reached issuance because an
                # earlier *required* leg failed first) becomes NOT_ATTEMPTED.
                # An item that already has its own resolved outcome from
                # revalidation - UNAVAILABLE/FAILED/TIMEOUT/PROVIDER_FAILURE,
                # reachable here when a *non-required* leg's revalidation
                # failed without itself stopping the run - keeps that
                # outcome (V9 Phase 6: the sibling of the same fix applied
                # to _mark_unattempted above; this branch had the identical
                # gap - independently caught by adversarial review).
                if item.state is BookingState.READY:
                    item.state = BookingState.NOT_ATTEMPTED
                    item.detail = "not attempted - an earlier leg could not be booked"
            continue
        if item.state is not BookingState.READY:
            # A non-required item that did not pass revalidation
            # (UNAVAILABLE/FAILED/TIMEOUT/PROVIDER_FAILURE) - required items
            # are already guaranteed READY here by the `failed_reval` gate
            # above, which stops the run before issuance for any that
            # aren't. Re-attempting issuance for an offer already known
            # unavailable/uncertain is not a retry of the same attempt, it's
            # an independent new one with its own chance of an effect at the
            # provider - it must not happen silently just because this leg
            # is optional (V9 Phase 6, caught by adversarial review). Its
            # revalidation-time state and detail are left exactly as they
            # were; nothing here overwrites them.
            continue
        with run._lock:
            item.state = BookingState.USER_CONFIRMED
        sleep(pace["issue_pre"])
        with run._lock:
            item.state = BookingState.BOOKING
        sleep(pace["issue_mid"])
        new_state, note, order_id = _issue_item(run, item, duffel=duffel)
        with run._lock:
            item.detail = note
            if new_state is BookingState.CONFIRMED:
                item.state = BookingState.CONFIRMED
                item.provider_order_id = order_id
            else:
                # Definite (FAILED/UNAVAILABLE) or uncertain (TIMEOUT/
                # PROVIDER_FAILURE) - either way this leg did not confirm and
                # a required one stops the run (V9 Phase 6: previously always
                # recorded as a blind FAILED regardless of which).
                item.state = new_state
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
    """Called when a required leg's *revalidation* fails and the run stops
    before issuance ever starts. Only a ``READY`` item - one that passed
    revalidation but will now never be issued - is downgraded to
    NOT_ATTEMPTED. Anything that already has a specific, resolved
    revalidation outcome (FAILED, UNAVAILABLE, TIMEOUT, PROVIDER_FAILURE)
    keeps it (V9 Phase 6: an earlier version's exemption list only
    protected FAILED, so an item revalidated as TIMEOUT/PROVIDER_FAILURE -
    the exact uncertain-outcome case this state split exists to preserve -
    would have been immediately overwritten right back to NOT_ATTEMPTED
    here, erasing the distinction the moment it was recorded)."""
    for i in run.items:
        if i.state is BookingState.READY:
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
) -> tuple[BookingState, str]:
    """Returns ``(new_state, note)``. ``READY`` means this leg may proceed to
    issuance; anything else means it may not - see ``_issue_item`` for why
    the *specific* non-READY state matters (V9 Phase 6): a network timeout
    or an unreachable provider is not the same fact as the fare being gone,
    and collapsing both into one generic "can't book this" value is how an
    uncertain outcome gets treated exactly like a definite one downstream.
    """
    if run.mode is PassMode.DEMO_ONLY or duffel is None or item.provider != "duffel":
        item.current_price = item.quoted_price
        return BookingState.READY, ""
    try:
        current = duffel.revalidate_offer(
            item.offer_id, item.origin_airport, item.destination_airport,
            travelers=item.travelers,
        )
    except DuffelOfferGone:
        return BookingState.UNAVAILABLE, "the fare is no longer available"
    except DuffelOrderError as error:
        # A definite response from the provider, not a connectivity/timeout
        # uncertainty - treated as FAILED like any other outright refusal.
        return BookingState.FAILED, f"could not re-check the fare ({error.code or 'error'})"
    except TimeoutError as error:
        # TimeoutError is itself an OSError subclass - caught first so it
        # gets its own, more specific state rather than falling into the
        # OSError branch below.
        return BookingState.TIMEOUT, f"could not re-check the fare in time ({type(error).__name__})"
    except (ProviderHttpError, OSError) as error:
        return BookingState.PROVIDER_FAILURE, f"could not re-check the fare ({type(error).__name__})"
    ref = current.provider_ref
    if ref and ref.is_expired_at():
        return BookingState.FAILED, "the fare expired before it could be booked"
    # Per person, like `quoted_price` - the party total is always
    # ``price * travelers`` and nothing multiplies it in twice.
    item.current_price = round(current.price_per_person, 2)
    if item.carrier == "" and current.operator:
        parts = current.operator.split()
        item.carrier = parts[0] if parts else ""
        item.flight_number = parts[1] if len(parts) > 1 else ""
    ref = current.provider_ref
    if ref is not None:
        # Marketing / operating carrier, kept apart. Never merge them.
        if getattr(ref, "marketing_carrier", None):
            item.carrier = ref.marketing_carrier or item.carrier
        item.carrier_name = getattr(ref, "marketing_carrier_name", "") or item.carrier_name
        item.operating_carrier = getattr(ref, "operating_carrier", "") or ""
        item.operating_flight_number = getattr(ref, "operating_flight_number", "") or ""
    delta = item.current_price - item.quoted_price
    if abs(delta) < 0.01:
        return BookingState.READY, ""
    return BookingState.READY, f"fare now {item.current_price:.2f} {item.currency} ({delta:+.2f})"


def _issue_item(
    run: BookingRun, item: ItemProgress, *, duffel: DuffelTransportProvider | None,
) -> tuple[BookingState, str, str | None]:
    """Returns ``(new_state, note, provider_order_id)``.

    ``new_state`` is ``CONFIRMED`` or one of the domain's own "did not
    confirm" states - never a bare boolean collapsed back to plain FAILED
    (V9 Phase 6). Duffel gives Detoura no client-supplied idempotency key
    for Order creation (verified: nothing in ``providers/duffel.py``
    negotiates one), so *Detoura's own* concurrency control - one execution
    per confirmation, see ``booking_flow.start_confirmation`` - is what
    prevents a double-submit, not the provider. A timeout or an unreachable
    provider here means Detoura genuinely does not know whether an Order
    was created; that must read as ``TIMEOUT``/``PROVIDER_FAILURE`` (which
    the domain routes toward ``RECOVERY_REQUIRED`` - a person decides,
    checking with the provider first) rather than a plain ``FAILED`` (which
    reads as "definitely nothing happened, safe to just try again" - the
    one thing that is not true here).
    """
    if run.mode is PassMode.DEMO_ONLY or duffel is None or item.provider != "duffel":
        return BookingState.CONFIRMED, "test booking item prepared (demo mode - no Duffel Order)", None
    if run.party is None:
        return BookingState.FAILED, "no traveller details", None
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
        return BookingState.UNAVAILABLE, "the fare was gone by the time we tried to book it", None
    except DuffelOrderError as error:
        # A definite response from the provider (including "price_changed" -
        # create_test_order's own re-check just before the write, see
        # providers/duffel.py) - not an uncertain outcome.
        return BookingState.FAILED, f"the provider refused the sandbox order ({error.code or 'order_failed'})", None
    except TimeoutError as error:
        # Caught ahead of the OSError branch below (TimeoutError is itself
        # an OSError subclass) so a timeout gets its own state rather than
        # the more generic PROVIDER_FAILURE - both route to RECOVERY_REQUIRED
        # (never a blind retry), but keeping them distinct preserves the
        # domain's own vocabulary for whoever reconciles this by hand.
        return BookingState.TIMEOUT, (
            "the provider did not respond in time - whether an order was "
            "created cannot be confirmed from here"
        ), None
    except (ProviderHttpError, OSError) as error:
        return BookingState.PROVIDER_FAILURE, (
            f"the provider could not be reached ({type(error).__name__}) - "
            "whether an order was created cannot be confirmed from here"
        ), None
    return BookingState.CONFIRMED, "Duffel Test Mode Order created", str(order.get("id"))
