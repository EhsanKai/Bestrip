"""Provider search economics, kept out of business logic (V9 Phase 1).

A provider (Duffel) can bill *excess* searches once a rolling search-to-book
ratio is exceeded. The terms are configuration (:class:`ProviderEconomicsConfig`,
from env) so they can change without touching acquisition. Every figure that
depends on an unconfigured term is **UNKNOWN** — returned as ``None``, never as
``0``.

This module does not decide anything; it only measures.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.money import to_minor_units
from ..persistence.db import Database
from ..search_intel_config import ProviderEconomicsConfig, search_intel_config


@dataclass(frozen=True, slots=True)
class SearchEconomics:
    provider: str
    provider_searches: int
    bookings_attributable: int | None
    search_to_book_ratio: float | None
    included_search_allowance: float | None
    estimated_excess_searches: float | None
    estimated_excess_search_cost_minor: int | None
    currency: str
    configured: bool

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "provider_searches": self.provider_searches,
            "bookings_attributable": self.bookings_attributable,
            "search_to_book_ratio": self.search_to_book_ratio,
            "included_search_allowance": self.included_search_allowance,
            "estimated_excess_searches": self.estimated_excess_searches,
            "estimated_excess_search_cost": (
                None
                if self.estimated_excess_search_cost_minor is None
                else round(self.estimated_excess_search_cost_minor / 100.0, 2)
            ),
            "currency": self.currency,
            "configured": self.configured,
        }


def _bookings_for_provider(db: Database, provider: str) -> int:
    row = db.query_one(
        "SELECT COUNT(DISTINCT b.booking_id) AS n "
        "FROM bookings b JOIN booking_items i ON i.booking_id = b.booking_id "
        "WHERE i.provider = ?",
        (provider,),
    )
    return int(row["n"]) if row and row["n"] is not None else 0


def _provider_searches(db: Database, provider: str) -> int:
    row = db.query_one(
        "SELECT COALESCE(SUM(provider_calls_used), 0) AS n "
        "FROM search_traces WHERE provider = ?",
        (provider,),
    )
    return int(row["n"]) if row and row["n"] is not None else 0


def estimate_for_search(
    *, provider_calls_this_search: int, cfg: ProviderEconomicsConfig | None = None,
) -> tuple[float | None, int | None, bool]:
    """Excess searches + cost for one search in isolation.

    Returns ``(estimated_excess_searches, cost_minor, configured)``. When the
    per-booking allowance model applies, one search on its own has no booking
    to amortise against, so excess is the calls beyond a flat allowance if one
    is configured, else UNKNOWN.
    """
    cfg = cfg or search_intel_config().economics
    if not cfg.is_configured:
        return None, None, False
    allowance = cfg.included_searches_flat
    if allowance is None:
        # only a per-booking allowance is set — a single search cannot be
        # judged without the booking context.
        return None, None, True
    excess = max(0.0, provider_calls_this_search - allowance)
    cost_minor = (
        to_minor_units(round(excess * (cfg.excess_search_fee or 0.0), 2))
        if cfg.excess_search_fee is not None else None
    )
    return excess, cost_minor, True


def rolling_economics(
    db: Database, *, cfg: ProviderEconomicsConfig | None = None,
) -> SearchEconomics:
    """Provider-wide economics from the persisted traces + bookings."""
    cfg = cfg or search_intel_config().economics
    provider = cfg.provider
    searches = _provider_searches(db, provider)
    bookings = _bookings_for_provider(db, provider)

    ratio = round(searches / bookings, 2) if bookings else None

    if not cfg.is_configured:
        return SearchEconomics(
            provider=provider, provider_searches=searches,
            bookings_attributable=bookings, search_to_book_ratio=ratio,
            included_search_allowance=None, estimated_excess_searches=None,
            estimated_excess_search_cost_minor=None, currency=cfg.currency,
            configured=False,
        )

    allowance: float | None = None
    if cfg.included_searches_per_booking is not None and bookings:
        allowance = cfg.included_searches_per_booking * bookings
    elif cfg.included_searches_flat is not None:
        allowance = float(cfg.included_searches_flat)

    if allowance is None:
        # configured, but no booking yet to amortise a per-booking allowance
        excess = None
        cost_minor = None
    else:
        excess = max(0.0, searches - allowance)
        cost_minor = (
            to_minor_units(round(excess * (cfg.excess_search_fee or 0.0), 2))
            if cfg.excess_search_fee is not None else None
        )

    return SearchEconomics(
        provider=provider, provider_searches=searches,
        bookings_attributable=bookings, search_to_book_ratio=ratio,
        included_search_allowance=allowance,
        estimated_excess_searches=excess,
        estimated_excess_search_cost_minor=cost_minor,
        currency=cfg.currency, configured=True,
    )
