"""Result models returned by the planner."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

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

    score_breakdown: ScoreBreakdown | None = None
    baseline_comparison: BaselineComparison | None = None

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

    baseline: BaselineResult | None = None
    recommendations: list[Itinerary] = Field(default_factory=list)
    metadata: PlannerMetadata
    debug: SearchDebug | None = None
