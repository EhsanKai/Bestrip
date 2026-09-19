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

from dataclasses import asdict, dataclass
from datetime import date
from typing import Sequence

from ..config import PlannerConfig
from ..models.attractiveness import DestinationAttractivenessProfile
from ..models.destination import Destination
from ..models.itinerary import Itinerary, PlanResult
from ..models.trip import TripRequest
from ..persistence.db import Database
from ..providers.cache import ExpiringProviderCache
from ..providers.duffel import DuffelTransportProvider
from ..search_intel_config import SearchIntelConfig, search_intel_config
from ..search_modes import SearchMode, apply_mode
from .acquisition import ProviderCallBudget, SnapshotTransportProvider
from .planner import TravelPlanner
from .portfolio import apply_portfolio_to_itineraries, candidates_from_itineraries, select_portfolio
from .real_supply import RealSupplyResult, acquire_real_supply
from .selection_store import SelectedOffer, SelectionStore


@dataclass(slots=True)
class LiveSearchResult:
    recommendations: list[Itinerary]
    supply: RealSupplyResult
    selection_ids: dict[int, str]
    """Index into ``recommendations`` -> selection id, for the ones that
    could be recorded. Keyed by position rather than any recomputed string
    id, so it survives unchanged regardless of how a caller later builds a
    client-facing recommendation id from the itinerary (see
    ``api.assembler.build_response``, which iterates this same list in this
    same order)."""
    search_trace: object | None = None
    """The persisted :class:`SearchIntelligenceTrace` when a recorder was
    supplied (V9 Phase 1), else ``None``."""
    plan_result: PlanResult | None = None
    """The underlying :class:`~detoura.models.itinerary.PlanResult` this
    search actually produced (V9 Post-Phase-6 Search Integration) - carries
    the same algorithm metadata (beam rounds, Pareto frontier size, etc.) a
    synthetic search's result does, so a consumer-facing API response can be
    assembled identically either way (``api.assembler.build_response``
    expects exactly this shape). ``None`` only if this dataclass is
    constructed by hand outside :func:`live_search` itself."""


def _novelty_from_knowledge(knowledge: str) -> float:
    """How much of a fresh discovery this destination is (§D "novelty").
    UNKNOWN (no live/prior history at all) is the most novel; a market with
    confirmed LIVE history is the least — the traveler has, in effect,
    already been "told about" a well-established market."""
    return {"UNKNOWN": 0.9, "PRIOR": 0.5, "LIVE": 0.3}.get(knowledge, 0.5)


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
    recorder=None,
    portfolio_db: Database | None = None,
    portfolio_cfg: SearchIntelConfig | None = None,
    origin_resolver=None,
) -> LiveSearchResult:
    """Run a real Duffel-backed search and record each recommendation's offers.

    ``recorder`` (V9 Phase 1): an optional ``SearchIntelRecorder``. When given,
    the search teaches Detoura something — candidate scoring, per-call
    observation, contribution attribution and a persisted trace — without
    changing ranking, pricing or the zero-network-beam invariant.

    ``portfolio_db`` (V9 Phase 3 §C/§D): an optional database to read
    :class:`~detoura.models.attractiveness.DestinationAttractivenessProfile`
    profiles from. When given, the planner's ranked recommendations are
    passed through the final Recommendation Portfolio reranker
    (``services.portfolio``) — attractiveness, user fit and diminishing-
    returns cheapness, then a bounded geo/experience-diversity pass — before
    attribution and the trace are built, so both reflect the *actual* final
    order shown to the traveler. **Opt-in and additive**: omitting it (the
    default) reproduces the exact pre-Phase-3 ranking, unchanged, and every
    existing caller of this function is unaffected. It is also a no-op
    whenever the search is not a clean single-destination-per-recommendation
    discovery search (see ``portfolio.candidates_from_itineraries``) — a
    multi-city itinerary's own ranking is never second-guessed. Still makes
    zero provider network calls (§A1) — everything it reads is already in
    ``result.recommendations`` or the database.

    ``origin_resolver`` (V9 Post-Phase-6 Search Integration): the resolver
    the internal planner uses to validate ``request.origin`` and label its
    response. Omitting it (the default, preserving every existing caller's
    behaviour unchanged) falls back to ``TravelPlanner``'s own default
    (:class:`~detoura.services.origin_resolver.StaticOriginResolver`, the
    closed 5-airport table) - a caller resolving origins against the wider
    catalog (:class:`~detoura.services.origin_resolver.CatalogOriginResolver`)
    for the *candidate* ``airports`` above should pass the same resolver
    here, or this function's own internal validation would reject an origin
    the caller already accepted.
    """
    supply = acquire_real_supply(
        request, duffel=duffel, destinations=destinations,
        airports=airports, days=days, budget=budget, cache=cache,
        recorder=recorder,
    )
    served = SnapshotTransportProvider(supply.snapshot, travelers=max(request.travelers, 1))
    planner = TravelPlanner(
        config=apply_mode(config or PlannerConfig(), mode),
        transport_provider=served,
        origin_resolver=origin_resolver,
    )
    result = planner.plan(request)
    portfolio_metrics: dict = {}
    portfolio_decisions: tuple[dict, ...] = ()

    if portfolio_db is not None and result.recommendations:
        from ..persistence import attractiveness as attractiveness_store

        cfg = portfolio_cfg or search_intel_config()
        destinations_by_id = {d.id: d for d in destinations}
        attractiveness_by_id: dict[str, DestinationAttractivenessProfile] = (
            attractiveness_store.batch_get_profiles(
                portfolio_db,
                [it.cities[0] for it in result.recommendations if len(it.cities) == 1],
                model_version=cfg.attractiveness_model_version,
            )
        )
        # Real per-destination acquisition-stage signals for this exact
        # search, when a recorder ran the funnel — fix for an independent
        # adversarial QA finding: leaving market_opportunity/novelty
        # permanently at the neutral "unknown" default let attractiveness +
        # user_fit alone win a slot against a genuinely better-priced rival,
        # because two of the six value components were *always* neutral for
        # every real candidate. `CandidateDecision.baseline_score` is the
        # same Opportunity score (§A) the funnel used to decide whether this
        # destination was worth a provider call in the first place.
        opportunity_by_dest_id: dict[str, float] = {}
        novelty_by_dest_id: dict[str, float] = {}
        for cd in getattr(recorder, "_candidates", []):
            dest_id = cd.market.rpartition("→")[2]
            if not dest_id:
                continue
            if cd.baseline_score is not None:
                opportunity_by_dest_id[dest_id] = cd.baseline_score
            novelty_by_dest_id[dest_id] = _novelty_from_knowledge(
                cd.score_components.get("knowledge", "UNKNOWN")
            )

        candidates = candidates_from_itineraries(
            result.recommendations, request=request,
            destinations_by_id=destinations_by_id,
            attractiveness_by_id=attractiveness_by_id,
            opportunity_by_dest_id=opportunity_by_dest_id,
            novelty_by_dest_id=novelty_by_dest_id,
        )
        if candidates is not None:
            portfolio_result = select_portfolio(
                candidates, destinations_by_id, cfg=cfg,
            )
            reranked = apply_portfolio_to_itineraries(result.recommendations, portfolio_result)
            if reranked:
                result = result.model_copy(update={"recommendations": reranked})
            portfolio_metrics = portfolio_result.metrics
            portfolio_decisions = tuple(asdict(d) for d in portfolio_result.all_decisions)

    trace = None
    if recorder is not None and getattr(recorder, "enabled", False):
        recorder.attribute(result.recommendations)
        m = supply.metrics
        trace = recorder.finalize(
            cache_hits=m.cache_hits, cache_misses=m.cache_misses,
            provider_calls_used=m.provider_calls,
            recommendations_produced=len(result.recommendations),
            portfolio=portfolio_decisions, portfolio_metrics=portfolio_metrics,
        )

    selection_ids: dict[int, str] = {}
    for rank, trip in enumerate(result.recommendations):
        rec_id = f"{supply.snapshot.generated_at.timestamp():.0f}-{rank}"
        offers = _selected_offers_for(trip, request.travelers)
        if not offers:
            continue
        selection_ids[rank] = selection_store.record(
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
        search_trace=trace,
        plan_result=result,
    )
