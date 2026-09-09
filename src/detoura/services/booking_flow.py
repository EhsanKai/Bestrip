"""The Phase 4 booking flow: selection -> intent -> travellers -> confirm -> pass.

One `BookingRun` per journey, held in memory with a short TTL (a booking that
sits untouched for an hour is abandoned; the offers behind it are dead anyway).
The store is the single source of booking truth - the client polls it, never
asserts into it.

`start_confirmation` spawns a daemon thread that runs the orchestrator and
mutates the run as each leg progresses, so `GET /booking-intents/{id}` returns
the real per-leg state rather than a fabricated one.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from collections import OrderedDict

from ..models.booking import PriceTolerance
from ..models.travel_pass import (
    DetouraTravelPass,
    PassMode,
    PassStatus,
    PassTicket,
)
from ..models.traveler import TravelerParty
from ..providers.duffel import DuffelTransportProvider, is_test_token
from ..providers.http import RateLimiter, RetryingHttpClient, UrllibHttpClient
from ..models.booking import BookingState
from .booking_orchestrator import (
    BookingPhase,
    BookingRun,
    ItemProgress,
    item_from_selected,
    run_booking,
)
from .journey_reference import new_journey_reference
from .selection_store import Selection

DEFAULT_BOOKING_TTL_SECONDS = 60 * 60
DEFAULT_MAX_BOOKINGS = 2_000

#: Cities Detoura knows, keyed by airport, for the pass's big visual. Kept here
#: rather than imported from the catalog so a booking never depends on catalog
#: state that may have changed since the search.
_AIRPORT_CITY = {
    "CGN": "Cologne", "DUS": "Düsseldorf", "FRA": "Frankfurt", "AMS": "Amsterdam",
    "EIN": "Eindhoven", "BER": "Berlin", "MUC": "Munich", "VIE": "Vienna",
    "PRG": "Prague", "BCN": "Barcelona", "MAD": "Madrid", "LHR": "London",
    "CDG": "Paris", "MXP": "Milan", "FCO": "Rome", "DUB": "Dublin",
    "CPH": "Copenhagen", "BUD": "Budapest", "ZRH": "Zurich", "BRU": "Brussels",
}


def _city(code: str) -> str:
    return _AIRPORT_CITY.get(code, code)


class BookingStore:
    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_BOOKING_TTL_SECONDS,
        max_bookings: int = DEFAULT_MAX_BOOKINGS,
        clock=time.monotonic,
    ) -> None:
        self._runs: "OrderedDict[str, BookingRun]" = OrderedDict()
        self._expiry: dict[str, float] = {}
        self._ttl = ttl_seconds
        self._max = max_bookings
        self._clock = clock
        self._lock = threading.Lock()

    def put(self, run: BookingRun) -> None:
        with self._lock:
            self._runs[run.booking_id] = run
            self._expiry[run.booking_id] = self._clock() + self._ttl
            self._runs.move_to_end(run.booking_id)
            while len(self._runs) > self._max:
                old, _ = self._runs.popitem(last=False)
                self._expiry.pop(old, None)

    def get(self, booking_id: str) -> BookingRun | None:
        with self._lock:
            run = self._runs.get(booking_id)
            if run is None:
                return None
            if self._expiry.get(booking_id, 0) <= self._clock():
                del self._runs[booking_id]
                self._expiry.pop(booking_id, None)
                return None
            return run

    def __len__(self) -> int:
        return len(self._runs)


_STORE: BookingStore | None = None


def booking_store() -> BookingStore:
    global _STORE
    if _STORE is None:
        _STORE = BookingStore()
    return _STORE


# ---------------------------------------------------------------------------
# Building the run
# ---------------------------------------------------------------------------
def _route_cities(items: list[ItemProgress]) -> tuple[str, ...]:
    if not items:
        return ()
    cities = [items[0].origin_city]
    for it in items:
        cities.append(it.destination_city)
    return tuple(cities)


def create_run_from_selection(
    selection: Selection, *, tolerance: PriceTolerance | None = None,
) -> BookingRun:
    """A SANDBOX_BOOKED run - real Duffel offers behind every leg."""
    items = [item_from_selected(i + 1, o) for i, o in enumerate(selection.offers)]
    for it in items:
        it.origin_city = _city(it.origin_airport)
        it.destination_city = _city(it.destination_airport)
    run = BookingRun(
        booking_id="bk_" + secrets.token_urlsafe(15),
        journey_reference=new_journey_reference(),
        mode=PassMode.SANDBOX_BOOKED,
        trip_label=selection.trip_label,
        route_cities=_route_cities(items),
        currency=selection.currency,
        discovered_total=selection.discovered_total,
        tolerance=tolerance or PriceTolerance(),
        items=items,
        selection_id=selection.selection_id,
        session_ref="sess_" + secrets.token_urlsafe(8),
    )
    return run


def create_run_demo(
    *,
    trip_label: str,
    currency: str,
    discovered_total: float,
    legs: list[dict],
    tolerance: PriceTolerance | None = None,
) -> BookingRun:
    """A DEMO_ONLY run from a synthetic trip - no Duffel offer ids, no Order.

    ``legs`` are plain dicts: origin, destination (IATA or city), departure,
    arrival, carrier, flight_number, price_per_person, travelers, cabin, checked.
    """
    items: list[ItemProgress] = []
    for i, leg in enumerate(legs, start=1):
        o, d = str(leg["origin"]), str(leg["destination"])
        items.append(ItemProgress(
            item_id=f"item-{i}",
            origin_city=_city(o), origin_airport=o,
            destination_city=_city(d), destination_airport=d,
            departure=leg["departure"], arrival=leg["arrival"],
            carrier=leg.get("carrier", ""), flight_number=leg.get("flight_number", ""),
            offer_id=f"demo-{i}", provider="synthetic",
            travelers=int(leg.get("travelers", 1)),
            quoted_price=float(leg["price_per_person"]),
            currency=currency,
            cabin_baggage=leg.get("cabin", "unknown"),
            checked_baggage=leg.get("checked", "unknown"),
        ))
    return BookingRun(
        booking_id="bk_" + secrets.token_urlsafe(15),
        journey_reference=new_journey_reference(),
        mode=PassMode.DEMO_ONLY,
        trip_label=trip_label,
        route_cities=_route_cities(items),
        currency=currency,
        discovered_total=discovered_total,
        tolerance=tolerance or PriceTolerance(),
        items=items,
        session_ref="sess_" + secrets.token_urlsafe(8),
    )


def attach_travelers(run: BookingRun, party: TravelerParty) -> None:
    """Fix the traveller party on the run. Size must match the trip."""
    needed = max((i.travelers for i in run.items), default=1)
    if party.size != needed:
        raise ValueError(f"this journey needs {needed} traveller(s), got {party.size}")
    with run._lock:
        run.party = party
        run.phase = BookingPhase.AWAITING_CONFIRMATION


def _duffel_for_booking() -> DuffelTransportProvider | None:
    token = os.getenv("DUFFEL_ACCESS_TOKEN", "")
    if not is_test_token(token):
        return None
    http = RetryingHttpClient(
        UrllibHttpClient(), max_retries=2, rate_limiter=RateLimiter(0.25)
    )
    return DuffelTransportProvider(
        access_token=token, http_client=http, max_calls=40, timeout=15.0
    )


def start_confirmation(run: BookingRun, *, duffel_factory=_duffel_for_booking) -> None:
    """One user confirmation for the whole journey. Spawns the run thread.

    A SANDBOX_BOOKED run with no configured sandbox token degrades to
    DEMO_ONLY rather than failing - and the resulting pass says so.
    """
    if run.party is None:
        raise ValueError("traveller details are required before confirmation")
    if run.phase not in (BookingPhase.AWAITING_CONFIRMATION, BookingPhase.RECONFIRM_REQUIRED):
        raise ValueError(f"cannot confirm from phase {run.phase.value}")

    duffel = duffel_factory() if run.mode is PassMode.SANDBOX_BOOKED else None
    if run.mode is PassMode.SANDBOX_BOOKED and duffel is None:
        run.mode = PassMode.DEMO_ONLY  # no token - honest downgrade

    def _worker() -> None:
        try:
            run_booking(run, duffel=duffel)
        except Exception:  # defensive: a run thread must not die silently
            with run._lock:
                run.phase = BookingPhase.FAILED
        finally:
            # Persist the final state + write the economics ledger even if the
            # customer has stopped polling.
            try:
                from ..persistence import get_db
                from .booking_commercial import finalize_economics
                from .booking_persistence import persist_run

                db = get_db()
                persist_run(run, db)
                finalize_economics(run, db)
            except Exception:
                pass

    threading.Thread(target=_worker, name=f"booking-{run.booking_id}", daemon=True).start()


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------
def build_travel_pass(run: BookingRun) -> DetouraTravelPass:
    """The server-owned artifact. Only READY when every required leg confirmed."""
    intent = run.journey_intent()
    outcome = intent.outcome
    if outcome is BookingState.CONFIRMED:
        status = PassStatus.READY
    elif outcome is BookingState.PARTIAL_FAILURE:
        status = PassStatus.RECOVERY_REQUIRED
    else:
        status = PassStatus.FAILED

    tickets = tuple(
        PassTicket(
            sequence=idx,
            origin_city=i.origin_city, origin_airport=i.origin_airport,
            destination_city=i.destination_city, destination_airport=i.destination_airport,
            departure=i.departure, arrival=i.arrival,
            carrier=i.carrier, flight_number=i.flight_number,
            cabin_baggage=i.cabin_baggage, checked_baggage=i.checked_baggage,
            price_per_person=i.quoted_price, currency=i.currency,
            booking_state=i.state,
            provider_order_id=i.provider_order_id,
        )
        for idx, i in enumerate(run.items, start=1)
    )
    order_ids = tuple(i.provider_order_id for i in run.items if i.provider_order_id)
    dates = tuple(sorted({i.departure.date().isoformat() for i in run.items}))
    unknowns = tuple(sorted({
        f"{i.origin_city} → {i.destination_city}: checked baggage"
        for i in run.items if i.checked_baggage == "unknown"
    }))

    return DetouraTravelPass(
        journey_reference=run.journey_reference,
        booking_id=run.booking_id,
        mode=run.mode,
        status=status,
        traveler_name=run.party.lead.full_name if run.party else "—",
        party_size=run.party.size if run.party else 1,
        route_cities=run.route_cities,
        travel_dates=dates,
        tickets=tickets,
        trip_total=run.discovered_total,
        currency=run.currency,
        baggage_complete=not unknowns,
        unknowns=unknowns,
        provider_order_ids=order_ids,
    )
