"""The final Recommendation Portfolio: value + diminishing-returns cheapness
+ diversified reranking (V9 Phase 3 §C, §D).

This is the step the pipeline diagram calls "Zero-network Optimizer ->
Recommendation Portfolio" — everything here runs on already-acquired data
(a candidate's observed price, its attractiveness profile, its user-fit and
trip-quality scores) and makes **zero provider network calls** (§A1, tested
in ``test_v9_phase3_portfolio.py``).

## Why this exists (the Cologne problem)

A ranked list is a portfolio, not the first N rows of a score sort (§C1).
Sorting purely by price (or even by a single "value" scalar) from an origin
like Cologne can fill the Top 10 with ten cheap, geographically close,
experientially similar German/Benelux cities — mathematically cheap,
product-wise poor. This module treats "cheap" as one diminishing-returns
input among several (§D1), and reranks the sorted candidates with a bounded,
deterministic, greedy pass that discounts a candidate's value by how
redundant it is — geographically and experientially — against what has
already been selected (§C2-C6). Nothing here is a hard ban: an exceptional
nearby deal can still win a slot (§C5), and country diversity is a
diagnostic, never a quota (§C7).

## Six components, never collapsed unexplained (§D)

    1. market_opportunity   "does this look unusually good economically?"
    2. attractiveness       "is this destination genuinely worth considering?"
    3. user_fit             "does it match this traveler?"
    4. trip_quality         "is the actual itinerary good?"
    5. novelty              "is this an interesting discovery?"
    6. (applied during selection, not in the base value) portfolio diversity

A single scalar (``base_value``) drives ranking, but every component and
every diversity penalty survives into :class:`PortfolioDecision` for the
explainability trace (§D2) — nothing is a magic unexplained number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..models.attractiveness import DestinationAttractivenessProfile
from ..models.destination import Destination
from ..models.itinerary import Itinerary
from ..models.trip import TripRequest
from ..search_intel_config import SearchIntelConfig, search_intel_config
from .experience_similarity import experience_redundancy_signal
from .geo import geo_redundancy_signal


def cheapness_value(
    price: float, *, floor: float, ceiling: float, gamma: float,
) -> float:
    """Bounded, nonlinear price-value curve (§D1).

    ``t`` is where ``price`` sits between the field's cheapest (``floor``,
    value 1.0) and priciest (``ceiling``, value 0.0) observed price, linear in
    price. Raising it to ``gamma > 1`` compresses the *cheap end*: because
    ``t`` is closer to 1 for cheap prices, and ``t**gamma`` grows more slowly
    than ``t`` as ``t`` approaches 1 for ``gamma > 1``, the same absolute
    price gap is worth **more** value near the expensive end of the field
    than near the cheap end — a EUR30-vs-EUR15 gap swings this more than a
    EUR10-vs-EUR5 gap of the same 2x ratio, matching the product principle
    that a EUR5 saving among already-cheap options should rarely by itself
    outweigh a real attractiveness/diversity difference. See
    ``docs/V9_PHASE3_*.md`` for the benchmarked comparison against the plain
    linear (``gamma=1``) treatment.

    ``floor == ceiling`` (every candidate the same price) returns the neutral
    ``1.0`` for all of them - price cannot inform a ranking with no
    variation, so it should not be able to.
    """
    if ceiling <= floor:
        return 1.0
    t = max(0.0, min(1.0, (ceiling - price) / (ceiling - floor)))
    return round(t ** max(1.0, gamma), 6)


@dataclass(frozen=True, slots=True)
class PortfolioCandidate:
    """Everything the final value/diversification pass needs about one
    candidate recommendation. One candidate = one destination (the common
    Detoura short-trip case); a multi-city itinerary is represented by its
    primary/first destination."""

    destination_id: str
    price_per_person: float
    market_opportunity: float | None = None
    """[0, 1] — from the acquisition-stage OpportunityScore (freshness,
    confidence, historical contribution) - "was this worth acquiring", not
    "is it cheap" (price is handled separately, see ``cheapness_value``)."""
    attractiveness_score: float | None = None
    """[0, 100] — DestinationAttractivenessProfile.aggregate_score."""
    user_fit: float | None = None
    """[0, 1] — preference_affinity for *this* traveler. Kept separate from
    ``attractiveness_score`` (§B5) all the way through."""
    trip_quality: float | None = None
    """[0, 1] — the planner's own TravelValueBreakdown.total, when a real
    itinerary is available. Neutral when the caller has none (e.g. a
    destination-only benchmark fixture)."""
    novelty: float | None = None
    """[0, 1] — how much of a fresh discovery this is (derived from
    LIVE/PRIOR/UNKNOWN knowledge upstream — UNKNOWN scores highest)."""
    pre_rank: int = 0


@dataclass(frozen=True, slots=True)
class PortfolioDecision:
    """One candidate's full explanation (§D2) — Ops/debug only, never sent to
    a consumer."""

    destination_id: str
    price_per_person: float
    components: dict = field(default_factory=dict)
    base_value: float = 0.0
    geo_redundancy_penalty: float = 0.0
    experience_redundancy_penalty: float = 0.0
    adjusted_value: float = 0.0
    selected: bool = False
    final_rank: int | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PortfolioResult:
    selected: list[PortfolioDecision]
    rejected: list[PortfolioDecision]
    metrics: dict = field(default_factory=dict)

    @property
    def all_decisions(self) -> list[PortfolioDecision]:
        return self.selected + self.rejected


def _component(value: float | None, *, scale_100: bool = False) -> float:
    if value is None:
        return 0.5
    v = value / 100.0 if scale_100 else value
    return max(0.0, min(1.0, v))


def base_value(
    candidate: PortfolioCandidate, *, price_floor: float, price_ceiling: float,
    cfg: SearchIntelConfig,
) -> tuple[float, dict]:
    """The un-diversified recommendation value (§D) — components 1-5."""
    price_c = cheapness_value(
        candidate.price_per_person, floor=price_floor, ceiling=price_ceiling,
        gamma=cfg.cheapness_curve_gamma,
    )
    attract_c = _component(candidate.attractiveness_score, scale_100=True)
    fit_c = _component(candidate.user_fit)
    opp_c = _component(candidate.market_opportunity)
    quality_c = _component(candidate.trip_quality)
    novelty_c = _component(candidate.novelty)

    weights = (
        cfg.value_weight_price, cfg.value_weight_attractiveness,
        cfg.value_weight_user_fit, cfg.value_weight_opportunity,
        cfg.value_weight_trip_quality, cfg.value_weight_novelty,
    )
    wsum = sum(weights) or 1.0
    values = (price_c, attract_c, fit_c, opp_c, quality_c, novelty_c)
    total = sum(w * v for w, v in zip(weights, values)) / wsum
    return round(max(0.0, min(1.0, total)), 6), {
        "price": price_c, "attractiveness": attract_c, "user_fit": fit_c,
        "market_opportunity": opp_c, "trip_quality": quality_c, "novelty": novelty_c,
    }


def select_portfolio(
    candidates: list[PortfolioCandidate],
    destinations_by_id: dict[str, Destination],
    *,
    size: int | None = None,
    cfg: SearchIntelConfig | None = None,
) -> PortfolioResult:
    """Greedy, deterministic diversified selection (§C4).

    At each step, picks the remaining candidate with the highest
    *diversity-adjusted* value — ``base_value * (1 - geo_penalty) * (1 -
    experience_penalty)`` — where both penalties are computed against the
    set already selected so far and are individually capped at their
    configured scale (never able to zero out an exceptional candidate, §C5).
    Ties broken by ``pre_rank`` for determinism.
    """
    cfg = cfg or search_intel_config()
    size = size or cfg.portfolio_size
    if not candidates:
        return PortfolioResult(selected=[], rejected=[], metrics={})

    prices = [c.price_per_person for c in candidates]
    price_floor, price_ceiling = min(prices), max(prices)

    pending: dict[str, tuple[PortfolioCandidate, float, dict]] = {}
    for c in candidates:
        bv, comps = base_value(c, price_floor=price_floor, price_ceiling=price_ceiling, cfg=cfg)
        pending[c.destination_id] = (c, bv, comps)

    selected: list[PortfolioDecision] = []
    selected_destinations: list[Destination] = []
    remaining_ids = set(pending)

    while remaining_ids and len(selected) < size:
        best_id = None
        best_adjusted = -1.0
        best_geo_pen = best_exp_pen = 0.0
        # `destination_id` (`x`) is a required secondary sort key, not
        # cosmetic: without it, a tie between `pre_rank` values (default 0
        # for every candidate at several call sites, including a fixture in
        # this module's own test suite) resolves via `remaining_ids`'s set
        # iteration order, which depends on Python's per-process string-hash
        # seed — the same input candidates could select a genuinely
        # different destination across otherwise-identical runs/processes.
        # Found by independent adversarial QA; this line is the fix.
        for did in sorted(remaining_ids, key=lambda x: (pending[x][0].pre_rank, x)):
            cand, bv, _comps = pending[did]
            dest = destinations_by_id.get(did)
            if dest is not None and selected_destinations:
                geo_sig = geo_redundancy_signal(
                    dest, selected_destinations, km_scale=cfg.geo_redundancy_km_scale,
                    free_allowance=cfg.geo_redundancy_free_allowance,
                )
                exp_sig = experience_redundancy_signal(
                    dest, selected_destinations,
                    similarity_threshold=cfg.experience_similarity_threshold,
                )
            else:
                geo_sig = exp_sig = 0.0
            geo_pen = cfg.portfolio_geo_penalty_scale * geo_sig
            exp_pen = cfg.portfolio_experience_penalty_scale * exp_sig
            adjusted = bv * (1.0 - geo_pen) * (1.0 - exp_pen)
            if adjusted > best_adjusted:
                best_id, best_adjusted = did, adjusted
                best_geo_pen, best_exp_pen = geo_pen, exp_pen

        cand, bv, comps = pending[best_id]
        dest = destinations_by_id.get(best_id)
        reason_bits = [f"value={bv:.3f}"]
        if best_geo_pen > 0:
            reason_bits.append(f"geo_penalty={best_geo_pen:.3f}")
        if best_exp_pen > 0:
            reason_bits.append(f"experience_penalty={best_exp_pen:.3f}")
        selected.append(PortfolioDecision(
            destination_id=best_id, price_per_person=cand.price_per_person,
            components=comps, base_value=bv,
            geo_redundancy_penalty=round(best_geo_pen, 4),
            experience_redundancy_penalty=round(best_exp_pen, 4),
            adjusted_value=round(best_adjusted, 6), selected=True,
            final_rank=len(selected) + 1, reason=", ".join(reason_bits),
        ))
        if dest is not None:
            selected_destinations.append(dest)
        remaining_ids.discard(best_id)

    rejected: list[PortfolioDecision] = []
    # Iterate a stable, sorted order (never the raw set) — `remaining_ids`'s
    # own iteration order depends on Python's per-process string-hash seed,
    # and building this list in that order previously made a tie in
    # `base_value` resolve differently across processes/runs even with
    # byte-identical input (found by independent adversarial QA).
    for did in sorted(remaining_ids):
        cand, bv, comps = pending[did]
        rejected.append(PortfolioDecision(
            destination_id=did, price_per_person=cand.price_per_person,
            components=comps, base_value=bv, adjusted_value=bv, selected=False,
            reason="not selected — outside portfolio size after diversification",
        ))
    rejected.sort(key=lambda d: (-d.base_value, d.destination_id))

    sel_destinations = [destinations_by_id[d.destination_id] for d in selected
                        if d.destination_id in destinations_by_id]
    from .experience_similarity import mean_pairwise_experience_similarity
    from .geo import country_diversity_ratio, mean_pairwise_distance_km

    metrics = {
        "size_selected": len(selected),
        "mean_attractiveness": (
            round(sum(pending[d.destination_id][2]["attractiveness"] for d in selected) * 100 / len(selected), 2)
            if selected else None
        ),
        "mean_price": round(sum(d.price_per_person for d in selected) / len(selected), 2) if selected else None,
        "country_diversity_ratio": country_diversity_ratio(sel_destinations),
        "mean_pairwise_distance_km": mean_pairwise_distance_km(sel_destinations),
        "mean_pairwise_experience_similarity": mean_pairwise_experience_similarity(sel_destinations),
    }
    return PortfolioResult(selected=selected, rejected=rejected, metrics=metrics)


# ==========================================================================
# Adapter: live Itinerary recommendations -> PortfolioCandidate (optional,
# additive integration seam for `services.live_search`, §D2).
# ==========================================================================
def candidates_from_itineraries(
    itineraries: Sequence[Itinerary],
    *,
    request: TripRequest,
    destinations_by_id: dict[str, Destination],
    attractiveness_by_id: dict[str, DestinationAttractivenessProfile],
    opportunity_by_dest_id: dict[str, float] | None = None,
    novelty_by_dest_id: dict[str, float] | None = None,
) -> list[PortfolioCandidate] | None:
    """Build portfolio candidates from real planner output, or ``None`` when
    the reranking pass does not apply.

    Deliberately conservative: returns ``None`` (meaning "leave the
    planner's own ranking untouched") whenever a recommendation is not a
    clean single-destination trip, or names a destination this catalog does
    not recognise — a multi-city itinerary has no single "primary
    destination" to diversify against, and this pass must never guess. The
    common Detoura short-trip discovery search (one destination per
    recommendation) is exactly the case §C1-§C6 describe.

    ``opportunity_by_dest_id``/``novelty_by_dest_id`` (fix for a finding from
    independent adversarial QA): the caller's real, per-destination
    acquisition-stage signals — ``services.live_search`` threads through the
    same ``CandidateDecision.baseline_score``/knowledge the funnel already
    computed for this exact search, rather than leaving every candidate's
    ``market_opportunity``/``novelty`` at the neutral "unknown" default. QA
    found that leaving these permanently ``None`` (this function's original
    behaviour) let a candidate win a slot on attractiveness+user_fit alone
    against a genuinely much better-priced rival, because two of the six
    value components were *always* neutral for every real candidate, not
    just for ones genuinely lacking that information. Omitting these
    parameters preserves that neutral fallback for a caller that truly has
    no such signal (e.g. a destination-only benchmark).
    """
    from .acquisition import preference_affinity

    opportunity_by_dest_id = opportunity_by_dest_id or {}
    novelty_by_dest_id = novelty_by_dest_id or {}

    out: list[PortfolioCandidate] = []
    for rank, it in enumerate(itineraries):
        if len(it.cities) != 1:
            return None
        dest_id = it.cities[0]
        dest = destinations_by_id.get(dest_id)
        if dest is None:
            return None
        profile = attractiveness_by_id.get(dest_id)
        attractiveness = profile.aggregate_score if (profile and profile.is_known) else None
        fit = preference_affinity([dest], request).get(dest_id)
        quality = it.score if 0.0 <= it.score <= 1.0 else None
        travelers = max(request.travelers, 1)
        out.append(PortfolioCandidate(
            destination_id=dest_id,
            price_per_person=round(it.total_cost / travelers, 2),
            market_opportunity=opportunity_by_dest_id.get(dest_id),
            attractiveness_score=attractiveness,
            user_fit=fit, trip_quality=quality,
            novelty=novelty_by_dest_id.get(dest_id), pre_rank=rank,
        ))
    return out


def apply_portfolio_to_itineraries(
    itineraries: Sequence[Itinerary], result: PortfolioResult,
) -> list[Itinerary]:
    """Reorder ``itineraries`` to match ``result.selected`` and renumber
    ``rank`` 1..N accordingly — the total-order contract every downstream
    consumer (``SearchIntelRecorder.attribute``, Ops) relies on."""
    by_dest = {it.cities[0]: it for it in itineraries if len(it.cities) == 1}
    out: list[Itinerary] = []
    for decision in result.selected:
        it = by_dest.get(decision.destination_id)
        if it is None:
            continue
        out.append(it.model_copy(update={"rank": len(out) + 1}))
    return out
