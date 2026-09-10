"""Search Intelligence domain models (V9 Phase 1).

An interpretable, statistical market-intelligence layer — **not** a machine
learning milestone. Every model here answers a question an operator could
answer by hand given the data.

The load-bearing boundary this module defines:

    PRICE MEMORY IS NEVER A LIVE QUOTE.

A :class:`PriceObservation` records *what the market looked like at a past
moment*. It carries no method that yields a bookable amount, and neither does
:class:`HistoricalPriceSignal` (the aggregate). The only prices a customer is
ever charged come from a fresh provider revalidation
(:mod:`detoura.services.revalidation`) and the commercial engine
(:mod:`detoura.services.booking_commercial`) — never from here. Acquisition and
analytics may read Price Memory; checkout may not.

Provider-neutral by construction: a `PriceObservation` is Detoura's own shape,
assembled from a normalized :class:`~detoura.models.transport.TransportOption`,
not a Duffel offer.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


# ======================================================================
# Market identity
# ======================================================================
class TripShape(str, Enum):
    """The shape of the journey an observation belongs to. Kept explicit so a
    one-way fare is never averaged with a round-trip total."""

    ONE_WAY = "ONE_WAY"
    ROUND_TRIP = "ROUND_TRIP"
    MULTI_CITY_LEG = "MULTI_CITY_LEG"
    """One leg of a longer itinerary, priced on its own."""


class SearchModeTag(str, Enum):
    QUICK = "QUICK"
    SMART = "SMART"
    DEEP = "DEEP"
    UNKNOWN = "UNKNOWN"


def travelers_bucket(n: int) -> str:
    """Party sizes are bucketed for aggregation because airlines price per
    party and per remaining seat — a solo fare and a family-of-four fare are
    different markets. Buckets keep the market key stable without one-per-N
    fragmentation."""
    n = max(int(n), 1)
    if n == 1:
        return "1"
    if n == 2:
        return "2"
    if n <= 4:
        return "3-4"
    return "5+"


class MarketKey(BaseModel):
    """The identity of a market: a route on a date, for a party shape.

    Two observations share a market iff their :class:`MarketKey` is equal.
    Currency is deliberately **not** part of the key — a market can be observed
    in more than one currency and aggregation handles that per-currency rather
    than merging.
    """

    model_config = ConfigDict(frozen=True)

    provider: str = Field(min_length=1, max_length=40)
    origin: str = Field(min_length=1, max_length=64)
    destination: str = Field(min_length=1, max_length=64)
    departure_date: date
    trip_shape: TripShape = TripShape.ONE_WAY
    travelers_bucket: str = "1"

    @classmethod
    def build(
        cls, *, provider: str, origin: str, destination: str,
        departure_date: date, trip_shape: TripShape = TripShape.ONE_WAY,
        travelers: int = 1,
    ) -> "MarketKey":
        return cls(
            provider=provider.strip().lower(),
            origin=origin.strip().upper(),
            destination=destination.strip().upper(),
            departure_date=departure_date,
            trip_shape=trip_shape,
            travelers_bucket=travelers_bucket(travelers),
        )

    def as_tuple(self) -> tuple:
        return (
            self.provider, self.origin, self.destination,
            self.departure_date.isoformat(), self.trip_shape.value,
            self.travelers_bucket,
        )


# ======================================================================
# Contribution attribution
# ======================================================================
class ContributionClass(str, Enum):
    """What one provider acquisition call ultimately did for the search.

    Determined from stable provenance ids propagated through the pipeline —
    call → offers → normalized offers → optimizer candidate edges → results —
    never inferred approximately.
    """

    NO_USABLE_OFFER = "A"          # produced nothing that normalized/retained
    USABLE_BUT_UNUSED = "B"        # retained offers, none entered a candidate
    OPTIMIZER_CANDIDATE = "C"      # contributed to the optimizer candidate set
    TOP_K = "D"                    # contributed to a Top-K recommendation
    WINNER = "E"                   # contributed to the winning recommendation

    @property
    def rank(self) -> int:
        return {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}[self.value]

    @property
    def is_useful(self) -> bool:
        return self.rank >= ContributionClass.OPTIMIZER_CANDIDATE.rank


# ======================================================================
# Price observation
# ======================================================================
class PriceObservation(BaseModel):
    """One historical market observation. Immutable once written.

    **Not a quote.** There is no ``.amount`` a caller can treat as bookable and
    no conversion to a live price. Monetary fields are the amount *as observed*
    at :attr:`observed_at`, for statistics and audit only.
    """

    model_config = ConfigDict(frozen=True)

    # --- identity ---
    observation_id: str = Field(min_length=8, max_length=64)
    observed_at: datetime
    provider: str = Field(min_length=1, max_length=40)

    # --- market ---
    origin: str = Field(min_length=1, max_length=64)
    destination: str = Field(min_length=1, max_length=64)
    departure_date: date
    trip_shape: TripShape = TripShape.ONE_WAY
    return_date: date | None = None
    travelers: int = Field(default=1, ge=1)
    travelers_bucket: str = "1"

    # --- offer characteristics (as observed; any may be absent) ---
    total_amount_minor: int = Field(ge=0)
    """Party total, integer minor units, in :attr:`currency`. The number the
    provider quoted at observation time — not a current fare."""
    per_person_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    direct: bool | None = None
    stops: int | None = Field(default=None, ge=0)
    marketing_carrier: str | None = None
    operating_carrier: str | None = None
    cabin: str | None = None
    baggage_cabin: str | None = None
    baggage_checked: str | None = None
    offer_count_for_edge: int | None = Field(default=None, ge=0)
    """How many offers the same acquisition call returned — context for how
    representative this observation is."""

    # --- acquisition context ---
    search_id: str = Field(min_length=1, max_length=64)
    acquisition_call_id: str = Field(min_length=1, max_length=64)
    search_mode: SearchModeTag = SearchModeTag.UNKNOWN
    candidate_reason: str = Field(default="", max_length=200)
    exploration: bool = False
    """True = this observation came from an EXPLORE acquisition call."""
    candidate_rank: int | None = Field(default=None, ge=0)
    """The candidate's rank before acquisition (0 = top)."""
    provider_call_ordinal: int | None = Field(default=None, ge=0)
    provider_call_budget: int | None = Field(default=None, ge=0)

    # --- quality / outcome ---
    normalized_ok: bool = True
    retained_after_limits: bool = True
    entered_candidate_set: bool = False
    contributed_to_top_k: bool = False
    contributed_to_winner: bool = False

    def market_key(self) -> MarketKey:
        return MarketKey(
            provider=self.provider.lower(),
            origin=self.origin.upper(),
            destination=self.destination.upper(),
            departure_date=self.departure_date,
            trip_shape=self.trip_shape,
            travelers_bucket=self.travelers_bucket or travelers_bucket(self.travelers),
        )

    @property
    def contribution_class(self) -> ContributionClass:
        if self.contributed_to_winner:
            return ContributionClass.WINNER
        if self.contributed_to_top_k:
            return ContributionClass.TOP_K
        if self.entered_candidate_set:
            return ContributionClass.OPTIMIZER_CANDIDATE
        if self.retained_after_limits and self.normalized_ok:
            return ContributionClass.USABLE_BUT_UNUSED
        return ContributionClass.NO_USABLE_OFFER


# ======================================================================
# Aggregation output
# ======================================================================
class MarketConfidence(str, Enum):
    """Interpretable trust in a market aggregate. Not an ML score.

    Derived deterministically from sample count, recency and price
    consistency — the exact formula is in
    :func:`detoura.services.market_intel.confidence_for` and documented in
    docs/V9_PHASE1_SEARCH_INTELLIGENCE.md.
    """

    NONE = "NONE"     # no observations at all
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ConfidenceBreakdown(BaseModel):
    """The components behind a :class:`MarketConfidence`, each in [0, 1], so the
    verdict is always explainable."""

    model_config = ConfigDict(frozen=True)

    sample_component: float = Field(ge=0.0, le=1.0)
    recency_component: float = Field(ge=0.0, le=1.0)
    consistency_component: float = Field(ge=0.0, le=1.0)
    score: float = Field(ge=0.0, le=1.0)
    verdict: MarketConfidence


class HistoricalPriceSignal(BaseModel):
    """Aggregated market intelligence for one :class:`MarketKey` and currency.

    **Explicitly not a fare.** Every field is a statistic over past
    observations. :meth:`__bool__`-style checks aside, nothing here yields an
    amount a caller may present as "current price" or send to checkout. The
    field names say "observed" on purpose.
    """

    model_config = ConfigDict(frozen=True)

    market: MarketKey
    currency: str = Field(min_length=3, max_length=3)

    sample_count: int = Field(ge=0)
    freshest_observation_at: datetime | None = None
    oldest_observation_at: datetime | None = None

    # percentiles are suppressed (``None``) below the min-sample threshold
    median_observed_minor: int | None = None
    cheap_reference_minor: int | None = None
    expensive_reference_minor: int | None = None
    min_observed_minor: int | None = None
    max_observed_minor: int | None = None
    #: coefficient of variation of the observed per-person prices, for
    #: diagnostics and the consistency component of confidence
    price_cv: float | None = None

    direct_sample_count: int = 0
    connecting_sample_count: int = 0

    useful_offer_rate: float | None = None
    top_k_contribution_rate: float | None = None
    winner_contribution_rate: float | None = None

    confidence: ConfidenceBreakdown

    #: An always-present, machine- and human-readable reminder that this is not
    #: a bookable price. Surfaces in every DTO built from this signal.
    not_a_quote: bool = True

    @property
    def has_usable_stats(self) -> bool:
        return self.median_observed_minor is not None
