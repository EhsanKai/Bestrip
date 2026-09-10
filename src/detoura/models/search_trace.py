"""The SearchIntelligenceTrace (V9 Phase 1).

One trace per real-supply search: why each candidate market was chosen for
acquisition, what each provider call returned, how it fed the optimizer, and
what the search cost. It is the audit record behind every acquisition decision.

**No traveller PII.** A trace carries a search id, an origin, dates, a mode and
provider metrics — never a name, email, phone, DOB or document. The search id
is an operational attribution handle, not a person.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from .search_intel import (
    AcquisitionStance,
    ContributionClass,
    MarketConfidence,
    SearchModeTag,
)


class CandidateDecision(BaseModel):
    """Why one market/edge was selected or skipped for acquisition."""

    model_config = ConfigDict(frozen=True)

    market: str
    """"origin→destination" for readability; the structured key is on the
    observations."""
    pre_acquisition_rank: int = Field(ge=0)
    stance: AcquisitionStance
    selected: bool
    selection_reason: str = Field(default="", max_length=240)
    intelligence_confidence: MarketConfidence = MarketConfidence.NONE
    historical_signal_used: bool = False
    baseline_score: float | None = None
    score_components: dict = Field(default_factory=dict)


class ProviderCallOutcome(BaseModel):
    """What one acquisition call (one AcquisitionEdge fetch) produced."""

    model_config = ConfigDict(frozen=True)

    acquisition_call_id: str
    edge: str                       # "ORIG→DEST on YYYY-MM-DD"
    ordinal: int = Field(ge=0)
    stance: AcquisitionStance
    from_cache: bool = False
    offers_received: int = 0
    offers_normalized: int = 0
    offers_retained: int = 0
    error: str = ""
    rate_limited: bool = False
    timed_out: bool = False
    elapsed_ms: float = 0.0
    contributed_to_optimizer: bool = False
    contributed_to_top_k: bool = False
    contributed_to_winner: bool = False

    @property
    def contribution_class(self) -> ContributionClass:
        if self.contributed_to_winner:
            return ContributionClass.WINNER
        if self.contributed_to_top_k:
            return ContributionClass.TOP_K
        if self.contributed_to_optimizer:
            return ContributionClass.OPTIMIZER_CANDIDATE
        if self.offers_retained > 0:
            return ContributionClass.USABLE_BUT_UNUSED
        return ContributionClass.NO_USABLE_OFFER


class SearchEconomicsSnapshot(BaseModel):
    """The provider-search economics for this one search. UNKNOWN stays
    UNKNOWN — a figure that cannot be computed is ``None``, never 0."""

    model_config = ConfigDict(frozen=True)

    provider_searches_performed: int = 0
    included_search_allowance: float | None = None
    estimated_excess_searches: float | None = None
    estimated_excess_search_cost_minor: int | None = None
    currency: str = "EUR"
    economics_configured: bool = False


class SearchIntelligenceTrace(BaseModel):
    """The full record of one real-supply search's intelligence."""

    model_config = ConfigDict(frozen=True)

    search_id: str = Field(min_length=1, max_length=64)
    started_at: datetime
    finished_at: datetime | None = None
    provider: str = "duffel"
    origin: str
    date_from: date
    date_to: date
    flexible: bool = False
    duration_days: int = Field(ge=1)
    travelers: int = Field(ge=1)
    search_mode: SearchModeTag = SearchModeTag.UNKNOWN

    provider_call_budget: int = 0
    provider_calls_used: int = 0
    provider_calls_failed: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    calls_explore: int = 0
    calls_exploit: int = 0

    candidates: tuple[CandidateDecision, ...] = ()
    call_outcomes: tuple[ProviderCallOutcome, ...] = ()

    optimizer_offers_considered: int = 0
    recommendations_produced: int = 0
    winner_recommendation_id: str = ""

    economics: SearchEconomicsSnapshot = Field(default_factory=SearchEconomicsSnapshot)

    @property
    def explore_fraction(self) -> float | None:
        total = self.calls_explore + self.calls_exploit
        return round(self.calls_explore / total, 4) if total else None

    @property
    def useful_call_rate(self) -> float | None:
        if not self.call_outcomes:
            return None
        useful = sum(1 for c in self.call_outcomes if c.contribution_class.is_useful)
        return round(useful / len(self.call_outcomes), 4)

    @property
    def top_k_contribution_rate(self) -> float | None:
        if not self.call_outcomes:
            return None
        n = sum(1 for c in self.call_outcomes if c.contributed_to_top_k)
        return round(n / len(self.call_outcomes), 4)

    @property
    def winner_contribution_rate(self) -> float | None:
        if not self.call_outcomes:
            return None
        n = sum(1 for c in self.call_outcomes if c.contributed_to_winner)
        return round(n / len(self.call_outcomes), 4)
