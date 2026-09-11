"""Bootstrap Market Prior domain (V9 Phase 2).

Three market concepts must never be confused:

  A. BOOTSTRAP PRIOR        — external / pre-seeded *approximate* historical
     (:class:`BootstrapMarketPrior`)    market knowledge. Answers "where is it
                              probably worth spending a live provider request?"
  B. LIVE OBSERVATION       — an actual normalized price Detoura observed during
     (:class:`~detoura.models.search_intel.PriceObservation`, Phase 1)
                              live provider acquisition.
  C. CURRENT LIVE OFFER     — a provider offer eligible for acquisition /
     (:class:`~detoura.models.transport.TransportOption`)
                              revalidation / booking.

A Bootstrap Market Prior:

  * is NOT current, NOT guaranteed available, NOT bookable;
  * MUST NOT appear as a flight price in checkout, enter ``PriceBreakdown``,
    become supplier fare in booking provenance, satisfy revalidation, or
    replace Duffel acquisition;
  * MUST NOT be shown to a consumer as a current ticket price.

Enforced here: no model in this module carries an ``.amount`` /
``.as_quote()`` / ``.current_price`` / ``.bookable_amount`` accessor, and every
signal is stamped ``not_a_quote = True``. Dedicated tests
(``tests/test_v9_market_prior.py``) assert that commercial / booking /
revalidation code cannot import or consume these types.

Sparse by construction — see :class:`HorizonBucket` / :class:`SeasonBucket`.
We store a handful of representative bucket rows per market, never one row per
future calendar day (V9 §1, §4, §5).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class MarketDataSource(str, Enum):
    """Provenance of a market-intelligence datum. Kept distinct so bootstrap
    data is never silently mixed into live-observation aggregates."""

    BOOTSTRAP_PRIOR = "BOOTSTRAP_PRIOR"
    DETOURA_LIVE_OBSERVATION = "DETOURA_LIVE_OBSERVATION"


class HorizonBucket(str, Enum):
    """Booking-horizon buckets — how far ahead of departure the prior describes.

    Sparse and configurable: a few representative horizons rather than 365
    exact days. The default set (``SEARCH_INTEL`` config, env
    ``MARKET_PRIOR_HORIZON_BUCKETS``) is 14 / 30 / 45 / 60 / 90 / 120 days,
    chosen to span "last-minute" through "book early". ``UNKNOWN`` is a real
    value for a source that gives no horizon."""

    H14 = "H14"
    H30 = "H30"
    H45 = "H45"
    H60 = "H60"
    H90 = "H90"
    H120 = "H120"
    UNKNOWN = "UNKNOWN"

    @property
    def days(self) -> int | None:
        return None if self is HorizonBucket.UNKNOWN else int(self.value[1:])

    @classmethod
    def for_days(cls, days: int) -> "HorizonBucket":
        """Snap an arbitrary lead time onto the nearest bucket."""
        table = [(14, cls.H14), (30, cls.H30), (45, cls.H45),
                 (60, cls.H60), (90, cls.H90), (120, cls.H120)]
        best = min(table, key=lambda t: abs(t[0] - days))
        return best[1]


class SeasonBucket(str, Enum):
    """Coarse seasonal demand bucket. Derived from the departure month, kept
    alongside the raw month so a source that only knows "summer" and one that
    knows "July" both fit."""

    LOW = "LOW"          # deep off-season
    SHOULDER = "SHOULDER"
    PEAK = "PEAK"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def for_month(cls, month: int) -> "SeasonBucket":
        # Northern-hemisphere leisure travel: Jun-Aug + Dec peak, Apr-May +
        # Sep-Oct shoulder, the rest low. Coarse on purpose.
        if month in (6, 7, 8, 12):
            return cls.PEAK
        if month in (4, 5, 9, 10):
            return cls.SHOULDER
        return cls.LOW


class WeekdayClass(str, Enum):
    WEEKDAY = "WEEKDAY"
    WEEKEND = "WEEKEND"
    UNKNOWN = "UNKNOWN"


class DurationBucket(str, Enum):
    SHORT = "SHORT"       # 1-3 nights
    MEDIUM = "MEDIUM"     # 4-7 nights
    LONG = "LONG"         # 8+ nights
    UNKNOWN = "UNKNOWN"

    @classmethod
    def for_nights(cls, nights: int) -> "DurationBucket":
        if nights <= 3:
            return cls.SHORT
        if nights <= 7:
            return cls.MEDIUM
        return cls.LONG


class PriorConfidence(str, Enum):
    """Interpretable, matching the Phase 1 semantics — not an ML score."""

    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# ======================================================================
# The stored prior
# ======================================================================
class BootstrapMarketPrior(BaseModel):
    """One sparse bootstrap datum for a market × time-context bucket.

    **Not a quote.** No accessor yields a bookable amount; the monetary fields
    are *approximate historical statistics* the source supplied, in minor units
    with an explicit currency, and any the source did not supply are ``None``
    (UNKNOWN, never 0)."""

    model_config = ConfigDict(frozen=True)

    # --- identity ---
    prior_id: str = Field(min_length=8, max_length=64)
    source: str = Field(min_length=1, max_length=64)
    """The import source id — a fixture name, a feed id. Never presented as an
    authoritative dataset."""
    source_version: str = Field(default="", max_length=64)
    imported_at: datetime
    source_date: date | None = None
    """The date the source's statistics are *as of*, when the source states it —
    drives freshness/decay. ``None`` = the source did not say."""

    # --- market ---
    origin_airport: str = Field(min_length=3, max_length=3)
    destination_airport: str = Field(min_length=3, max_length=3)
    destination_id: str | None = None

    # --- time context (sparse buckets) ---
    season: SeasonBucket = SeasonBucket.UNKNOWN
    month: int | None = Field(default=None, ge=1, le=12)
    horizon_bucket: HorizonBucket = HorizonBucket.UNKNOWN
    weekday_class: WeekdayClass = WeekdayClass.UNKNOWN
    duration_bucket: DurationBucket = DurationBucket.UNKNOWN

    # --- statistics (as supplied; absent -> None) ---
    currency: str = Field(min_length=3, max_length=3)
    sample_count: int | None = Field(default=None, ge=0)
    observed_low_minor: int | None = Field(default=None, ge=0)
    """A lower reference / cheap-end figure the source reported."""
    median_minor: int | None = Field(default=None, ge=0)
    typical_minor: int | None = Field(default=None, ge=0)
    """A "typical price" if the source reports one distinct from the median."""
    observed_high_minor: int | None = Field(default=None, ge=0)
    confidence: PriorConfidence = PriorConfidence.LOW

    # --- availability context (as supplied) ---
    direct_possible: bool | None = None
    weekly_frequency: int | None = Field(default=None, ge=0)
    """Approx weekly departures on the route, if the source gives it."""
    carrier_count: int | None = Field(default=None, ge=0)

    not_a_quote: bool = True
    provenance_version: int = 1

    def market_tuple(self) -> tuple:
        return (self.origin_airport, self.destination_airport,
                self.season.value, self.horizon_bucket.value)


# ======================================================================
# Aggregation output
# ======================================================================
class HistoricalMarketPriorSignal(BaseModel):
    """Interpretable prior intelligence for one candidate market.

    Expected price bands here are **INTERNAL INTELLIGENCE** for acquisition
    scoring only — never a current fare, never surfaced to a consumer, never a
    checkout input. ``not_a_quote`` is stamped and every monetary field is
    named ``*_estimate``."""

    model_config = ConfigDict(frozen=True)

    origin_airport: str
    destination_airport: str
    destination_id: str | None = None
    currency: str

    prior_available: bool
    row_count: int = 0
    source: str = ""
    source_version: str = ""
    source_date: date | None = None
    age_days: float | None = None

    # internal expected band (minor units) — suppressed when too thin
    low_estimate_minor: int | None = None
    typical_estimate_minor: int | None = None
    high_estimate_minor: int | None = None

    #: [0, 1] — how attractive this market's expected price is relative to the
    #: candidate field, credited only at >= MEDIUM confidence. 0.5 = neutral.
    relative_price_attractiveness: float = 0.5

    direct_supply_signal: float | None = None
    """[0, 1] rough supply/frequency signal, or ``None`` when the source is
    silent."""

    confidence: PriorConfidence = PriorConfidence.NONE
    #: components behind the confidence verdict, each in [0, 1]
    confidence_components: dict = Field(default_factory=dict)

    not_a_quote: bool = True

    @property
    def has_band(self) -> bool:
        return self.typical_estimate_minor is not None
