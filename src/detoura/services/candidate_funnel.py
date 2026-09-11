"""The V9 Phase 2 candidate funnel (§20).

    ~200 catalog destinations
      → hard eligibility        (enabled, acquisition_eligible, has airport, not avoided)
      → cheap feasibility       (resolvable airport, not the origin, plausible duration)
      → Market Prior + Price Memory batch lookup   (bounded queries, no N+1)
      → Market Opportunity scoring
      → diversity / cold-start handling
      → EXPLOIT / EXPLORE allocation  (Phase 1 allocate — EXPLORE floor preserved)
      → bounded shortlist
      → (caller builds a bounded acquisition plan → Duffel)

Every stage count is recorded on a :class:`FunnelTrace`. This module chooses
*which* markets to acquire; it never changes *how many* — the hard
``ProviderCallBudget`` still binds downstream and the inter-city / date fan-out
is constructed only among the small shortlist (§24, §25).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from ..models.destination import Destination
from ..models.market_prior import PriorConfidence
from ..models.search_intel import AcquisitionStance, MarketConfidence, MarketKey
from ..models.trip import TripRequest
from ..persistence.db import Database
from ..search_intel_config import SearchIntelConfig, search_intel_config
from .acquisition import preference_affinity
from .acquisition_scoring import allocate
from .market_intel import batch_market_signals, primary_signal
from .market_prior_signal import batch_prior_signals, primary_prior_signal
from .opportunity import OpportunityInput, OpportunityScore, score_opportunities


@dataclass(slots=True)
class FunnelTrace:
    catalog_total: int = 0
    enabled_total: int = 0
    eligible_total: int = 0
    feasible_total: int = 0
    prior_known_count: int = 0
    live_history_known_count: int = 0
    fully_unknown_count: int = 0
    scored_total: int = 0
    shortlisted_total: int = 0
    exploit_candidates: int = 0
    explore_candidates: int = 0
    diversity_demoted: int = 0
    decisions: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "catalog_total": self.catalog_total,
            "enabled_total": self.enabled_total,
            "eligible_total": self.eligible_total,
            "feasible_total": self.feasible_total,
            "prior_known_count": self.prior_known_count,
            "live_history_known_count": self.live_history_known_count,
            "fully_unknown_count": self.fully_unknown_count,
            "scored_total": self.scored_total,
            "shortlisted_total": self.shortlisted_total,
            "exploit_candidates": self.exploit_candidates,
            "explore_candidates": self.explore_candidates,
            "diversity_demoted": self.diversity_demoted,
        }


@dataclass(slots=True)
class FunnelResult:
    chosen: list[Destination]
    scores: dict[str, OpportunityScore]   # dest id -> its score
    trace: FunnelTrace


def _diversity_adjust(
    ranked: list[OpportunityScore], *, per_subregion_soft_cap: int = 3,
    penalty: float = 0.08,
) -> tuple[list[OpportunityScore], int]:
    """Modest, interpretable: the Nth candidate from a subregion (beyond the
    soft cap) loses ``penalty`` per extra, so a strong outlier still wins its
    slot but ten near-identical cluster markets do not crowd out comparable
    alternatives. No hard quota. Re-sorts by adjusted score."""
    seen: dict[str, int] = {}
    demoted = 0
    adjusted: list[OpportunityScore] = []
    for s in sorted(ranked, key=lambda x: (-x.score, x.pre_rank)):
        key = s.subregion or "?"
        n = seen.get(key, 0)
        extra = max(0, n - (per_subregion_soft_cap - 1))
        if extra > 0:
            new = max(0.0, s.score - penalty * extra)
            adjusted.append(OpportunityScore(
                market=s.market, pre_rank=s.pre_rank, knowledge=s.knowledge,
                stance=s.stance, score=round(new, 4),
                components={**s.components, "diversity_penalty": round(penalty * extra, 4)},
                reason=s.reason, subregion=s.subregion, country_code=s.country_code,
            ))
            demoted += 1
        else:
            adjusted.append(s)
        seen[key] = n + 1
    adjusted.sort(key=lambda x: (-x.score, x.pre_rank))
    return adjusted, demoted


def run_funnel(
    db: Database,
    request: TripRequest,
    catalog: list[Destination],
    *,
    origin_airports: list[str],
    departure_date: date,
    slots: int,
    provider: str = "duffel",
    cfg: SearchIntelConfig | None = None,
    now: datetime | None = None,
) -> FunnelResult:
    cfg = cfg or search_intel_config()
    now = now or datetime.now(timezone.utc)
    trace = FunnelTrace(catalog_total=len(catalog))

    avoid = {c.casefold() for c in request.avoid_destinations}
    must = {c.casefold() for c in request.must_visit}
    origin_up = [o.upper() for o in origin_airports] or [request.origin.upper()]

    enabled = [d for d in catalog if d.enabled]
    trace.enabled_total = len(enabled)

    eligible = [
        d for d in enabled
        if d.acquisition_eligible and d.primary_airport
        and d.id.casefold() not in avoid
    ]
    trace.eligible_total = len(eligible)

    feasible = [
        d for d in eligible
        if d.primary_airport.upper() not in origin_up
        and request.duration_days <= max(d.recommended_max_days * 3, 14)
    ]
    # must-visit cities are always feasible candidates
    feasible += [d for d in eligible if d.id.casefold() in must and d not in feasible]
    trace.feasible_total = len(feasible)
    if not feasible:
        return FunnelResult([], {}, trace)

    dest_airports = [d.primary_airport.upper() for d in feasible]
    live_by_market = batch_market_signals(
        db,
        [MarketKey.build(provider=provider, origin=o, destination=a,
                         departure_date=departure_date, travelers=request.travelers)
         for o in origin_up for a in dest_airports],
        cfg=cfg, now=now,
    )
    prior_by_dest = batch_prior_signals(
        db, origin_airports=origin_up, destination_airports=dest_airports,
        cfg=cfg, now=now,
    )
    contrib = _contribution_history(db, provider, dest_airports)
    affinity = preference_affinity(feasible, request)

    inputs: list[OpportunityInput] = []
    for rank, d in enumerate(feasible):
        a = d.primary_airport.upper()
        # merge the per-(origin,dest) live signals for this destination
        live_sigs = []
        for o in origin_up:
            k = MarketKey.build(provider=provider, origin=o, destination=a,
                                departure_date=departure_date, travelers=request.travelers).as_tuple()
            s = primary_signal(live_by_market.get(k, {}))
            if s is not None:
                live_sigs.append(s)
        live = max(live_sigs, key=lambda s: (s.sample_count, s.confidence.score)) if live_sigs else None
        prior = primary_prior_signal(prior_by_dest.get(a, {}))
        ch = contrib.get(a, {})
        inputs.append(OpportunityInput(
            market=f"→{d.id}", pre_rank=rank, live=live, prior=prior,
            preference_affinity=affinity.get(d.id), feasible=True,
            useful_rate=ch.get("useful"), top_k_rate=ch.get("top_k"),
            winner_rate=ch.get("winner"),
            subregion=d.subregion, country_code=d.country_code,
        ))

    scored = score_opportunities(inputs, cfg=cfg)
    trace.scored_total = len(scored)
    trace.prior_known_count = sum(1 for s in scored if s.knowledge == "PRIOR")
    trace.live_history_known_count = sum(1 for s in scored if s.knowledge == "LIVE")
    trace.fully_unknown_count = sum(1 for s in scored if s.knowledge == "UNKNOWN")

    scored, demoted = _diversity_adjust(scored)
    trace.diversity_demoted = demoted

    chosen_scores = allocate(scored, slots=slots, cfg=cfg)
    chosen_markets = {s.market for s in chosen_scores}
    trace.shortlisted_total = len(chosen_scores)
    trace.exploit_candidates = sum(1 for s in chosen_scores if s.stance is AcquisitionStance.EXPLOIT)
    trace.explore_candidates = sum(1 for s in chosen_scores if s.stance is AcquisitionStance.EXPLORE)

    by_id = {d.id: d for d in feasible}
    order = {s.market: i for i, s in enumerate(chosen_scores)}
    chosen = sorted(
        (by_id[s.market[1:]] for s in scored if s.market in chosen_markets),
        key=lambda d: order.get(f"→{d.id}", 10_000),
    )
    score_by_id = {s.market[1:]: s for s in scored}
    trace.decisions = [
        {
            "destination": s.market[1:], "pre_rank": s.pre_rank,
            "knowledge": s.knowledge, "stance": s.stance.value,
            "selected": s.market in chosen_markets,
            "opportunity_score": s.score, "reason": s.reason,
            "components": s.components, "subregion": s.subregion,
        }
        for s in scored
    ]
    return FunnelResult(chosen, score_by_id, trace)


def _contribution_history(db: Database, provider: str, dest_airports: list[str]) -> dict[str, dict]:
    """This search's candidate destinations' historical useful/top-K/winner
    rates from live Price Memory, in one query."""
    if not dest_airports:
        return {}
    marks = ",".join("?" for _ in dest_airports)
    rows = db.query(
        f"SELECT destination,"
        " AVG(CASE WHEN entered_candidate_set THEN 1.0 ELSE 0.0 END) AS useful,"
        " AVG(CASE WHEN contributed_to_top_k THEN 1.0 ELSE 0.0 END) AS top_k,"
        " AVG(CASE WHEN contributed_to_winner THEN 1.0 ELSE 0.0 END) AS winner,"
        " COUNT(*) AS n"
        " FROM price_observations"
        " WHERE provider = ? AND destination IN (" + marks + ")"
        " AND COALESCE(provenance_version, 1) >= 2"
        " GROUP BY destination",
        (provider, *[a.upper() for a in dest_airports]),
    )
    return {
        r["destination"]: {"useful": r["useful"], "top_k": r["top_k"], "winner": r["winner"]}
        for r in rows if r["n"]
    }
