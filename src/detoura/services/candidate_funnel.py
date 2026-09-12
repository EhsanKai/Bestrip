"""The V9 Phase 2/3 candidate funnel (§20, Phase 3 §A/§C6).

    ~200 catalog destinations
      → hard eligibility        (enabled, acquisition_eligible, has airport, not avoided)
      → cheap feasibility       (resolvable airport, not the origin, plausible duration)
      → Market Prior + Price Memory + Attractiveness batch lookup   (bounded queries, no N+1)
      → Market Opportunity scoring (incl. global attractiveness, V9 Phase 3 §A3)
      → diversity / cold-start handling (subregion soft cap + real geo/experience
        redundancy within a subregion, V9 Phase 3 §C6)
      → EXPLOIT / EXPLORE allocation  (Phase 1 allocate — EXPLORE floor preserved)
      → bounded shortlist
      → (caller builds a bounded acquisition plan → Duffel)

Every stage count is recorded on a :class:`FunnelTrace`. This module chooses
*which* markets to acquire; it never changes *how many* — the hard
``ProviderCallBudget`` still binds downstream and the inter-city / date fan-out
is constructed only among the small shortlist (§24, §25). Catalog growth must
not imply provider-call growth (V9 Phase 3 §A2) — nothing added here touches
``slots``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from ..models.destination import Destination
from ..models.market_prior import PriorConfidence
from ..models.search_intel import AcquisitionStance, MarketConfidence, MarketKey
from ..models.trip import TripRequest
from ..persistence import attractiveness as attractiveness_store
from ..persistence.db import Database
from ..search_intel_config import SearchIntelConfig, search_intel_config
from .acquisition import preference_affinity
from .acquisition_scoring import allocate
from .experience_similarity import experience_redundancy_signal
from .geo import geo_redundancy_signal
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
    attractiveness_known_count: int = 0
    """V9 Phase 3: how many feasible candidates had a known (non-UNKNOWN)
    attractiveness profile — a coverage diagnostic, never a gate."""
    exploration_lottery_rotated: bool = False
    """V9 Phase 3: whether the exploration lottery (see
    ``_apply_exploration_lottery``) actually swapped in a different EXPLORE
    candidate this search — an explainability signal, not a gate."""
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
            "attractiveness_known_count": self.attractiveness_known_count,
            "exploration_lottery_rotated": self.exploration_lottery_rotated,
        }


@dataclass(slots=True)
class FunnelResult:
    chosen: list[Destination]
    scores: dict[str, OpportunityScore]   # dest id -> its score
    trace: FunnelTrace


def _exploration_rotation_bonus(destination_id: str, departure_date: date, *, epsilon: float) -> float:
    """A small, deterministic, per-(destination, date) bonus in
    ``[0, epsilon)``, applied only to EXPLORE candidates (V9 Phase 3 fix for
    an independent-QA finding).

    Without this, the EXPLORE pool is ranked purely by score, and a
    destination that is *systematically* slightly behind its ~200
    all-EXPLORE competitors (e.g. every other UNKNOWN candidate happens to
    have a real, somewhat-above-neutral attractiveness profile while this
    one has none at all) is picked **zero** times across any number of
    searches, on any date — proven by adversarial QA: 0/100 selections
    across 25 independent searches for each of 4 different starved
    destinations. That contradicts §A5 ("a destination with weak/no history
    but high potential must still sometimes receive a provider call") and
    the module's own EXPLORE-floor framing, which only guards the
    EXPLOIT-vs-EXPLORE *split*, not fairness *within* an all-EXPLORE field.

    The fix rotates which EXPLORE candidates get a competitive edge by
    ``departure_date`` — deterministic and reproducible for any *fixed*
    request (the Determinism requirement is about repeated identical
    requests, not about the same destination winning on every date), while
    giving every destination a periodic, non-zero chance across the dates a
    catalog actually gets searched against. A SHA-256 hash rather than
    Python's built-in ``hash()`` — the latter is salted per-process
    (``PYTHONHASHSEED``) specifically to avoid this being predictable, which
    would silently reintroduce cross-process nondeterminism here.
    """
    digest = hashlib.sha256(f"{destination_id}|{departure_date.isoformat()}".encode()).hexdigest()
    frac = int(digest[:8], 16) / 0xFFFFFFFF
    return frac * epsilon


def _apply_exploration_lottery(
    scored: list[OpportunityScore], chosen: list[OpportunityScore], *, departure_date: date,
) -> tuple[list[OpportunityScore], bool]:
    """Give **every** EXPLORE candidate a genuine, non-zero, uniform chance
    of an acquisition slot on some search — not just the highest-scoring
    ones (V9 Phase 3 fix for an independent-QA finding).

    A small additive score nudge was tried first and rejected: with ~200
    EXPLORE candidates competing for a handful of slots, a bonus small
    enough not to distort genuine signal could not close a 200-place gap
    for a systematically-last-ranked candidate — it remained selected in
    0/40 searches even varying ``departure_date`` across a year. This is a
    real derandomized **lottery** instead: one dedicated EXPLORE slot goes to
    whichever EXPLORE candidate has the highest ``sha256(destination_id +
    departure_date)`` key — a uniform hash gives every candidate an equal
    ``1 / len(explore_pool)`` chance on any given date, deterministic for a
    fixed request (repeating the identical search reproduces the identical
    winner), and rotates fairly as dates vary. It never touches an EXPLOIT
    pick, and never changes how many slots are used — only swaps the
    single **lowest-scoring already-chosen EXPLORE pick** for the lottery
    winner when they differ, so the EXPLORE floor's size is unaffected."""
    explore_pool = [s for s in scored if s.stance is AcquisitionStance.EXPLORE]
    if not explore_pool:
        return chosen, False
    winner = max(
        explore_pool,
        key=lambda s: (_exploration_rotation_bonus(s.market[1:], departure_date, epsilon=1.0), s.market),
    )
    chosen_markets = {s.market for s in chosen}
    if winner.market in chosen_markets:
        return chosen, False
    chosen_explore = [s for s in chosen if s.stance is AcquisitionStance.EXPLORE]
    if not chosen_explore:
        return chosen, False  # no reserved EXPLORE slot to rotate within (e.g. slots == 1)
    displaced = min(chosen_explore, key=lambda s: (s.score, s.market))
    new_chosen = [s for s in chosen if s.market != displaced.market] + [winner]
    return new_chosen, True


def _diversity_adjust(
    ranked: list[OpportunityScore], *, by_id: dict[str, Destination],
    per_subregion_soft_cap: int = 3, penalty: float = 0.08,
    cfg: SearchIntelConfig | None = None,
) -> tuple[list[OpportunityScore], int]:
    """Which markets deserve a scarce provider call — not the final
    recommendation portfolio (that is ``services.portfolio``, applied later
    to real acquired prices). Two layers, both soft (V9 Phase 3 §C5/§C6):

    1. The original Phase 2 subregion soft cap — the Nth candidate from a
       subregion beyond the soft cap loses a flat ``penalty`` per extra, so a
       strong outlier still wins its acquisition slot.
    2. V9 Phase 3: **within that same subregion**, a real geographic +
       experience-similarity redundancy signal (the same reusable functions
       the final portfolio pass uses) further discounts a candidate that is
       both close to *and* experientially similar to a stronger candidate
       already accepted from the group — catching the case the flat
       per-subregion count cannot: two markets in different named subregions
       that are nonetheless next door to each other (§C6 generalises beyond
       any one hardcoded region). Bounded to comparisons *within* a subregion
       (typically a handful of candidates), never O(catalog²) — see the
       Performance requirement.

    No hard quota either way. Re-sorts by adjusted score."""
    cfg = cfg or search_intel_config()
    seen: dict[str, int] = {}
    accepted_by_subregion: dict[str, list[Destination]] = {}
    demoted = 0
    adjusted: list[OpportunityScore] = []
    for s in sorted(ranked, key=lambda x: (-x.score, x.pre_rank)):
        key = s.subregion or "?"
        n = seen.get(key, 0)
        extra = max(0, n - (per_subregion_soft_cap - 1))
        flat_penalty = penalty * extra

        dest = by_id.get(s.market[1:])
        group = accepted_by_subregion.get(key, [])
        redundancy_penalty = 0.0
        if dest is not None and group:
            geo_sig = geo_redundancy_signal(
                dest, group, km_scale=cfg.geo_redundancy_km_scale,
                free_allowance=cfg.geo_redundancy_free_allowance,
            )
            exp_sig = experience_redundancy_signal(
                dest, group, similarity_threshold=cfg.experience_similarity_threshold,
            )
            redundancy_penalty = (
                cfg.geo_redundancy_penalty * geo_sig
                + cfg.experience_similarity_penalty * exp_sig
            )

        total_penalty = flat_penalty + redundancy_penalty
        if total_penalty > 0:
            new = max(0.0, s.score - total_penalty)
            adjusted.append(OpportunityScore(
                market=s.market, pre_rank=s.pre_rank, knowledge=s.knowledge,
                stance=s.stance, score=round(new, 4),
                components={
                    **s.components,
                    "diversity_penalty": round(flat_penalty, 4),
                    "geo_redundancy_penalty": round(
                        cfg.geo_redundancy_penalty * geo_sig if dest is not None and group else 0.0, 4
                    ),
                    "experience_redundancy_penalty": round(
                        cfg.experience_similarity_penalty * exp_sig if dest is not None and group else 0.0, 4
                    ),
                },
                reason=s.reason, subregion=s.subregion, country_code=s.country_code,
            ))
            demoted += 1
        else:
            adjusted.append(s)
        seen[key] = n + 1
        if dest is not None:
            accepted_by_subregion.setdefault(key, []).append(dest)
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
    # V9 Phase 3 §A3/§B: one batch query for the whole shortlist, never one
    # per candidate. A destination with no row is simply absent from the
    # dict — UNKNOWN, scored neutral downstream, never a fabricated value.
    attractiveness_by_id = attractiveness_store.batch_get_profiles(
        db, [d.id for d in feasible], model_version=cfg.attractiveness_model_version,
    )

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
        profile = attractiveness_by_id.get(d.id)
        attract = (profile.aggregate_score / 100.0) if (profile and profile.is_known) else None
        inputs.append(OpportunityInput(
            market=f"→{d.id}", pre_rank=rank, live=live, prior=prior,
            preference_affinity=affinity.get(d.id), feasible=True,
            useful_rate=ch.get("useful"), top_k_rate=ch.get("top_k"),
            winner_rate=ch.get("winner"),
            subregion=d.subregion, country_code=d.country_code,
            attractiveness_score=attract,
        ))

    scored = score_opportunities(inputs, cfg=cfg)
    trace.scored_total = len(scored)
    trace.prior_known_count = sum(1 for s in scored if s.knowledge == "PRIOR")
    trace.live_history_known_count = sum(1 for s in scored if s.knowledge == "LIVE")
    trace.fully_unknown_count = sum(1 for s in scored if s.knowledge == "UNKNOWN")
    trace.attractiveness_known_count = sum(
        1 for d in feasible if attractiveness_by_id.get(d.id) and attractiveness_by_id[d.id].is_known
    )

    by_id_for_diversity = {d.id: d for d in feasible}
    scored, demoted = _diversity_adjust(scored, by_id=by_id_for_diversity, cfg=cfg)
    trace.diversity_demoted = demoted

    chosen_scores = allocate(scored, slots=slots, cfg=cfg)
    lottery_rotated = False
    if cfg.explore_rotation_epsilon > 0:
        chosen_scores, lottery_rotated = _apply_exploration_lottery(
            scored, chosen_scores, departure_date=departure_date,
        )
    chosen_markets = {s.market for s in chosen_scores}
    trace.shortlisted_total = len(chosen_scores)
    trace.exploit_candidates = sum(1 for s in chosen_scores if s.stance is AcquisitionStance.EXPLOIT)
    trace.explore_candidates = sum(1 for s in chosen_scores if s.stance is AcquisitionStance.EXPLORE)
    trace.exploration_lottery_rotated = lottery_rotated

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
