"""Discovered offer vs revalidated offer, field by field (V8 Phase 3).

The comparator is deliberately dumb about *why* something changed and precise
about *what* changed and *how much it matters*. It takes what the server
recorded at search time and a freshly re-fetched `TransportOption`, and returns
a list of `OfferChange` plus a per-offer status.

Three judgements it is trusted to make, and nothing more:

* **A currency change is never a price change.** USD 400 becoming EUR 400 is
  not a €0 delta - it is a different quote in a currency we will not relabel
  (the V7 Phase 3 rule). Blocking.
* **A baggage downgrade is never a harmless price move.** `INCLUDED` becoming
  `UNKNOWN` is blocking even if the fare fell, because the traveller agreed to
  a trip where the bag was on the plane.
* **A quote whose freshness we cannot establish is not fresh.** A re-fetched
  offer with no `expires_at` is `UNKNOWN`, and `UNKNOWN` blocks - it has not
  been shown to be current, it has only failed to be shown stale.

The tolerance is passed in. There is no default here and no constant: a hidden
tolerance is exactly the thing the spec forbids.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models.baggage import BaggageStatus
from ..models.booking import PriceTolerance
from ..models.provider_reference import OfferFreshness
from ..models.revalidation import ChangeSeverity, OfferChange, OfferRevalidationStatus
from ..models.transport import TransportOption
from .selection_store import SelectedOffer

#: Below this, a money move is rounding, not a change worth a line.
_PRICE_NOISE = 0.01


def _fmt_money(amount: float | None, currency: str) -> str:
    return "unknown" if amount is None else f"{amount:.2f} {currency}"


def _bag(status: BaggageStatus | None) -> str:
    return "unknown" if status is None else status.value


def _downgrade(before: BaggageStatus | None, after: BaggageStatus | None) -> bool:
    """Whether a baggage status got worse for the traveller.

    Only `INCLUDED -> anything else` counts. Everything else is either an
    improvement or a lateral move between kinds of "not on the plane yet", and
    the traveller can act on those without being forced back through consent.
    """
    return before is BaggageStatus.INCLUDED and after is not BaggageStatus.INCLUDED


def compare_offer(
    discovered: SelectedOffer,
    current: TransportOption,
    *,
    tolerance: PriceTolerance,
    now: datetime | None = None,
) -> tuple[list[OfferChange], OfferRevalidationStatus]:
    """One discovered offer against its re-fetched self.

    ``current`` is assumed already confirmed to exist (the caller handles
    404/expired/provider-error before reaching here). This function decides
    UNCHANGED / PRICE_CHANGED / TERMS_CHANGED and enumerates the differences.
    """
    now = now or datetime.now(timezone.utc)
    changes: list[OfferChange] = []

    ref = current.provider_ref
    current_amount = current.price_per_person * max(discovered.travelers, 1)
    current_raw_currency = (ref.quoted_currency if ref else None) or discovered.discovered_currency

    # --- currency -------------------------------------------------------
    disc_raw_ccy = discovered.discovered_raw_currency or discovered.discovered_currency
    currency_changed = bool(
        disc_raw_ccy and current_raw_currency and disc_raw_ccy != current_raw_currency
    )
    if currency_changed:
        changes.append(OfferChange(
            field="currency",
            discovered=disc_raw_ccy,
            current=current_raw_currency,
            severity=ChangeSeverity.BLOCKING,
            detail="the quote is now in a different currency; it will not be relabelled",
        ))

    # --- price ---------------------------------------------------------
    delta = round(current_amount - discovered.discovered_amount, 2)
    if not currency_changed and abs(delta) >= _PRICE_NOISE:
        if delta < 0:
            severity = ChangeSeverity.INFO
            detail = "the price fell since you looked"
        elif tolerance.accepts(discovered.discovered_amount, current_amount):
            severity = ChangeSeverity.MINOR
            detail = "the price rose but stayed within the configured tolerance"
        else:
            severity = ChangeSeverity.BLOCKING
            detail = "the price rose beyond the configured tolerance"
        changes.append(OfferChange(
            field="total_price",
            discovered=_fmt_money(discovered.discovered_amount, discovered.discovered_currency),
            current=_fmt_money(current_amount, discovered.discovered_currency),
            severity=severity,
            detail=detail,
        ))

    # --- baggage -----------------------------------------------------
    for kind, disc_status, cur_status in (
        ("cabin_bag", discovered.discovered_baggage_cabin,
         current.baggage.cabin_bag.status if current.baggage else None),
        ("checked_bag", discovered.discovered_baggage_checked,
         current.baggage.checked_bag.status if current.baggage else None),
    ):
        if disc_status is None or cur_status is None or disc_status == cur_status:
            continue
        if _downgrade(disc_status, cur_status):
            severity, detail = ChangeSeverity.BLOCKING, "a bag that was included is no longer included"
        elif cur_status is BaggageStatus.INCLUDED:
            severity, detail = ChangeSeverity.INFO, "a bag is now included that was not before"
        else:
            severity, detail = ChangeSeverity.MINOR, "the baggage terms changed"
        changes.append(OfferChange(
            field=kind, discovered=_bag(disc_status), current=_bag(cur_status),
            severity=severity, detail=detail,
        ))

    # --- hold / instant payment -----------------------------------
    disc_hold = discovered.discovered_hold_supported
    cur_hold = ref.hold_supported if ref else None
    if disc_hold is True and cur_hold is not True:
        changes.append(OfferChange(
            field="hold", discovered="supported", current=_tri(cur_hold),
            severity=ChangeSeverity.BLOCKING,
            detail="this fare could be held before; it now needs instant payment",
        ))
    elif disc_hold is not True and cur_hold is True:
        changes.append(OfferChange(
            field="hold", discovered=_tri(disc_hold), current="supported",
            severity=ChangeSeverity.INFO, detail="this fare can now be held",
        ))

    # --- itinerary identity: departure / arrival ------------------
    # A live Duffel offer id is immutable, so a re-fetch that comes back with
    # different times is either a different journey wearing the same id or a
    # provider bug - either way the traveller agreed to a schedule that no
    # longer holds. Blocking.
    for field_name, disc_dt, cur_dt in (
        ("departure", discovered.discovered_departure, current.departure),
        ("arrival", discovered.discovered_arrival, current.arrival),
    ):
        if disc_dt is None or cur_dt is None:
            continue
        if _same_instant(disc_dt, cur_dt):
            continue
        changes.append(OfferChange(
            field=field_name,
            discovered=disc_dt.isoformat(),
            current=cur_dt.isoformat(),
            severity=ChangeSeverity.BLOCKING,
            detail=f"the {field_name} time is not the one you selected",
        ))

    # --- freshness of the re-fetched quote ------------------------
    freshness = ref.freshness_at(now) if ref else OfferFreshness.UNKNOWN
    if freshness is OfferFreshness.EXPIRED:
        changes.append(OfferChange(
            field="expiry", discovered="valid", current="expired",
            severity=ChangeSeverity.BLOCKING, detail="the re-fetched quote has already expired",
        ))
    elif freshness is OfferFreshness.UNKNOWN:
        changes.append(OfferChange(
            field="expiry", discovered="valid", current="unknown",
            severity=ChangeSeverity.BLOCKING,
            detail="the provider stated no expiry, so this quote has not been shown to be current",
        ))
    elif freshness is OfferFreshness.EXPIRING_SOON:
        changes.append(OfferChange(
            field="expiry", discovered="valid", current="expiring soon",
            severity=ChangeSeverity.INFO, detail="book promptly; this quote expires within minutes",
        ))

    return changes, _status_from(changes)


def _tri(value: bool | None) -> str:
    return "unknown" if value is None else ("supported" if value else "not supported")


def _same_instant(a: datetime, b: datetime) -> bool:
    """Compare two datetimes as instants, tolerating a naive/aware mismatch."""
    if (a.tzinfo is None) != (b.tzinfo is None):
        a = a.replace(tzinfo=timezone.utc) if a.tzinfo is None else a
        b = b.replace(tzinfo=timezone.utc) if b.tzinfo is None else b
    return abs((a - b).total_seconds()) < 60


def _status_from(changes: list[OfferChange]) -> OfferRevalidationStatus:
    """The per-offer label.

    Terms beat price when both moved. A price move in any direction is
    `PRICE_CHANGED` - a drop is still a change worth showing. But a purely
    informational *term* note ("expiring soon", hold that became available) is
    disclosed in ``changes`` without relabelling the offer.
    """
    term_fields = {"currency", "cabin_bag", "checked_bag", "hold", "departure", "arrival", "expiry"}
    material_terms = [
        c for c in changes
        if c.field in term_fields and c.severity is not ChangeSeverity.INFO
    ]
    if material_terms:
        return OfferRevalidationStatus.TERMS_CHANGED
    if any(c.field == "total_price" for c in changes):
        return OfferRevalidationStatus.PRICE_CHANGED
    return OfferRevalidationStatus.UNCHANGED
