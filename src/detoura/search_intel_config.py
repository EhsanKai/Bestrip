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

    # --- V9 Phase 3: Destination Attractiveness in candidate scoring -------
    weight_attractiveness: float = 0.14
    """Weight of global destination attractiveness in the acquisition
    Opportunity score (§A3) — additive alongside price/confidence/freshness/
    preference/feasibility/supply/contribution, never replacing any of them.
    A candidate with an UNKNOWN profile scores the neutral 0.5 here, same as
    every other unset component — attractiveness can raise a candidate's
    acquisition priority, never disqualify one for having no profile yet."""
    attractiveness_model_version: int = 1
    """Which :mod:`detoura.models.attractiveness` model version acquisition
    scoring reads. Bumping this does not reinterpret old rows — see
    ``persistence/attractiveness.py``."""
    explore_rotation_epsilon: float = 0.05
    """Fix for an independent-QA finding (UNKNOWN starvation): a small,
    deterministic, per-(destination, departure_date) bonus added to EXPLORE
    candidates only (``candidate_funnel.py::_exploration_rotation_bonus``),
    so a destination that is systematically slightly behind ~200 other
    all-EXPLORE competitors is not selected literally zero times across
    every date a catalog is ever searched against — the EXPLORE-vs-EXPLOIT
    floor alone only guards that split, not fairness *within* the EXPLORE
    pool. ``0`` disables the rotation entirely (reproduces the pre-fix
    behaviour) for callers/tests that need the plain score ordering."""

    # --- V9 Phase 3: geographic / experience diversity in the pre-acquisition
    # --- candidate funnel (§A, §C6) — softer and catalog-relative, replacing
    # --- the Phase 2 crude subregion-only soft cap.
    geo_redundancy_km_scale: float = 250.0
    """Two candidates within this many km of each other are considered
    "close"; the penalty fades smoothly past it (soft, not a hard ban —
    §C2/§C5). Roughly "day-trip-adjacent" without hardcoding a specific
    country's geography."""
    geo_redundancy_penalty: float = 0.10
    """Score penalty applied per already-selected candidate within
    ``geo_redundancy_km_scale``, beyond a small free allowance — see
    :func:`detoura.services.geo.redundancy_penalty`."""
    geo_redundancy_free_allowance: int = 1
    """How many close candidates are free before the penalty starts (so two
    genuinely excellent nearby destinations never automatically fight)."""
    experience_similarity_penalty: float = 0.10
    """Score penalty scale for high experience-feature-vector similarity to
    an already-selected candidate (§C3), same soft/free-allowance shape as
    the geographic penalty."""
    experience_similarity_threshold: float = 0.80
    """Cosine similarity above which two destinations are treated as
    "very similar" for the experience-redundancy penalty."""

    # --- V9 Phase 3: final Recommendation Portfolio (§C4, §D) --------------
    portfolio_size: int = 10
    """The Top-N a portfolio reranking pass selects (the product's "Top 10")."""
    portfolio_geo_penalty_scale: float = 0.55
    """Maximum fractional value reduction from geographic redundancy in the
    final portfolio pass — a soft ceiling, never 1.0 (§C5: a single
    exceptional nearby deal can never be reduced to worthless, since the
    fraction retained, ``1 - penalty``, never reaches 0). Chosen by
    benchmarking the Cologne fixture (``docs/V9_PHASE3_*.md``): materially
    lower values fail to stop cheap-nearby saturation; materially higher
    values start displacing genuinely excellent nearby deals."""
    portfolio_experience_penalty_scale: float = 0.55
    """Maximum fractional value reduction from experience-similarity
    redundancy in the final portfolio pass. Same benchmarking basis as
    ``portfolio_geo_penalty_scale``."""
    value_weight_price: float = 0.30
    value_weight_attractiveness: float = 0.20
    value_weight_user_fit: float = 0.15
    value_weight_opportunity: float = 0.20
    value_weight_trip_quality: float = 0.10
    value_weight_novelty: float = 0.05
    """The six final-recommendation-value components (§D): price (via the
    cheapness curve), global attractiveness, user fit, market opportunity,
    trip/itinerary quality, and novelty/discovery. Weighted sum, each
    component in [0, 1], neutral 0.5 when a component is unknown for a given
    candidate — see :mod:`detoura.services.portfolio`. Not dominated by
    price alone, but the two "is this economically sound" signals together
    (price + market_opportunity, 0.50) meet or exceed the two "is this a
    nice place for anyone" signals together (attractiveness + user_fit,
    0.35) — retuned after independent adversarial QA found the original
    split (0.26/0.22/0.18/0.16/0.10/0.08) let a candidate priced 5-10x the
    field ceiling still win a slot on maxed attractiveness+user_fit alone
    against a realistic mid-priced rival, whenever the caller's
    market_opportunity/trip_quality/novelty were merely neutral (which they
    always were before a second fix threaded real per-search opportunity
    data through ``services.live_search``). Both fixes are needed: neutral
    defaults must not be free value, and price must not be a minority
    signal."""
    cheapness_curve_gamma: float = 1.6
    """Exponent of the price-value curve (§D1): ``value = t**gamma`` where
    ``t`` is the candidate's position between the field's cheapest and
    priciest price, linear-normalized. ``gamma > 1`` compresses the
    difference between very-cheap prices (a bounded/nonlinear treatment so a
    €5-vs-€10 gap counts for less than a €15-vs-€30 gap of the same *ratio*
    — see ``services/portfolio.py`` and the Cologne benchmark). ``gamma=1``
    is the plain linear treatment; never < 1 (that would invert the guard)."""

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
            weight_attractiveness=_float("SEARCH_INTEL_W_ATTRACTIVENESS", 0.14),
            attractiveness_model_version=max(1, _int("ATTRACTIVENESS_MODEL_VERSION", 1)),
            explore_rotation_epsilon=max(0.0, _float("PHASE3_EXPLORE_ROTATION_EPSILON", 0.05)),
            geo_redundancy_km_scale=_float("PHASE3_GEO_REDUNDANCY_KM_SCALE", 250.0),
            geo_redundancy_penalty=_float("PHASE3_GEO_REDUNDANCY_PENALTY", 0.10),
            geo_redundancy_free_allowance=max(0, _int("PHASE3_GEO_REDUNDANCY_FREE", 1)),
            experience_similarity_penalty=_float("PHASE3_EXPERIENCE_SIMILARITY_PENALTY", 0.10),
            experience_similarity_threshold=min(
                1.0, max(0.0, _float("PHASE3_EXPERIENCE_SIMILARITY_THRESHOLD", 0.80))
            ),
            portfolio_size=max(1, _int("PHASE3_PORTFOLIO_SIZE", 10)),
            portfolio_geo_penalty_scale=min(
                0.95, max(0.0, _float("PHASE3_PORTFOLIO_GEO_PENALTY_SCALE", 0.55))
            ),
            portfolio_experience_penalty_scale=min(
                0.95, max(0.0, _float("PHASE3_PORTFOLIO_EXPERIENCE_PENALTY_SCALE", 0.55))
            ),
            cheapness_curve_gamma=max(1.0, _float("PHASE3_CHEAPNESS_CURVE_GAMMA", 1.6)),
            value_weight_price=_float("PHASE3_VALUE_W_PRICE", 0.30),
            value_weight_attractiveness=_float("PHASE3_VALUE_W_ATTRACTIVENESS", 0.20),
            value_weight_user_fit=_float("PHASE3_VALUE_W_USER_FIT", 0.15),
            value_weight_opportunity=_float("PHASE3_VALUE_W_OPPORTUNITY", 0.20),
            value_weight_trip_quality=_float("PHASE3_VALUE_W_TRIP_QUALITY", 0.10),
            value_weight_novelty=_float("PHASE3_VALUE_W_NOVELTY", 0.05),
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
