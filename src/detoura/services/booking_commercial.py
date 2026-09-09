"""Glue between a live ``BookingRun`` and the commercial engine (V8.5).

Keeps :mod:`detoura.services.booking_flow` free of pricing concerns. Two jobs:

* ``price_run`` - (re)compute the customer-facing quote for a run from its
  current supplier costs, the chosen tier and an optional promo code. Called
  when the intent is created, when the customer changes tier/promo, and again
  at confirmation (a promo can lapse between review and confirm).
* ``finalize_economics`` - once, when the run reaches a terminal phase, write
  the immutable ledger row and record the promo redemption.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models.commercial import ServiceTier
from ..models.promo import PromoRedemption
from ..persistence import economics, promos
from ..persistence.db import Database
from .booking_orchestrator import BookingPhase, BookingRun
from .commercial import CommercialPricingService, QuoteResult

_TERMINAL = {
    BookingPhase.COMPLETE,
    BookingPhase.PARTIAL_FAILURE,
    BookingPhase.FAILED,
}


def _supplier_transport(run: BookingRun) -> float:
    """The run already computed its discovered total correctly for both the
    selection and demo paths; that is the supplier transport cost."""
    return float(run.discovered_total)


def price_run(
    run: BookingRun,
    db: Database,
    *,
    service_tier: ServiceTier | None = None,
    promo_code: str | None = None,
    user_key: str | None = None,
) -> QuoteResult:
    if service_tier is not None:
        run.service_tier = service_tier
    if promo_code is not None:
        run.requested_promo = promo_code.strip().upper() or None
    if user_key is not None:
        run.user_key = user_key

    service = CommercialPricingService(db)
    result = service.quote(
        supplier_transport=_supplier_transport(run),
        currency=run.currency,
        ticket_count=len(run.items),
        service_tier=run.service_tier,
        promo_code=run.requested_promo,
        user_key=run.user_key,
    )
    with run._lock:
        run.quote = result.quote
    return result


def finalize_economics(run: BookingRun, db: Database) -> bool:
    """Write the ledger row + promo redemption exactly once. Safe to call on
    every poll after the run finishes."""
    if run.economics_written or run.phase not in _TERMINAL or run.quote is None:
        return False

    snapshot = {
        "booking_id": run.booking_id,
        "journey_reference": run.journey_reference,
        "mode": run.mode.value,
        "phase": run.phase.value,
        "trip_label": run.trip_label,
        "route_cities": list(run.route_cities),
        "service_tier": run.service_tier.value,
        "items": [
            {
                "sequence": idx,
                "origin": i.origin_city,
                "destination": i.destination_city,
                "state": i.state.value,
                "quoted_price": i.quoted_price,
                "current_price": i.current_price,
                "provider": i.provider,
                "provider_order_id": i.provider_order_id,
            }
            for idx, i in enumerate(run.items, start=1)
        ],
    }

    wrote = economics.write_snapshot(
        db,
        booking_id=run.booking_id,
        journey_reference=run.journey_reference,
        quote=run.quote,
        snapshot=snapshot,
    )

    if run.quote.promo_code and run.quote.promo_discount > 0:
        promos.record_redemption(
            db,
            PromoRedemption(
                code=run.quote.promo_code,
                booking_id=run.booking_id,
                user_key=run.user_key,
                discount_amount=run.quote.promo_discount,
                currency=run.currency,
                redeemed_at=datetime.now(timezone.utc),
            ),
        )

    run.economics_written = True
    return wrote
