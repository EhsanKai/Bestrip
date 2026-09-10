"""Baseline interpretable acquisition scoring + explore/exploit (V9 Phase 1).

This is the **first** historical acquisition score, not the final V9 adaptive
engine. It is a documented weighted sum of interpretable components — no model,
no training.

## Score

For a candidate market, each component is in [0, 1]:

    price_attractiveness  cheaper historical median vs the candidate field's
                          median, but only credited when confidence >= MEDIUM
                          and clamped to [0, 1]; 0.5 when there is no usable
                          history (neutral, never a penalty)
    confidence            MarketConfidence -> {NONE:0, LOW:0.25, MEDIUM:0.6, HIGH:1}
    freshness             the recency component of the confidence breakdown
    preference            caller-supplied destination affinity in [0, 1]
                          (0.5 when unknown)
    feasibility           1 if the destination resolves to an airport, else 0

    base = ( w_price * price_attractiveness
           + w_conf  * confidence
           + w_fresh * freshness
           + w_pref  * preference
           + w_feas  * feasibility ) / (w_price + w_conf + w_fresh + w_pref + w_feas)

    score = base + (exploration_bonus if stance == EXPLORE else 0)   # then clamp [0, 1]

Weights and the bonus are `SearchIntelConfig` (env-tunable). The design
constraint from V9 §8 — **historical cheapness must not dominate** — is why
`w_price` defaults to 0.30 and price attractiveness is neutral (0.5), not
maximal, for unseen markets.

## Explore vs exploit (V9 §9)

A candidate is **EXPLOIT** iff it has usable history at MEDIUM or HIGH
confidence. Otherwise **EXPLORE**. Then :func:`allocate` guarantees a minimum
EXPLORE share of the budget (env `SEARCH_INTEL_EXPLORE_FRACTION` /
`SEARCH_INTEL_EXPLORE_MIN_SLOTS`) by promoting the best-scoring EXPLORE
candidates ahead of marginal EXPLOIT ones — so a zero-history market is never
permanently unreachable.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ..models.search_intel import HistoricalPriceSignal, MarketConfidence
from ..models.search_trace import AcquisitionStance
from ..search_intel_config import SearchIntelConfig, search_intel_config

_CONF_SCORE = {
    MarketConfidence.NONE: 0.0,
    MarketConfidence.LOW: 0.25,
    MarketConfidence.MEDIUM: 0.6,
    MarketConfidence.HIGH: 1.0,
}


@dataclass(frozen=True, slots=True)
class CandidateInput:
    """What the scorer needs about one candidate market."""

    market: str                     # "ORIG→DEST" label
    pre_rank: int
    signal: HistoricalPriceSignal | None
    preference_affinity: float | None = None   # [0, 1]
    feasible: bool = True


@dataclass(frozen=True, slots=True)
class CandidateScore:
    market: str
    pre_rank: int
    stance: AcquisitionStance
    score: float
    components: dict = field(default_factory=dict)
    confidence: MarketConfidence = MarketConfidence.NONE
    historical_signal_used: bool = False
    reason: str = ""


def _price_attractiveness(
    signal: HistoricalPriceSignal | None, field_median: float | None,
) -> float:
    if (
        signal is None
        or signal.median_observed_minor is None
        or signal.confidence.verdict in (MarketConfidence.NONE, MarketConfidence.LOW)
        or not field_median
        or field_median <= 0
    ):
        return 0.5  # neutral — no usable history, never a penalty
    # cheaper than the field median -> above 0.5, capped; ratio 0.5 -> 1.0,
    # ratio 1.0 -> 0.5, ratio 2.0 -> 0.0
    ratio = signal.median_observed_minor / field_median
    return max(0.0, min(1.0, 1.0 - (ratio - 0.5)))


def classify_stance(signal: HistoricalPriceSignal | None) -> AcquisitionStance:
    if signal is not None and signal.confidence.verdict in (
        MarketConfidence.MEDIUM, MarketConfidence.HIGH,
    ):
        return AcquisitionStance.EXPLOIT
    return AcquisitionStance.EXPLORE


def score_candidates(
    candidates: list[CandidateInput], *, cfg: SearchIntelConfig | None = None,
) -> list[CandidateScore]:
    cfg = cfg or search_intel_config()
    medians = [
        c.signal.median_observed_minor
        for c in candidates
        if c.signal and c.signal.median_observed_minor is not None
    ]
    field_median = statistics.median(medians) if medians else None

    wsum = (cfg.weight_price_attractiveness + cfg.weight_confidence
            + cfg.weight_freshness + cfg.weight_preference + cfg.weight_feasibility)
    wsum = wsum or 1.0

    out: list[CandidateScore] = []
    for c in candidates:
        sig = c.signal
        conf = sig.confidence.verdict if sig else MarketConfidence.NONE
        price_c = _price_attractiveness(sig, field_median)
        conf_c = _CONF_SCORE[conf]
        fresh_c = sig.confidence.recency_component if sig else 0.0
        pref_c = 0.5 if c.preference_affinity is None else max(0.0, min(1.0, c.preference_affinity))
        feas_c = 1.0 if c.feasible else 0.0
        base = (
            cfg.weight_price_attractiveness * price_c
            + cfg.weight_confidence * conf_c
            + cfg.weight_freshness * fresh_c
            + cfg.weight_preference * pref_c
            + cfg.weight_feasibility * feas_c
        ) / wsum
        stance = classify_stance(sig)
        score = base + (cfg.exploration_bonus if stance is AcquisitionStance.EXPLORE else 0.0)
        score = round(max(0.0, min(1.0, score)), 4)
        used = bool(sig and sig.sample_count > 0)
        reason = (
            f"{stance.value.lower()}: "
            + (
                f"history {conf.value.lower()} ({sig.sample_count} obs)"
                if used else "no history — acquiring to learn"
            )
        )
        out.append(CandidateScore(
            market=c.market, pre_rank=c.pre_rank, stance=stance, score=score,
            confidence=conf, historical_signal_used=used, reason=reason,
            components={
                "price_attractiveness": round(price_c, 4),
                "confidence": round(conf_c, 4),
                "freshness": round(fresh_c, 4),
                "preference": round(pref_c, 4),
                "feasibility": round(feas_c, 4),
                "base": round(base, 4),
                "exploration_bonus": (
                    cfg.exploration_bonus if stance is AcquisitionStance.EXPLORE else 0.0
                ),
            },
        ))
    return out


def allocate(
    scored: list[CandidateScore], *, slots: int, cfg: SearchIntelConfig | None = None,
) -> list[CandidateScore]:
    """Choose which ``slots`` candidates to acquire, guaranteeing a minimum
    EXPLORE share so unseen markets stay reachable (V9 §9, §10).

    Returns the selected candidates in acquisition order (EXPLOIT first, then
    the reserved EXPLORE picks), each unchanged except that the returned list
    *is* the selection.
    """
    cfg = cfg or search_intel_config()
    if slots <= 0 or not scored:
        return []
    if len(scored) <= slots:
        return sorted(scored, key=lambda s: (-s.score, s.pre_rank))

    explore_target = 0
    if slots > 1:
        explore_target = max(
            cfg.explore_min_slots,
            int(round(slots * cfg.explore_fraction)),
        )
    explore_target = min(explore_target, slots)
    exploit_target = slots - explore_target

    explore_pool = sorted(
        (s for s in scored if s.stance is AcquisitionStance.EXPLORE),
        key=lambda s: (-s.score, s.pre_rank),
    )
    exploit_pool = sorted(
        (s for s in scored if s.stance is AcquisitionStance.EXPLOIT),
        key=lambda s: (-s.score, s.pre_rank),
    )

    chosen_explore = explore_pool[:explore_target]
    chosen_exploit = exploit_pool[:exploit_target]

    # If one pool is short, backfill from the other so the budget is used.
    short = slots - len(chosen_explore) - len(chosen_exploit)
    if short > 0:
        extra_exploit = exploit_pool[len(chosen_exploit):]
        extra_explore = explore_pool[len(chosen_explore):]
        backfill = sorted(
            extra_exploit + extra_explore, key=lambda s: (-s.score, s.pre_rank),
        )[:short]
        # keep them in their own group for ordering
        for s in backfill:
            (chosen_explore if s.stance is AcquisitionStance.EXPLORE
             else chosen_exploit).append(s)

    return chosen_exploit + chosen_explore
