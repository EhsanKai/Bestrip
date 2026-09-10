"""Interpretable market aggregation over Price Memory (V9 Phase 1).

Turns raw :class:`PriceObservation` rows into a :class:`HistoricalPriceSignal`
per market **and per currency** — EUR and USD observations are never merged as
though a euro were a dollar. There is no FX here; if a market has been observed
in more than one currency it produces more than one signal.

Nothing in this module yields a bookable amount. A `HistoricalPriceSignal` is a
statistic; `not_a_quote` is stamped on every one.

## Confidence formula (documented, deterministic — not ML)

For a market's observations in one currency:

    sample_component      = min(1, sample_count / SAMPLE_FULL)          SAMPLE_FULL = 12
    recency_component     = max(0, 1 - age_days(freshest) / horizon_days)
    consistency_component = 1 - min(1, price_cv)     price_cv = stdev / mean of per-person prices

    score   = 0.45 * sample_component
            + 0.35 * recency_component
            + 0.20 * consistency_component

    verdict = NONE   if sample_count == 0
              LOW    if score < 0.34
              MEDIUM if score < 0.67
              HIGH   otherwise

`horizon_days` is `SearchIntelConfig.freshness_horizon_days` (env
`PRICE_MEMORY_FRESHNESS_HORIZON_DAYS`, default 45). Percentiles are suppressed
below `min_samples_for_aggregate` (env `PRICE_MEMORY_MIN_SAMPLES`, default 3):
one stale observation must never read like a confident market.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone

from ..models.search_intel import (
    ConfidenceBreakdown,
    HistoricalPriceSignal,
    MarketConfidence,
    MarketKey,
)
from ..persistence import price_memory as pm
from ..persistence.db import Database
from ..search_intel_config import SearchIntelConfig, search_intel_config

SAMPLE_FULL = 12


def _percentile(sorted_vals: list[int], q: float) -> int:
    if not sorted_vals:
        return 0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return int(round(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac))


def confidence_for(
    *, sample_count: int, freshest_at: datetime | None, price_cv: float | None,
    cfg: SearchIntelConfig, now: datetime | None = None,
) -> ConfidenceBreakdown:
    now = now or datetime.now(timezone.utc)
    sample_c = min(1.0, sample_count / SAMPLE_FULL) if sample_count else 0.0
    if freshest_at is not None:
        if freshest_at.tzinfo is None:
            freshest_at = freshest_at.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - freshest_at).total_seconds() / 86400.0)
        recency_c = max(0.0, 1.0 - age_days / max(cfg.freshness_horizon_days, 1e-6))
    else:
        recency_c = 0.0
    # Consistency is only meaningful with >= 2 prices; with fewer it is
    # *unknown*, which is neutral (0.5), never "perfectly consistent".
    if price_cv is None or sample_count < 2:
        consistency_c = 0.5
    else:
        consistency_c = 1.0 - min(1.0, price_cv)
    score = round(0.45 * sample_c + 0.35 * recency_c + 0.20 * consistency_c, 4)
    if sample_count == 0:
        verdict = MarketConfidence.NONE
    elif score < 0.34:
        verdict = MarketConfidence.LOW
    elif score < 0.67:
        verdict = MarketConfidence.MEDIUM
    else:
        verdict = MarketConfidence.HIGH
    # A thin sample can never read as more than tentatively confident, however
    # recent it is: one price from yesterday is still one price.
    if 0 < sample_count < cfg.min_samples_for_aggregate and verdict in (
        MarketConfidence.MEDIUM, MarketConfidence.HIGH,
    ):
        verdict = MarketConfidence.LOW
    return ConfidenceBreakdown(
        sample_component=round(sample_c, 4),
        recency_component=round(recency_c, 4),
        consistency_component=round(consistency_c, 4),
        score=score,
        verdict=verdict,
    )


def _signal_from_rows(
    market: MarketKey, currency: str, rows: list[dict], cfg: SearchIntelConfig,
    now: datetime | None,
) -> HistoricalPriceSignal:
    prices = sorted(int(r["per_person_minor"]) for r in rows)
    n = len(prices)
    times = [datetime.fromisoformat(r["observed_at"]) for r in rows]
    freshest = max(times) if times else None
    oldest = min(times) if times else None

    mean = statistics.fmean(prices) if prices else 0.0
    cv = (statistics.pstdev(prices) / mean) if (n >= 2 and mean > 0) else (0.0 if n else None)

    enough = n >= cfg.min_samples_for_aggregate
    median = _percentile(prices, 0.5) if enough else None
    cheap = _percentile(prices, cfg.cheap_percentile) if enough else None
    expensive = _percentile(prices, cfg.expensive_percentile) if enough else None

    direct_n = sum(1 for r in rows if r["direct"] == 1)
    conn_n = sum(1 for r in rows if r["direct"] == 0)

    def _rate(col: str) -> float | None:
        return round(sum(1 for r in rows if r[col] == 1) / n, 4) if n else None

    return HistoricalPriceSignal(
        market=market,
        currency=currency,
        sample_count=n,
        freshest_observation_at=freshest,
        oldest_observation_at=oldest,
        median_observed_minor=median,
        cheap_reference_minor=cheap,
        expensive_reference_minor=expensive,
        min_observed_minor=prices[0] if enough else None,
        max_observed_minor=prices[-1] if enough else None,
        price_cv=round(cv, 4) if cv is not None else None,
        direct_sample_count=direct_n,
        connecting_sample_count=conn_n,
        useful_offer_rate=_rate("entered_candidate_set"),
        top_k_contribution_rate=_rate("contributed_to_top_k"),
        winner_contribution_rate=_rate("contributed_to_winner"),
        confidence=confidence_for(
            sample_count=n, freshest_at=freshest, price_cv=cv, cfg=cfg, now=now,
        ),
    )


def market_signals(
    db: Database, market: MarketKey, *, cfg: SearchIntelConfig | None = None,
    now: datetime | None = None,
) -> dict[str, HistoricalPriceSignal]:
    """All signals for one market, keyed by currency. Empty dict = no history
    (cold start — the caller falls back to deterministic signals)."""
    cfg = cfg or search_intel_config()
    rows = pm.observations_for_market(db, market)
    return _group_by_currency(market, rows, cfg, now)


def batch_market_signals(
    db: Database, markets: list[MarketKey], *, cfg: SearchIntelConfig | None = None,
    now: datetime | None = None,
) -> dict[tuple, dict[str, HistoricalPriceSignal]]:
    """One DB query for many markets (V9 §15 — no N+1). Keyed by
    ``MarketKey.as_tuple()`` then by currency."""
    cfg = cfg or search_intel_config()
    grouped = pm.observations_for_markets(db, markets)
    out: dict[tuple, dict[str, HistoricalPriceSignal]] = {}
    by_key = {m.as_tuple(): m for m in markets}
    for key_tuple, rows in grouped.items():
        market = by_key.get(key_tuple)
        if market is None or not rows:
            continue  # a market with no history is absent — cold start
        out[key_tuple] = _group_by_currency(market, rows, cfg, now)
    return out


def _group_by_currency(
    market: MarketKey, rows: list[dict], cfg: SearchIntelConfig, now: datetime | None,
) -> dict[str, HistoricalPriceSignal]:
    by_ccy: dict[str, list[dict]] = {}
    for r in rows:
        by_ccy.setdefault(str(r["currency"]).upper(), []).append(r)
    return {
        ccy: _signal_from_rows(market, ccy, ccy_rows, cfg, now)
        for ccy, ccy_rows in by_ccy.items()
    }


def primary_signal(
    signals: dict[str, HistoricalPriceSignal], *, prefer: str = "EUR",
) -> HistoricalPriceSignal | None:
    """Pick one signal when a caller wants a single view — the preferred
    currency if present, else the one with the most samples. Never blends."""
    if not signals:
        return None
    if prefer.upper() in signals:
        return signals[prefer.upper()]
    return max(signals.values(), key=lambda s: s.sample_count)
