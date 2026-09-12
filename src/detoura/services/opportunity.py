"""Market Opportunity scoring (V9 Phase 2 §14, §15).

Estimates **"how worthwhile is a live acquisition call for this market?"** —
not "which historical fare is lowest". A single anomalously cheap prior or live
observation must not monopolise acquisition forever; a slightly dearer market
with frequent direct supply, a good preference fit and a history of
contributing to Top-K results can and should rank above it.

Interpretable weighted sum, every component in ``[0, 1]``, weights from
:class:`SearchIntelConfig` (env-tunable). Documented in
``docs/V9_PHASE2_MARKET_PRIOR_AND_CATALOG.md``.

## Signal precedence for the price component (V9 §12)

    recent+sufficient live Price Memory   ->  live price attractiveness (prior ~ignored)
    thin live history                     ->  blend, live weighted higher
    no live but a prior                   ->  prior attractiveness, scaled by
                                              prior confidence and capped at
                                              MARKET_PRIOR_WEIGHT_CEILING (0.55)
    neither                               ->  neutral 0.5 (cold start)

## Components

    price          blended live/prior price attractiveness (above)
    confidence     max(live confidence, prior confidence) mapped to [0,1]
    freshness      max(live recency, prior freshness)
    preference     caller-supplied destination affinity  (0.5 unknown)
    feasibility    1 if the destination resolves to an airport else 0
    supply         direct/frequency signal from prior     (0.5 unknown)
    contribution   this market's historical useful-call / Top-K / winner rate
                   from live Price Memory (0.5 unknown — never a penalty for a
                   market we have not tried)

    base  = Σ(wᵢ · componentᵢ) / Σwᵢ
    score = clamp[0,1]( base + exploration_bonus  if stance == EXPLORE )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..models.market_prior import HistoricalMarketPriorSignal, PriorConfidence
from ..models.search_intel import AcquisitionStance, HistoricalPriceSignal, MarketConfidence
from ..search_intel_config import SearchIntelConfig, search_intel_config

_LIVE_CONF = {
    MarketConfidence.NONE: 0.0, MarketConfidence.LOW: 0.25,
    MarketConfidence.MEDIUM: 0.6, MarketConfidence.HIGH: 1.0,
}
_PRIOR_CONF = {
    PriorConfidence.NONE: 0.0, PriorConfidence.LOW: 0.25,
    PriorConfidence.MEDIUM: 0.6, PriorConfidence.HIGH: 1.0,
}


class Knowledge(str, Enum):
    """How Detoura knows this market (V9 §21)."""

    LIVE = "LIVE"        # useful recent live Price Memory
    PRIOR = "PRIOR"      # no useful live history, but a bootstrap prior exists
    UNKNOWN = "UNKNOWN"  # neither


@dataclass(frozen=True, slots=True)
class OpportunityInput:
    market: str                     # "ORIG→dest" label
    pre_rank: int
    live: HistoricalPriceSignal | None
    prior: HistoricalMarketPriorSignal | None
    preference_affinity: float | None = None
    """USER FIT (V9 §B5) — how well this destination matches *this
    traveler's* stated preferences (:func:`detoura.services.acquisition.preference_affinity`).
    Deliberately separate from ``attractiveness_score`` below, which is the
    same number for every traveler. ``None`` = unknown, scored neutral."""
    feasible: bool = True
    #: this market's historical contribution rates from live Price Memory,
    #: any may be None (unknown)
    useful_rate: float | None = None
    top_k_rate: float | None = None
    winner_rate: float | None = None
    subregion: str | None = None
    country_code: str | None = None
    attractiveness_score: float | None = None
    """GLOBAL DESTINATION ATTRACTIVENESS (V9 Phase 3 §B) — this destination's
    :class:`~detoura.models.attractiveness.DestinationAttractivenessProfile`
    aggregate, rescaled to ``[0, 1]``. ``None`` when the profile is UNKNOWN
    (no catalog basis) or not supplied by the caller — scored neutral (0.5),
    never a penalty: a destination Detoura has not yet profiled is not
    assumed unattractive (§A3 "do not silently convert unknown values into
    optimistic values" cuts the other way too — neutral, not penalised)."""


@dataclass(frozen=True, slots=True)
class OpportunityScore:
    market: str
    pre_rank: int
    knowledge: str
    stance: AcquisitionStance
    score: float
    components: dict = field(default_factory=dict)
    reason: str = ""
    subregion: str | None = None
    country_code: str | None = None


def _knowledge(live: HistoricalPriceSignal | None,
               prior: HistoricalMarketPriorSignal | None,
               cfg: SearchIntelConfig) -> str:
    if live is not None and live.sample_count > 0 and live.confidence.verdict in (
        MarketConfidence.MEDIUM, MarketConfidence.HIGH,
    ):
        return Knowledge.LIVE
    if prior is not None and prior.prior_available and prior.confidence in (
        PriorConfidence.MEDIUM, PriorConfidence.HIGH,
    ):
        return Knowledge.PRIOR
    if live is not None and live.sample_count > 0:
        return Knowledge.LIVE  # thin live history still counts as LIVE-known
    if prior is not None and prior.prior_available:
        return Knowledge.PRIOR
    return Knowledge.UNKNOWN


def _price_component(
    live: HistoricalPriceSignal | None, prior: HistoricalMarketPriorSignal | None,
    field_live_median: float | None, cfg: SearchIntelConfig,
) -> tuple[float, dict]:
    live_attr = None
    if (live is not None and live.median_observed_minor is not None
            and live.confidence.verdict in (MarketConfidence.MEDIUM, MarketConfidence.HIGH)
            and field_live_median):
        ratio = live.median_observed_minor / field_live_median
        live_attr = max(0.0, min(1.0, 1.0 - (ratio - 0.5)))
    live_n = live.sample_count if live is not None else 0

    prior_attr = prior.relative_price_attractiveness if prior is not None else None
    prior_conf = _PRIOR_CONF.get(prior.confidence, 0.0) if prior is not None else 0.0
    # a prior's influence is capped and scaled by its own confidence
    prior_weight = min(cfg.weight_prior_ceiling, cfg.weight_prior_ceiling * prior_conf)
    # ...and shrinks toward zero as live history accumulates
    if live_n >= cfg.live_supersedes_min_samples:
        prior_weight *= 0.05
    elif live_n > 0:
        prior_weight *= max(0.1, 1.0 - live_n / cfg.live_supersedes_min_samples)

    if live_attr is not None and prior_attr is not None:
        w_live = cfg.weight_live_when_confident
        val = (w_live * live_attr + prior_weight * prior_attr) / (w_live + prior_weight)
        basis = "live+prior"
    elif live_attr is not None:
        val, basis = live_attr, "live"
    elif prior_attr is not None and prior_weight > 0.01:
        val = 0.5 + (prior_attr - 0.5) * (prior_weight / cfg.weight_prior_ceiling)
        basis = "prior"
    else:
        val, basis = 0.5, "cold-start"
    return round(max(0.0, min(1.0, val)), 4), {
        "basis": basis, "live_attractiveness": live_attr,
        "prior_attractiveness": prior_attr, "prior_weight": round(prior_weight, 4),
    }


def score_opportunities(
    inputs: list[OpportunityInput], *, cfg: SearchIntelConfig | None = None,
) -> list[OpportunityScore]:
    cfg = cfg or search_intel_config()
    live_medians = [
        i.live.median_observed_minor for i in inputs
        if i.live and i.live.median_observed_minor is not None
    ]
    import statistics
    field_live_median = statistics.median(live_medians) if live_medians else None

    w_price, w_conf, w_fresh = 0.28, 0.16, 0.12
    w_pref, w_feas, w_supply, w_contrib = 0.16, 0.10, 0.08, 0.10
    w_attract = max(0.0, cfg.weight_attractiveness)
    wsum = w_price + w_conf + w_fresh + w_pref + w_feas + w_supply + w_contrib + w_attract

    out: list[OpportunityScore] = []
    for i in inputs:
        kn = _knowledge(i.live, i.prior, cfg)
        price_c, price_meta = _price_component(i.live, i.prior, field_live_median, cfg)

        live_cv = _LIVE_CONF.get(i.live.confidence.verdict, 0.0) if i.live else 0.0
        prior_cv = _PRIOR_CONF.get(i.prior.confidence, 0.0) if i.prior else 0.0
        conf_c = max(live_cv, prior_cv)

        live_fresh = i.live.confidence.recency_component if i.live else 0.0
        prior_fresh = (i.prior.confidence_components.get("freshness", 0.0)
                       if i.prior else 0.0)
        fresh_c = max(live_fresh, prior_fresh)

        pref_c = 0.5 if i.preference_affinity is None else max(0.0, min(1.0, i.preference_affinity))
        feas_c = 1.0 if i.feasible else 0.0
        supply_c = (i.prior.direct_supply_signal if (i.prior and i.prior.direct_supply_signal is not None)
                    else 0.5)
        # contribution history: reward markets that have actually helped, but a
        # market we have never tried is neutral (0.5), never penalised.
        contrib_parts = [r for r in (i.useful_rate, i.top_k_rate, i.winner_rate) if r is not None]
        contrib_c = (0.5 + 0.5 * (sum(contrib_parts) / len(contrib_parts))) if contrib_parts else 0.5
        # V9 Phase 3 §A3/§B: global destination attractiveness, independent of
        # this traveler's preferences (that is `pref_c` above). UNKNOWN -> the
        # same neutral 0.5 every other unset component gets, never a penalty.
        attract_c = (
            0.5 if i.attractiveness_score is None
            else max(0.0, min(1.0, i.attractiveness_score))
        )

        base = (
            w_price * price_c + w_conf * conf_c + w_fresh * fresh_c
            + w_pref * pref_c + w_feas * feas_c + w_supply * supply_c
            + w_contrib * contrib_c + w_attract * attract_c
        ) / wsum

        stance = _stance(kn)
        score = base + (cfg.exploration_bonus if stance is AcquisitionStance.EXPLORE else 0.0)
        score = round(max(0.0, min(1.0, score)), 4)
        reason = _reason(kn, stance, i, price_meta)
        out.append(OpportunityScore(
            market=i.market, pre_rank=i.pre_rank, knowledge=kn.value, stance=stance,
            score=score, reason=reason, subregion=i.subregion,
            country_code=i.country_code,
            components={
                "price": price_c, "price_basis": price_meta["basis"],
                "confidence": round(conf_c, 4), "freshness": round(fresh_c, 4),
                "preference": round(pref_c, 4), "feasibility": feas_c,
                "supply": round(supply_c, 4), "contribution": round(contrib_c, 4),
                "attractiveness": round(attract_c, 4),
                "attractiveness_known": i.attractiveness_score is not None,
                "base": round(base, 4),
                "exploration_bonus": (
                    cfg.exploration_bonus if stance is AcquisitionStance.EXPLORE else 0.0
                ),
            },
        ))
    return out


def _stance(knowledge: str) -> AcquisitionStance:
    # EXPLOIT when we have usable knowledge (live OR a confident prior);
    # EXPLORE for UNKNOWN and thin-LIVE / low-confidence-PRIOR markets is
    # decided by the recorder's knowledge check below — here, LIVE/PRIOR ->
    # EXPLOIT, UNKNOWN -> EXPLORE.
    return AcquisitionStance.EXPLOIT if knowledge in (Knowledge.LIVE, Knowledge.PRIOR) \
        else AcquisitionStance.EXPLORE


def _reason(knowledge: str, stance: AcquisitionStance, i: OpportunityInput,
            price_meta: dict) -> str:
    if knowledge == Knowledge.LIVE:
        n = i.live.sample_count if i.live else 0
        c = i.live.confidence.verdict.value.lower() if i.live else "none"
        return f"exploit: fresh live history ({n} obs, {c})" + (
            " + strong preference match" if (i.preference_affinity or 0) > 0.7 else ""
        )
    if knowledge == Knowledge.PRIOR:
        c = i.prior.confidence.value.lower() if i.prior else "none"
        return (f"exploit: bootstrap prior indicates an attractive market "
                f"({c} confidence); no Detoura live history")
    return "explore: no live history or prior"
