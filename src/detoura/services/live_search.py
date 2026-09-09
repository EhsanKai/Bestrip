"""A real Duffel-backed search that leaves a revalidatable trail (V8 Phase 3).

Phase 2's `acquire_real_supply` produces a snapshot; the planner searches it and
returns recommendations. What was missing for Phase 3 is the bridge to booking:
each recommendation's legs carry a Duffel `offer_id`, but that id is internal
and never reaches a client. So this module records, per recommendation, exactly
the offers behind it - id, discovered price, discovered terms - in the
`SelectionStore`, and hands back an opaque `selection_id` the client can send to
`/api/v1/trips/revalidate`.

Nothing here changes search: it calls the same `acquire_real_supply` and the
same `TravelPlanner`, then reads the result. Ranking, scoring and the
zero-network-beam invariant are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

from ..config import PlannerConfig
from ..models.destination import Destination
from ..models.itinerary import Itinerary
from ..models.trip import TripRequest
from ..providers.cache import ExpiringProviderCache
from ..providers.duffel import DuffelTransportProvider
from ..search_modes import SearchMode, apply_mode
from .acquisition import ProviderCallBudget, SnapshotTransportProvider
from .planner import TravelPlanner
from .real_supply import RealSupplyResult, acquire_real_supply
from .selection_store import SelectedOffer, SelectionStore


@dataclass(slots=True)
class LiveSearchResult:
    recommendations: list[Itinerary]
    supply: RealSupplyResult
    selection_ids: dict[str, str]
    """recommendation id -> selection id, for the ones that could be recorded."""


def _selected_offers_for(trip: Itinerary, travelers: int) -> list[SelectedOffer] | None:
    """Every bookable Duffel offer behind one recommendation, or ``None``.

    ``None`` when any leg has no provider reference - a trip we cannot honestly
    promise to revalidate should not get a selection id at all.
    """
    offers: list[SelectedOffer] = []
    for leg in trip.legs:
        ref = leg.provider_ref
        if ref is None or ref.provider != "duffel" or not ref.offer_id:
            return None
        offers.append(SelectedOffer(
            offer_id=ref.offer_id,
            provider="duffel",
            origin=leg.origin,
            destination=leg.destination,
            leg_label=f"{leg.origin} → {leg.destination}",
            travelers=max(travelers, 1),
            discovered_amount=round(leg.price_per_person * max(travelers, 1), 2),
            discovered_currency="EUR",
            discovered_raw_amount=ref.quoted_amount,
            discovered_raw_currency=ref.quoted_currency,
            discovered_baggage_cabin=leg.baggage.cabin_bag.status if leg.baggage else None,
            discovered_baggage_checked=leg.baggage.checked_bag.status if leg.baggage else None,
            discovered_hold_supported=ref.hold_supported,
            discovered_expires_at=ref.expires_at.isoformat() if ref.expires_at else None,
            discovered_departure=leg.departure,
            discovered_arrival=leg.arrival,
        ))
    return offers


def live_search(
    request: TripRequest,
    *,
    duffel: DuffelTransportProvider,
    selection_store: SelectionStore,
    destinations: Sequence[Destination],
    airports: Sequence[str],
    days: Sequence[date],
    mode: SearchMode = SearchMode.SMART,
    budget: ProviderCallBudget | None = None,
    cache: ExpiringProviderCache | None = None,
    config: PlannerConfig | None = None,
) -> LiveSearchResult:
    """Run a real Duffel-backed search and record each recommendation's offers."""
    supply = acquire_real_supply(
        request, duffel=duffel, destinations=destinations,
        airports=airports, days=days, budget=budget, cache=cache,
    )
    served = SnapshotTransportProvider(supply.snapshot, travelers=max(request.travelers, 1))
    planner = TravelPlanner(
        config=apply_mode(config or PlannerConfig(), mode),
        transport_provider=served,
    )
    result = planner.plan(request)

    selection_ids: dict[str, str] = {}
    for rank, trip in enumerate(result.recommendations):
        rec_id = f"{supply.snapshot.generated_at.timestamp():.0f}-{rank}"
        offers = _selected_offers_for(trip, request.travelers)
        if not offers:
            continue
        selection_ids[rec_id] = selection_store.record(
            recommendation_id=rec_id,
            trip_label=trip.route_label() if hasattr(trip, "route_label") else "",
            currency="EUR",
            discovered_total=trip.total_cost,
            offers=tuple(offers),
        )

    return LiveSearchResult(
        recommendations=list(result.recommendations),
        supply=supply,
        selection_ids=selection_ids,
    )
