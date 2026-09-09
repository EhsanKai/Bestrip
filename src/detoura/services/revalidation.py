"""Re-check a selected itinerary against Duffel, immediately before booking (V8 Phase 3).

The core product guarantee: a trip discovered from a snapshot must never proceed
toward booking on stale data. This is the gate.

For every bookable offer in the selection, one Duffel Get Offer call. What comes
back is compared against what the server recorded at search time. The result
says, per offer and for the itinerary as a whole, whether booking may proceed,
proceed with disclosure, require the traveller to agree again, or stop.

Rules held here:

* **No fallback to the snapshot price, ever.** If Duffel cannot be reached for
  an offer, that offer is `PROVIDER_ERROR` and the itinerary is not bookable.
  The alternative - quietly trusting the discovered price - is the exact defect
  this phase exists to prevent.
* **Unavailable, expired and provider-error stay distinct.** They need
  different responses and only one of them is safe to retry.
* **The multi-ticket invariant.** Two valid tickets and one unavailable is
  `NOT_BOOKABLE`, never "ready". `JourneyBookingIntent` enforces the same thing
  in the domain; this is the same rule at the provider boundary.
* **Bounded.** One call per offer, and a hard ceiling on offers per request, so
  a revalidation cannot be turned into an unbounded Duffel query.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models.booking import PriceTolerance
from ..models.provider_reference import OfferFreshness
from ..models.revalidation import (
    ChangeSeverity,
    OfferChange,
    OfferRevalidationResult,
    OfferRevalidationStatus,
    RevalidatedOffer,
    RevalidationStatus,
)
from ..providers.duffel import (
    DuffelAuthError,
    DuffelConfigurationError,
    DuffelLiveModeError,
    DuffelOfferGone,
    DuffelTransportProvider,
    ProviderCallBudgetExceeded,
)
from ..providers.http import ProviderHttpError
from .offer_comparator import compare_offer
from .selection_store import SelectedOffer, Selection

#: A selected itinerary is a handful of legs. Anything past this is not a trip,
#: it is someone using the endpoint as a Duffel proxy.
MAX_OFFERS_PER_REVALIDATION = 8


class RevalidationLimitExceeded(RuntimeError):
    """The selection names more offers than one revalidation may re-fetch."""


def _revalidate_one(
    offer: SelectedOffer,
    *,
    duffel: DuffelTransportProvider,
    tolerance: PriceTolerance,
    now: datetime,
) -> RevalidatedOffer:
    base = dict(
        offer_id=offer.offer_id,
        provider=offer.provider,
        origin=offer.origin,
        destination=offer.destination,
        leg_label=offer.leg_label,
        discovered_amount=offer.discovered_amount,
        discovered_currency=offer.discovered_currency,
        discovered_baggage_cabin=offer.discovered_baggage_cabin,
        discovered_baggage_checked=offer.discovered_baggage_checked,
        discovered_hold_supported=offer.discovered_hold_supported,
    )

    try:
        current = duffel.revalidate_offer(
            offer.offer_id, offer.origin, offer.destination,
            travelers=offer.travelers,
        )
    except DuffelOfferGone:
        return RevalidatedOffer(
            **base, status=OfferRevalidationStatus.UNAVAILABLE,
            detail="the provider no longer has this offer",
        )
    except DuffelLiveModeError:
        # A response that could not prove it was sandbox. Fail closed - never
        # compared, never trusted.
        return RevalidatedOffer(
            **base, status=OfferRevalidationStatus.PROVIDER_ERROR,
            detail="the provider response could not be confirmed as test mode",
        )
    except (DuffelAuthError, DuffelConfigurationError, ProviderCallBudgetExceeded,
            ProviderHttpError, TimeoutError, OSError) as error:
        return RevalidatedOffer(
            **base, status=OfferRevalidationStatus.PROVIDER_ERROR,
            detail=f"the offer could not be re-checked ({type(error).__name__})",
        )

    ref = current.provider_ref
    current_amount = round(current.price_per_person * max(offer.travelers, 1), 2)
    current_ccy = (ref.quoted_currency if ref else None) or offer.discovered_currency
    current_hold = ref.hold_supported if ref else None
    cur_cabin = current.baggage.cabin_bag.status if current.baggage else None
    cur_checked = current.baggage.checked_bag.status if current.baggage else None
    current_expiry = ref.expires_at if ref else None

    live_fields = dict(
        current_amount=current_amount,
        current_currency=current_ccy,
        current_baggage_cabin=cur_cabin,
        current_baggage_checked=cur_checked,
        current_hold_supported=current_hold,
        current_expires_at=current_expiry,
    )

    if ref and ref.freshness_at(now) is OfferFreshness.EXPIRED:
        return RevalidatedOffer(
            **base, **live_fields, status=OfferRevalidationStatus.EXPIRED,
            changes=(OfferChange(
                field="expiry", discovered="valid", current="expired",
                severity=ChangeSeverity.BLOCKING,
                detail="the offer was re-fetched but its stated expiry has passed",
            ),),
            detail="the offer has expired",
        )

    changes, status = compare_offer(offer, current, tolerance=tolerance, now=now)
    return RevalidatedOffer(
        **base, **live_fields, status=status, changes=tuple(changes),
    )


def revalidate_selection(
    selection: Selection,
    *,
    duffel: DuffelTransportProvider,
    tolerance: PriceTolerance | None = None,
    now: datetime | None = None,
) -> OfferRevalidationResult:
    """Re-check every offer in ``selection`` and classify the itinerary.

    ``tolerance`` is required to be explicit at the call site or defaulted to
    :class:`PriceTolerance` (which is ``NO_INCREASE`` - any rise needs consent).
    There is deliberately no looser hidden default.
    """
    if len(selection.offers) > MAX_OFFERS_PER_REVALIDATION:
        raise RevalidationLimitExceeded(
            f"{len(selection.offers)} offers exceeds the per-revalidation "
            f"ceiling of {MAX_OFFERS_PER_REVALIDATION}"
        )
    tolerance = tolerance or PriceTolerance()
    now = now or datetime.now(timezone.utc)

    revalidated = tuple(
        _revalidate_one(offer, duffel=duffel, tolerance=tolerance, now=now)
        for offer in selection.offers
    )

    required = tuple(
        r for r, o in zip(revalidated, selection.offers) if o.required
    )
    all_bookable = all(r.status.is_bookable for r in required)

    # The itinerary total the traveller saw includes accommodation and
    # transfers, which Phase 3 does not re-price. So the current total is the
    # discovered total with only the revalidated *flight* movement applied -
    # the non-flight components are carried forward, the way recheck.py carries
    # a transfer. A total is only meaningful when every offer was re-priced.
    if all_bookable and all(r.current_amount is not None for r in revalidated):
        flight_delta = sum(
            (r.current_amount - r.discovered_amount) for r in revalidated
        )
        current_total = round(selection.discovered_total + flight_delta, 2)
    else:
        current_total = None

    status = _itinerary_status(revalidated, all_bookable=all_bookable)

    return OfferRevalidationResult(
        selection_id=selection.selection_id,
        status=status,
        bookable=all_bookable,
        discovered_total=selection.discovered_total,
        current_total=current_total,
        currency=selection.currency,
        tolerance=tolerance,
        offers=revalidated,
        checked_at=now,
    )


def _itinerary_status(
    offers: tuple[RevalidatedOffer, ...], *, all_bookable: bool
) -> RevalidationStatus:
    """Fold the per-offer results into one verdict.

    Order matters: a currency change or an unbookable required offer stops the
    itinerary outright, before price movement is even considered.
    """
    if not all_bookable:
        return RevalidationStatus.NOT_BOOKABLE
    if any(c.field == "currency" for o in offers for c in o.changes):
        return RevalidationStatus.NOT_BOOKABLE

    all_changes = [c for o in offers for c in o.changes]
    if any(c.severity is ChangeSeverity.BLOCKING for c in all_changes):
        return RevalidationStatus.USER_RECONFIRMATION_REQUIRED
    if any(c.severity is ChangeSeverity.MINOR for c in all_changes):
        return RevalidationStatus.READY_WITH_MINOR_CHANGE
    return RevalidationStatus.READY
