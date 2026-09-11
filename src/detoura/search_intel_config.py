"""Configuration for the Search Intelligence layer (V9 Phase 1).

Every tunable is read from the environment here, once, so that no policy number
is hardcoded into an algorithm or a business rule. Acquisition scoring,
retention, the Top-K cutoff and the provider search-economics terms all come
from this object; changing Duffel's published pricing, for instance, is an env
change, not a code change.

Nothing here is secret. The Duffel *token* is read elsewhere (env only, never
logged); this module holds only commercial terms and algorithm parameters.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


#: ~30 important European origin airports the bootstrap job prioritises
#: (origin-first — V9 Phase 2 §6). Overridable via env
#: ``MARKET_PRIOR_BOOTSTRAP_ORIGINS``.
DEFAULT_BOOTSTRAP_ORIGINS: tuple[str, ...] = (
    "LHR", "CDG", "AMS", "FRA", "MAD", "BCN", "FCO", "MUC", "BER", "DUB",
    "CPH", "VIE", "ZRH", "LIS", "ARN", "OSL", "HEL", "BRU", "MAN", "MXP",
    "ATH", "WAW", "PRG", "DUS", "CGN", "STN", "GVA", "OTP", "BUD", "EDI",
)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _int_tuple(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        vals = tuple(int(x) for x in raw.replace(" ", "").split(",") if x)
        return vals or default
    except ValueError:
        return default


def _str_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    vals = tuple(x.strip().upper() for x in raw.split(",") if x.strip())
    return vals or default


def _opt_float(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ProviderEconomicsConfig:
    """Commercial terms for a provider's search API, kept out of business logic.

    A provider may bill *excess* searches once a rolling search-to-book ratio is
    exceeded. Any term that is not configured stays ``None`` and every figure
    derived from it is reported as UNKNOWN - never as zero.
    """

    provider: str = "duffel"
    #: Searches included before excess billing starts, per booking, or a flat
    #: monthly allowance. ``None`` = not configured -> excess is UNKNOWN.
    included_searches_per_booking: float | None = None
    included_searches_flat: int | None = None
    #: Fee per excess search, in ``currency``. ``None`` = UNKNOWN.
    excess_search_fee: float | None = None
    currency: str = "EUR"
    #: The search-to-book ratio above which excess billing is assumed to apply.
    excess_ratio_threshold: float | None = None

    @property
    def is_configured(self) -> bool:
        return (
            self.excess_search_fee is not None
            and (
                self.included_searches_per_booking is not None
                or self.included_searches_flat is not None
            )
        )


@dataclass(frozen=True, slots=True)
class SearchIntelConfig:
    """The whole Search Intelligence configuration surface."""

    enabled: bool = True
    """Master switch. Off -> the recorder is a no-op; search is unaffected."""

    retention_days: int = 180
    """PriceObservation rows older than this are pruned. Never touches booking
    economics or audit rows."""

    top_k: int = 5
    """The Top-K cut for contribution attribution and metrics."""

    # --- baseline acquisition scoring weights (documented in
    # --- docs/V9_PHASE1_SEARCH_INTELLIGENCE.md; all in [0, 1], summed then
    # --- normalised) ---
    weight_price_attractiveness: float = 0.30
    weight_confidence: float = 0.20
    weight_freshness: float = 0.15
    weight_preference: float = 0.20
    weight_feasibility: float = 0.15
    exploration_bonus: float = 0.10
    """Added to a candidate's score when it is an EXPLORE pick, so exploration
    is never purely a leftover of exploitation."""

    # --- exploration allocation ---
    explore_fraction: float = 0.20
    """Target share of the acquisition budget spent on EXPLORE candidates."""
    explore_min_slots: int = 1
    """At least this many EXPLORE slots whenever the budget allows > 1 call, so
    a zero-history market is never permanently unreachable."""

    # --- market aggregation ---
    min_samples_for_aggregate: int = 3
    """Below this, an aggregate is returned but flagged low-confidence and
    percentiles are suppressed."""
    cheap_percentile: float = 0.25
    expensive_percentile: float = 0.75
    #: Age (days) at which a market's freshness contribution reaches zero.
    freshness_horizon_days: float = 45.0

    economics: ProviderEconomicsConfig = field(default_factory=ProviderEconomicsConfig)

    # --- V9 Phase 2: Bootstrap Market Prior --------------------------------
    prior_enabled: bool = True
    prior_retention_days: int = 365
    """Bootstrap prior rows older than this (by ``source_date`` or
    ``imported_at``) are pruned. Longer than live Price Memory — a prior is
    meant to be a slowly-changing background estimate."""
    prior_horizon_bucket_days: tuple[int, ...] = (14, 30, 45, 60, 90, 120)
    """The representative booking-horizon buckets a bootstrap import targets.
    Configuration, not policy (env ``MARKET_PRIOR_HORIZON_BUCKETS``)."""
    prior_decay_half_life_days: float = 120.0
    """Confidence weight of a prior halves every this-many days past its
    ``source_date``. Interpretable exponential decay (V9 §13)."""
    prior_min_rows_for_band: int = 2
    """Below this many matching prior rows, the expected price band is
    suppressed (only a coarse "prior exists" signal remains)."""

    # Signal precedence (V9 §12): recent live > older live > prior > cold-start.
    # These are the *weights* the opportunity score blends the price-attractiveness
    # component with; live always dominates when it is fresh and sufficient.
    weight_live_when_confident: float = 1.0
    weight_prior_ceiling: float = 0.55
    """A prior's maximum influence on the price-attractiveness component, even
    at HIGH prior confidence — so a bootstrap estimate never fully speaks for a
    market Detoura has never actually priced."""
    live_supersedes_min_samples: int = 4
    """At or above this many fresh live observations, the prior's
    price-attractiveness influence is scaled down to near zero."""

    bootstrap_origin_airports: tuple[str, ...] = DEFAULT_BOOTSTRAP_ORIGINS
    """~30 important European origin airports the bootstrap job prioritises
    (origin-first — V9 §6). Env ``MARKET_PRIOR_BOOTSTRAP_ORIGINS``."""

    @classmethod
    def from_env(cls) -> "SearchIntelConfig":
        return cls(
            enabled=os.getenv("SEARCH_INTEL_ENABLED", "1").strip() not in ("0", "false", "no"),
            retention_days=_int("PRICE_MEMORY_RETENTION_DAYS", 180),
            top_k=max(1, _int("SEARCH_INTEL_TOP_K", 5)),
            weight_price_attractiveness=_float("SEARCH_INTEL_W_PRICE", 0.30),
            weight_confidence=_float("SEARCH_INTEL_W_CONFIDENCE", 0.20),
            weight_freshness=_float("SEARCH_INTEL_W_FRESHNESS", 0.15),
            weight_preference=_float("SEARCH_INTEL_W_PREFERENCE", 0.20),
            weight_feasibility=_float("SEARCH_INTEL_W_FEASIBILITY", 0.15),
            exploration_bonus=_float("SEARCH_INTEL_EXPLORE_BONUS", 0.10),
            explore_fraction=min(1.0, max(0.0, _float("SEARCH_INTEL_EXPLORE_FRACTION", 0.20))),
            explore_min_slots=max(0, _int("SEARCH_INTEL_EXPLORE_MIN_SLOTS", 1)),
            min_samples_for_aggregate=max(1, _int("PRICE_MEMORY_MIN_SAMPLES", 3)),
            cheap_percentile=_float("PRICE_MEMORY_CHEAP_PCT", 0.25),
            expensive_percentile=_float("PRICE_MEMORY_EXPENSIVE_PCT", 0.75),
            freshness_horizon_days=_float("PRICE_MEMORY_FRESHNESS_HORIZON_DAYS", 45.0),
            economics=ProviderEconomicsConfig(
                provider=os.getenv("PROVIDER_ECONOMICS_PROVIDER", "duffel").strip() or "duffel",
                included_searches_per_booking=_opt_float("DUFFEL_INCLUDED_SEARCHES_PER_BOOKING"),
                included_searches_flat=(
                    _int("DUFFEL_INCLUDED_SEARCHES_FLAT", 0) or None
                ),
                excess_search_fee=_opt_float("DUFFEL_EXCESS_SEARCH_FEE"),
                currency=os.getenv("DUFFEL_EXCESS_SEARCH_CURRENCY", "EUR").strip() or "EUR",
                excess_ratio_threshold=_opt_float("DUFFEL_EXCESS_RATIO_THRESHOLD"),
            ),
            prior_enabled=os.getenv("MARKET_PRIOR_ENABLED", "1").strip() not in ("0", "false", "no"),
            prior_retention_days=_int("MARKET_PRIOR_RETENTION_DAYS", 365),
            prior_horizon_bucket_days=_int_tuple(
                "MARKET_PRIOR_HORIZON_BUCKETS", (14, 30, 45, 60, 90, 120)
            ),
            prior_decay_half_life_days=_float("MARKET_PRIOR_DECAY_HALF_LIFE_DAYS", 120.0),
            prior_min_rows_for_band=max(1, _int("MARKET_PRIOR_MIN_ROWS_FOR_BAND", 2)),
            weight_prior_ceiling=min(1.0, max(0.0, _float("MARKET_PRIOR_WEIGHT_CEILING", 0.55))),
            live_supersedes_min_samples=max(1, _int("MARKET_PRIOR_LIVE_SUPERSEDES_MIN", 4)),
            bootstrap_origin_airports=_str_tuple(
                "MARKET_PRIOR_BOOTSTRAP_ORIGINS", DEFAULT_BOOTSTRAP_ORIGINS
            ),
        )


_CONFIG: SearchIntelConfig | None = None


def search_intel_config() -> SearchIntelConfig:
    """Process-wide config, read from the environment on first use."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = SearchIntelConfig.from_env()
    return _CONFIG


def reset_search_intel_config() -> None:
    """Tests set env then call this to force a re-read."""
    global _CONFIG
    _CONFIG = None
