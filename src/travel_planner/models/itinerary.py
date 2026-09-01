"""Result models returned by the planner."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ..profiles import ProfileName
from .debug import SearchDebug
from .transport import TransportOption


class ScoreBreakdown(BaseModel):
    """Per-component scores plus the weighted total.

    Keeping the components makes "why did this itinerary win?" answerable
    without re-running the optimizer.
    """

    model_config = ConfigDict(frozen=True)

    budget: float
    preference: float
    destination: float
    convenience: float
    time: float
    diversity: float
    total: float
    weights: dict[str, float] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class TravelValueBreakdown(BaseModel):
    """The V2 objective, component by component.

    Everything needed to answer "why did this trip win?" without re-running the
    optimizer - and everything a future LLM explainer needs to write prose
    without inventing a number.
    """

    model_config = ConfigDict(frozen=True)

    profile: ProfileName

    cost: float
    experience: float
    preferences: float
    time: float
    diversity: float

    total: float
    weights: dict[str, float] = Field(default_factory=dict)

    budget_utilization: float = 0.0
    usable_destination_minutes: int = 0
    transport_minutes: int = 0
    usable_ratio: float = 0.0
    """Usable destination time as a share of the requested days."""


class CostBreakdown(BaseModel):
    """Where the money goes. All figures are party totals."""

    model_config = ConfigDict(frozen=True)

    transport: float = 0.0
    accommodation: float = 0.0
    ground_transfer: float = 0.0

    @property
    def total(self) -> float:
        return round(self.transport + self.accommodation + self.ground_transfer, 2)


class StaySummary(BaseModel):
    """One city stay as presented to a caller."""

    model_config = ConfigDict(frozen=True)

    city: str
    arrival: datetime
    departure: datetime
    nights: int
    accommodation_cost: float = 0.0
    accommodation_name: str | None = None
    accommodation_tier: str | None = None
    usable_minutes: int = 0


class ExplanationFactor(str, Enum):
    """Deterministic, structured reasons an itinerary was recommended.

    Prose belongs in the LLM layer; the optimizer emits facts.
    """

    FITS_BUDGET = "fits_budget"
    GOOD_BUDGET_USAGE = "good_budget_usage"
    LEAVES_BUDGET_UNUSED = "leaves_budget_unused"
    CHEAPER_THAN_BASELINE = "cheaper_than_baseline"
    STRONG_PREFERENCE_MATCH = "strong_preference_match"
    HIGH_DESTINATION_QUALITY = "high_destination_quality"
    SINGLE_CITY = "single_city"
    TWO_CITIES = "two_cities"
    MULTI_CITY = "multi_city"
    REASONABLE_TRAVEL_TIME = "reasonable_travel_time"
    HEAVY_TRAVEL_TIME = "heavy_travel_time"
    GOOD_USE_OF_WINDOW = "good_use_of_window"
    SHORT_TRIP = "short_trip"
    LATE_ARRIVAL = "late_arrival"
    EARLY_DEPARTURE = "early_departure"
    VISITS_PREFERRED_DESTINATION = "visits_preferred_destination"
    VISITS_MANDATORY_DESTINATION = "visits_mandatory_destination"
    LOW_ACCOMMODATION_COST = "low_accommodation_cost"
    NEARBY_DEPARTURE_AIRPORT = "nearby_departure_airport"


class BaselineResult(BaseModel):
    """The naive single-destination round trip used as a reference point.

    This is what a conventional "cheapest return flight to X" search would
    produce for the user's preferred destination.
    """

    destination: str
    total_cost: float
    currency: str = "EUR"
    duration_days: float
    legs: list[TransportOption] = Field(default_factory=list)
    total_travel_minutes: int = 0
    cost_breakdown: CostBreakdown = Field(default_factory=CostBreakdown)
    nights: int = 0


class BaselineComparison(BaseModel):
    """How a candidate itinerary compares against the baseline."""

    baseline_destination: str
    baseline_cost: float
    money_saved: float
    additional_cities: int
    additional_travel_minutes: int


class Itinerary(BaseModel):
    """A complete, budget- and duration-feasible round trip."""

    rank: int
    score: float

    total_cost: float
    currency: str = "EUR"

    duration_days: float

    origin_airport: str
    return_airport: str

    cities: list[str]
    stay_days: list[int] = Field(default_factory=list)

    legs: list[TransportOption]

    total_travel_minutes: int

    departure: datetime
    arrival: datetime

    cost_breakdown: CostBreakdown = Field(default_factory=CostBreakdown)
    stays: list[StaySummary] = Field(default_factory=list)

    ground_transfer_minutes: int = 0
    usable_destination_minutes: int = 0

    profile: ProfileName | None = None
    value_breakdown: TravelValueBreakdown | None = None
    """The V2 objective that actually produced :attr:`score`."""
    score_breakdown: ScoreBreakdown | None = None
    """The V1 six-component diagnostic, kept for continuity and tuning."""

    explanation_factors: list[ExplanationFactor] = Field(default_factory=list)

    baseline_comparison: BaselineComparison | None = None

    @property
    def total_transport_minutes(self) -> int:
        """All time in transit, ground transfers included."""
        return self.total_travel_minutes + self.ground_transfer_minutes

    @property
    def preference_score(self) -> float:
        return self.value_breakdown.preferences if self.value_breakdown else 0.0

    @property
    def experience_score(self) -> float:
        return self.value_breakdown.experience if self.value_breakdown else 0.0

    @property
    def route_nodes(self) -> list[str]:
        """Full node sequence including the origin and return airports."""
        if not self.legs:
            return [self.origin_airport]
        return [self.legs[0].origin] + [leg.destination for leg in self.legs]

    def route_label(self) -> str:
        return " -> ".join(self.route_nodes)


class PlannerMetadata(BaseModel):
    """Non-itinerary facts about a planning run."""

    origin: str
    profile: ProfileName = ProfileName.BEST_VALUE
    origin_airports: list[str] = Field(default_factory=list)
    start_dates: list[str] = Field(default_factory=list)
    beam_width: int = 0
    max_cities: int = 0
    states_generated: int = 0
    states_rejected: int = 0
    completed_itineraries: int = 0
    pareto_kept: int = 0
    diversity_kept: int = 0
    returned: int = 0
    elapsed_seconds: float = 0.0
    currency: str = "EUR"
    warnings: list[str] = Field(default_factory=list)


class PlanResult(BaseModel):
    """The planner's full answer."""

    profile: ProfileName = ProfileName.BEST_VALUE
    baseline: BaselineResult | None = None
    recommendations: list[Itinerary] = Field(default_factory=list)
    metadata: PlannerMetadata
    debug: SearchDebug | None = None
