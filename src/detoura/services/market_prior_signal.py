"""Aggregate Bootstrap Market Prior rows into an interpretable signal
(V9 Phase 2 §11, §13).

For a candidate market (origin airports × a destination airport, optionally
filtered to a season/horizon context) this produces one
:class:`HistoricalMarketPriorSignal` **per currency** — priors in EUR and USD
are never blended, no FX is invented.

## Confidence + decay (documented, deterministic — not ML)

    row_component     = min(1, matching_row_count / 4)
    source_conf       = mean of the rows' stated PriorConfidence
                        (NONE 0 / LOW 0.33 / MEDIUM 0.66 / HIGH 1)
    freshness         = 0.5 ** (age_days / MARKET_PRIOR_DECAY_HALF_LIFE_DAYS)   # default half-life 120d
                        age from the newest row's source_date (imported_at if absent)

    score = 0.30 * row_component + 0.35 * source_conf + 0.35 * freshness

    verdict = NONE   if row_count == 0
              LOW    if score < 0.34
              MEDIUM if score < 0.67
              HIGH   otherwise

A prior more than ~1 half-life stale can no longer reach HIGH however many rows
it has; a prior from months ago decays toward LOW and finally cannot dominate
acquisition at all (see :mod:`detoura.services.opportunity`).
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timezone

from ..models.market_prior import HistoricalMarketPriorSignal, PriorConfidence
from ..persistence import market_priors as store
from ..persistence.db import Database
from ..search_intel_config import SearchIntelConfig, search_intel_config

_CONF_VAL = {
    PriorConfidence.NONE: 0.0, PriorConfidence.LOW: 0.33,
    PriorConfidence.MEDIUM: 0.66, PriorConfidence.HIGH: 1.0,
}
_ROWS_FULL = 4


def _age_days(rows: list[dict], now: datetime) -> tuple[float | None, date | None]:
    dates: list[date] = []
    for r in rows:
        raw = r.get("source_date") or (r.get("imported_at") or "")[:10]
        try:
            dates.append(date.fromisoformat(raw))
        except (ValueError, TypeError):
            continue
    if not dates:
        return None, None
    newest = max(dates)
    return max(0.0, (now.date() - newest).days), newest


def _verdict(score: float, rows: int) -> PriorConfidence:
    if rows == 0:
        return PriorConfidence.NONE
    if score < 0.34:
        return PriorConfidence.LOW
    if score < 0.67:
        return PriorConfidence.MEDIUM
    return PriorConfidence.HIGH


def _signal_for_currency(
    origin_disp: str, dest_airport: str, dest_id: str | None,
    currency: str, rows: list[dict], cfg: SearchIntelConfig, now: datetime,
    field_typical: float | None,
) -> HistoricalMarketPriorSignal:
    n = len(rows)
    row_c = min(1.0, n / _ROWS_FULL)
    source_conf = statistics.fmean(
        _CONF_VAL.get(PriorConfidence(r["confidence"]), 0.33) for r in rows
    ) if rows else 0.0
    age, newest = _age_days(rows, now)
    half = max(cfg.prior_decay_half_life_days, 1e-6)
    freshness = (0.5 ** (age / half)) if age is not None else 0.35
    score = round(0.30 * row_c + 0.35 * source_conf + 0.35 * freshness, 4)
    verdict = _verdict(score, n)
    # The aggregate can never be more confident than the source itself claimed:
    # if the source rows are mostly LOW/NONE, cap at MEDIUM however fresh or
    # numerous they are — a bootstrap estimate the source doubted is not HIGH.
    if source_conf < 0.5 and verdict is PriorConfidence.HIGH:
        verdict = PriorConfidence.MEDIUM

    lows = [r["observed_low_minor"] for r in rows if r["observed_low_minor"] is not None]
    typs = [
        r["typical_minor"] if r["typical_minor"] is not None else r["median_minor"]
        for r in rows
        if (r["typical_minor"] is not None or r["median_minor"] is not None)
    ]
    highs = [r["observed_high_minor"] for r in rows if r["observed_high_minor"] is not None]
    enough = n >= cfg.prior_min_rows_for_band
    low_e = int(statistics.median(lows)) if (enough and lows) else None
    typ_e = int(statistics.median(typs)) if (enough and typs) else None
    high_e = int(statistics.median(highs)) if (enough and highs) else None

    # relative price attractiveness — cheaper typical vs the candidate field's
    # typical, credited only at >= MEDIUM prior confidence, else neutral 0.5.
    rel = 0.5
    if (verdict in (PriorConfidence.MEDIUM, PriorConfidence.HIGH)
            and typ_e is not None and field_typical and field_typical > 0):
        ratio = typ_e / field_typical
        rel = max(0.0, min(1.0, 1.0 - (ratio - 0.5)))

    # supply signal from weekly_frequency / carrier_count where present
    freqs = [r["weekly_frequency"] for r in rows if r["weekly_frequency"] is not None]
    directs = [r["direct_possible"] for r in rows if r["direct_possible"] is not None]
    supply = None
    if freqs or directs:
        f = min(1.0, (statistics.fmean(freqs) / 21.0)) if freqs else 0.4
        dr = (sum(1 for x in directs if x) / len(directs)) if directs else 0.5
        supply = round(0.6 * f + 0.4 * dr, 4)

    return HistoricalMarketPriorSignal(
        origin_airport=origin_disp, destination_airport=dest_airport,
        destination_id=dest_id, currency=currency,
        prior_available=n > 0, row_count=n,
        source=rows[0]["source"] if rows else "",
        source_version=rows[0]["source_version"] if rows else "",
        source_date=newest, age_days=age,
        low_estimate_minor=low_e, typical_estimate_minor=typ_e,
        high_estimate_minor=high_e,
        relative_price_attractiveness=round(rel, 4),
        direct_supply_signal=supply,
        confidence=verdict,
        confidence_components={
            "row_component": round(row_c, 4),
            "source_confidence": round(source_conf, 4),
            "freshness": round(freshness, 4),
            "score": score,
        },
    )


def batch_prior_signals(
    db: Database,
    *,
    origin_airports: list[str],
    destination_airports: list[str],
    cfg: SearchIntelConfig | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, HistoricalMarketPriorSignal]]:
    """One query for every candidate destination (V9 §10 — no N+1). Returns
    ``{destination_airport -> {currency -> signal}}``; a destination with no
    prior is absent (cold start)."""
    cfg = cfg or search_intel_config()
    now = now or datetime.now(timezone.utc)
    if not cfg.prior_enabled:
        return {}
    grouped = store.priors_for_destinations(
        db, origin_airports=origin_airports, destination_airports=destination_airports,
    )
    # field-level typical, per currency, for the relative-attractiveness component
    field_typ: dict[str, list[int]] = {}
    for rows in grouped.values():
        for r in rows:
            v = r["typical_minor"] if r["typical_minor"] is not None else r["median_minor"]
            if v is not None:
                field_typ.setdefault(str(r["currency"]).upper(), []).append(int(v))
    field_typical = {c: statistics.median(v) for c, v in field_typ.items() if v}

    origin_disp = "|".join(sorted(a.upper() for a in origin_airports))
    out: dict[str, dict[str, HistoricalMarketPriorSignal]] = {}
    for dest, rows in grouped.items():
        if not rows:
            continue
        by_ccy: dict[str, list[dict]] = {}
        for r in rows:
            by_ccy.setdefault(str(r["currency"]).upper(), []).append(r)
        out[dest] = {
            ccy: _signal_for_currency(
                origin_disp, dest, rows[0].get("destination_id"), ccy, ccy_rows,
                cfg, now, field_typical.get(ccy),
            )
            for ccy, ccy_rows in by_ccy.items()
        }
    return out


def primary_prior_signal(
    signals: dict[str, HistoricalMarketPriorSignal], *, prefer: str = "EUR",
) -> HistoricalMarketPriorSignal | None:
    if not signals:
        return None
    if prefer.upper() in signals:
        return signals[prefer.upper()]
    return max(signals.values(), key=lambda s: s.row_count)
