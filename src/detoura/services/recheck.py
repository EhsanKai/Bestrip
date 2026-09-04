"""Re-checking a saved trip against the providers (V6.1).

A saved trip is a snapshot. The price was true when it was taken and says
nothing about now, so the Saved screen deliberately shows no price-change line
until something re-prices it. This module is that something.

**It re-quotes the trip that was saved; it does not search for a new one.** The
question a traveler is asking is "is *this* trip still there, at that price?",
and re-running the optimizer would answer a different question - it would find
whatever is best today and invite comparing two different trips as though the
first had changed. So each leg and each stay is looked up individually and
matched back to what was saved.

Three rules this module exists to hold, all of them the same rule the search
endpoint holds, applied to a narrower question:

1. **A provider outage is not a missing trip.** If we could not reach the
   provider, the answer is ``UNVERIFIABLE`` - never ``UNAVAILABLE``. Telling
   someone their trip is gone because our timeout fired would be a lie with
   consequences: they would stop looking at a trip that is still bookable.
2. **Nothing is invented.** A leg that cannot be matched is reported as gone,
   not silently replaced with the next flight out. Two flights at different
   times are not the same trip, and quietly swapping one for the other would
   make the price comparison meaningless.
3. **The cache is bypassed.** The caching decorators exist so that one search
   does not issue fifteen thousand upstream calls; serving a *re-check* from
   that same memo table would return the very number we are supposed to be
   testing. Re-checks go to ``.inner``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..models.accommodation import AccommodationOption
from ..models.freshness import PriceFreshness, combine
from ..models.transport import TransportOption
from ..providers.failures import FailureLog, ProviderFailureKind
from ..services.planner import TravelPlanner

#: Below this, a price move is not worth interrupting anyone about. Fares
#: wobble by rounding and currency noise; a judgement, not a measurement.
MATERIAL_CHANGE = 1.0

#: How far the parts may sum from the saved total before we refuse to
#: compare them at all. Tight, because they should agree exactly: this
#: catches a component the client did not send, not floating-point dust.
RECONCILE_TOLERANCE = 0.05


class ComponentState(str, Enum):
    """What became of one leg or one stay."""

    FOUND = "FOUND"
    """Re-quoted. The price may or may not have moved."""

    SOLD_OUT = "SOLD_OUT"
    """Still listed, but no longer bookable for this party size."""

    GONE = "GONE"
    """The provider answered and this option was not in the answer."""

    UNVERIFIABLE = "UNVERIFIABLE"
    """The provider did not answer. We know nothing new about this part."""

    CARRIED = "CARRIED"
    """Included at its saved price, and deliberately not re-checked.

    Ground transfer only. The product contract reports a trip's transfers as a
    single figure rather than as individual options, so a client cannot name
    them precisely enough to re-quote - and splitting the figure to guess at
    them would be inventing prices. Carrying it forward keeps the totals
    comparable, which is the part that must not be wrong; the response says
    plainly that this piece was not re-checked."""


class RecheckStatus(str, Enum):
    """What became of the trip as a whole."""

    UNCHANGED = "UNCHANGED"
    PRICE_CHANGED = "PRICE_CHANGED"
    PARTIALLY_UNAVAILABLE = "PARTIALLY_UNAVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNVERIFIABLE = "UNVERIFIABLE"
    """We could not complete the check. Distinct from every other value: it is
    a statement about us, not about the trip."""


@dataclass(frozen=True)
class ComponentResult:
    """One leg or one stay, re-checked."""

    label: str
    state: ComponentState
    saved_price: float
    current_price: float | None = None
    freshness: PriceFreshness | None = None
    detail: str = ""

    @property
    def change(self) -> float | None:
        if self.current_price is None:
            return None
        return round(self.current_price - self.saved_price, 2)


@dataclass
class RecheckResult:
    """The whole trip, re-checked."""

    status: RecheckStatus
    checked_at: datetime
    saved_price: float
    current_price: float | None
    legs: list[ComponentResult] = field(default_factory=list)
    stays: list[ComponentResult] = field(default_factory=list)
    transfers: list[ComponentResult] = field(default_factory=list)
    freshness: PriceFreshness = PriceFreshness.UNKNOWN
    reconciled: bool = True
    """Whether the parts we were given add up to the price that was saved."""

    @property
    def components(self) -> list[ComponentResult]:
        return [*self.legs, *self.stays, *self.transfers]

    @property
    def change(self) -> float | None:
        if self.current_price is None:
            return None
        return round(self.current_price - self.saved_price, 2)

    @property
    def change_pct(self) -> float | None:
        if self.current_price is None or self.saved_price <= 0:
            return None
        return round((self.current_price - self.saved_price) / self.saved_price * 100, 1)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _same_leg(option: TransportOption, *, departure: datetime, operator: str) -> bool:
    """Whether ``option`` is the leg that was saved.

    Operator plus departure instant. Not the option id: ids are a provider's
    own handle and are entitled to change between calls for what a traveler
    would call the same flight.
    """
    if option.departure != departure:
        return False
    # An operator the saved trip did not record cannot be used to rule a
    # candidate out - matching on the timetable alone is the weaker claim, and
    # the weaker claim is the honest one when we know less.
    return not operator or option.operator == operator


def _same_stay(option: AccommodationOption, *, name: str) -> bool:
    """Whether ``option`` is the stay that was saved.

    By name, because that is the only stable identity the product ever showed
    the traveler. A stay saved without one cannot be matched, and says so
    rather than matching the first room in the list.
    """
    return bool(name) and option.name == name


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
def _recheck_leg(
    planner: TravelPlanner,
    *,
    origin: str,
    destination: str,
    departure: datetime,
    operator: str,
    saved_price: float,
    travelers: int,
    failures: FailureLog,
) -> ComponentResult:
    label = f"{origin} → {destination}"
    try:
        # `.inner`, not the caching wrapper: see the module docstring.
        options = planner.transport.inner.search(origin, destination, departure.date())
    except Exception as error:  # noqa: BLE001 - any provider fault is the same answer here
        failures.record(
            ProviderFailureKind.UNAVAILABLE,
            "transport",
            detail=str(error),
            context=label,
        )
        return ComponentResult(
            label=label,
            state=ComponentState.UNVERIFIABLE,
            saved_price=saved_price,
            detail="We could not reach the transport provider.",
        )

    match = next(
        (o for o in options if _same_leg(o, departure=departure, operator=operator)),
        None,
    )
    if match is None:
        return ComponentResult(
            label=label,
            state=ComponentState.GONE,
            saved_price=saved_price,
            detail="This departure is no longer offered.",
        )

    if not match.has_seats_for(travelers):
        failures.record(
            ProviderFailureKind.SOLD_OUT, "transport", context=label
        )
        return ComponentResult(
            label=label,
            state=ComponentState.SOLD_OUT,
            saved_price=saved_price,
            current_price=round(match.price_per_person * travelers, 2),
            freshness=match.provenance.freshness_at(datetime.now())
            if match.provenance
            else None,
            detail="No longer enough seats for the party.",
        )

    return ComponentResult(
        label=label,
        state=ComponentState.FOUND,
        saved_price=saved_price,
        current_price=round(match.price_per_person * travelers, 2),
        freshness=match.provenance.freshness_at(datetime.now())
        if match.provenance
        else None,
    )


def _recheck_stay(
    planner: TravelPlanner,
    *,
    city: str,
    check_in: datetime,
    check_out: datetime,
    name: str,
    saved_price: float,
    travelers: int,
    failures: FailureLog,
) -> ComponentResult:
    label = name or city
    if not name:
        # Nothing to match on. Reporting this as gone would be a claim we
        # cannot support; the honest answer is that we could not check it.
        return ComponentResult(
            label=label,
            state=ComponentState.UNVERIFIABLE,
            saved_price=saved_price,
            detail="This stay was saved without a name, so it cannot be matched.",
        )

    try:
        options = planner.accommodation.inner.search(
            city, check_in.date(), check_out.date(), travelers
        )
    except Exception as error:  # noqa: BLE001
        failures.record(
            ProviderFailureKind.UNAVAILABLE,
            "accommodation",
            detail=str(error),
            context=label,
        )
        return ComponentResult(
            label=label,
            state=ComponentState.UNVERIFIABLE,
            saved_price=saved_price,
            detail="We could not reach the accommodation provider.",
        )

    match = next((o for o in options if _same_stay(o, name=name)), None)
    if match is None:
        return ComponentResult(
            label=label,
            state=ComponentState.GONE,
            saved_price=saved_price,
            detail="This stay is no longer offered for these dates.",
        )

    if not match.has_capacity_for(travelers):
        failures.record(
            ProviderFailureKind.SOLD_OUT, "accommodation", context=label
        )
        return ComponentResult(
            label=label,
            state=ComponentState.SOLD_OUT,
            saved_price=saved_price,
            current_price=match.total_price(travelers),
            freshness=match.provenance.freshness_at(datetime.now())
            if match.provenance
            else None,
            detail="No longer enough rooms for the party.",
        )

    return ComponentResult(
        label=label,
        state=ComponentState.FOUND,
        saved_price=saved_price,
        current_price=match.total_price(travelers),
        freshness=match.provenance.freshness_at(datetime.now())
        if match.provenance
        else None,
    )


def _carried_transfer(*, label: str, saved_price: float) -> ComponentResult:
    """Ground transfer, included at what was paid and flagged as unchecked.

    See :attr:`ComponentState.CARRIED`. This is the one component the re-check
    does not verify, and it is the safest one to leave: a local train fare is
    the most stable line in the trip, and leaving it *out* would be far worse
    than leaving it unchecked - the total would come out low and an unchanged
    trip would be reported as a price drop.
    """
    return ComponentResult(
        label=label,
        state=ComponentState.CARRIED,
        saved_price=saved_price,
        current_price=saved_price,
        detail="Carried forward at the saved price; transfers are not re-checked.",
    )


def _status(components: list[ComponentResult], change: float | None) -> RecheckStatus:
    """Fold the parts into one answer.

    Order matters and is the point of the function. ``UNVERIFIABLE`` is checked
    first because it is the only value that describes *us* rather than the
    trip: with a part unchecked we cannot claim the trip is intact, and we
    certainly cannot claim it is gone.
    """
    states = [c.state for c in components]

    if ComponentState.UNVERIFIABLE in states:
        return RecheckStatus.UNVERIFIABLE

    lost = [s for s in states if s in (ComponentState.GONE, ComponentState.SOLD_OUT)]
    if lost:
        # Every part gone is a different message from one part gone: the first
        # ends the trip, the second is repairable by re-searching that leg.
        #
        # Measured against the parts we actually checked. A CARRIED transfer is
        # not evidence of a surviving trip - counting it would report a trip
        # with every flight and room gone as merely *partially* unavailable,
        # on the strength of a bus fare nobody re-quoted.
        checkable = [s for s in states if s is not ComponentState.CARRIED]
        return (
            RecheckStatus.UNAVAILABLE
            if len(lost) == len(checkable)
            else RecheckStatus.PARTIALLY_UNAVAILABLE
        )

    if change is not None and abs(change) >= MATERIAL_CHANGE:
        return RecheckStatus.PRICE_CHANGED
    return RecheckStatus.UNCHANGED


def recheck_trip(
    planner: TravelPlanner,
    *,
    legs: list[dict],
    stays: list[dict],
    transfers: list[dict] | None = None,
    travelers: int,
    saved_price: float,
    failures: FailureLog | None = None,
    now: datetime | None = None,
) -> RecheckResult:
    """Re-quote a saved trip, component by component.

    ``legs``, ``stays`` and ``transfers`` are the plain shapes the client
    saved, so the caller does not have to reconstruct engine objects it never
    had.
    """
    failures = failures if failures is not None else FailureLog()
    now = now or datetime.now()

    leg_results = [
        _recheck_leg(
            planner,
            origin=leg["origin"],
            destination=leg["destination"],
            departure=leg["departure"],
            operator=leg.get("operator", ""),
            saved_price=round(float(leg["price_per_person"]) * travelers, 2),
            travelers=travelers,
            failures=failures,
        )
        for leg in legs
    ]
    stay_results = [
        _recheck_stay(
            planner,
            city=stay["city"],
            check_in=stay["arrival"],
            check_out=stay["departure"],
            name=stay.get("name") or "",
            saved_price=float(stay["cost"]),
            travelers=travelers,
            failures=failures,
        )
        for stay in stays
    ]
    transfer_results = [
        _carried_transfer(
            label=transfer.get("label") or "Airport transfers",
            saved_price=float(transfer["cost"]),
        )
        for transfer in (transfers or [])
    ]

    components = [*leg_results, *stay_results, *transfer_results]

    # Do the parts we were handed actually make up the trip that was saved?
    #
    # If they do not, some component was left out of the request, and every
    # number below would be wrong in the most misleading direction available:
    # a total assembled from a subset is *lower*, so an unchanged trip would be
    # announced as a saving. Refusing to compare is the only honest move, and
    # checking it here makes that failure structurally impossible rather than
    # something each caller has to remember.
    reconciled = (
        abs(sum(c.saved_price for c in components) - saved_price) <= RECONCILE_TOLERANCE
    )

    # A total is only meaningful when every part was priced. A "new total"
    # computed from three of four legs would read as a bargain rather than as
    # an incomplete answer.
    priced = [c.current_price for c in components]
    current = (
        round(sum(p for p in priced if p is not None), 2)
        if reconciled and priced and all(p is not None for p in priced)
        else None
    )

    change = None if current is None else round(current - saved_price, 2)
    status = (
        _status(components, change)
        if reconciled
        else RecheckStatus.UNVERIFIABLE
    )
    return RecheckResult(
        status=status,
        checked_at=now,
        saved_price=saved_price,
        current_price=current,
        legs=leg_results,
        stays=stay_results,
        transfers=transfer_results,
        freshness=combine(*[c.freshness for c in components]),
        reconciled=reconciled,
    )
