"""Structured observability objects.

The optimizer never explains itself with log strings only: every decision that
removes a candidate produces a typed record, so tests (and the API in
``debug=True`` mode) can assert on *why* something happened.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class RejectionReason(str, Enum):
    """Why a state or itinerary was discarded."""

    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    DURATION_EXCEEDED = "DURATION_EXCEEDED"
    DURATION_UNDERUSED = "DURATION_UNDERUSED"
    AVOIDED_DESTINATION = "AVOIDED_DESTINATION"
    MISSING_MANDATORY_DESTINATION = "MISSING_MANDATORY_DESTINATION"
    DUPLICATE_DESTINATION = "DUPLICATE_DESTINATION"
    MIN_CITY_STAY_VIOLATED = "MIN_CITY_STAY_VIOLATED"
    MAX_CITIES_EXCEEDED = "MAX_CITIES_EXCEEDED"
    TRANSPORT_TYPE_NOT_ALLOWED = "TRANSPORT_TYPE_NOT_ALLOWED"
    DATE_WINDOW_VIOLATED = "DATE_WINDOW_VIOLATED"
    NOT_RETURNED_TO_ORIGIN = "NOT_RETURNED_TO_ORIGIN"
    UNREACHABLE_RETURN_BUDGET = "UNREACHABLE_RETURN_BUDGET"
    UNREACHABLE_RETURN_TIME = "UNREACHABLE_RETURN_TIME"
    UNAFFORDABLE_ACCOMMODATION = "UNAFFORDABLE_ACCOMMODATION"
    NO_ACCOMMODATION_AVAILABLE = "NO_ACCOMMODATION_AVAILABLE"
    NO_CITIES_VISITED = "NO_CITIES_VISITED"
    INVALID_CONNECTION = "INVALID_CONNECTION"


class FilterStage(str, Enum):
    """Which post-processing stage removed a completed itinerary."""

    PARETO = "PARETO"
    DUPLICATE_ROUTE = "DUPLICATE_ROUTE"
    """Exactly the same airports and cities, a different time of day (V3)."""
    DIVERSITY = "DIVERSITY"
    MAX_RESULTS = "MAX_RESULTS"


class RejectedState(BaseModel):
    """A generated state that failed constraint validation."""

    iteration: int
    route: list[str]
    """Human-readable node sequence, e.g. ``["DUS", "London", "DUS"]``."""
    reason: RejectionReason
    detail: str = ""


class PrunedState(BaseModel):
    """A valid state that lost the beam-width competition."""

    iteration: int
    route: list[str]
    estimated_score: float


class IterationDebug(BaseModel):
    """One beam-search expansion round."""

    iteration: int
    states_in: int = 0
    generated: int = 0
    rejected: int = 0
    surviving: int = 0
    beam_width: int = 0
    pruned_by_beam: int = 0
    kept: int = 0
    completed_found: int = 0
    rejection_counts: dict[RejectionReason, int] = Field(default_factory=dict)
    kept_routes: list[str] = Field(default_factory=list)
    pruned_examples: list[PrunedState] = Field(default_factory=list)
    rejected_examples: list[RejectedState] = Field(default_factory=list)

    def render(self) -> str:
        """The compact human-readable form shown in development mode."""
        lines = [
            f"Iteration {self.iteration}",
            "------------------",
            f"States in:    {self.states_in}",
            f"Generated:    {self.generated}",
            f"Rejected:     {self.rejected}",
            f"Remaining:    {self.surviving}",
            f"Beam width:   {self.beam_width}",
            f"Beam pruning: {self.pruned_by_beam}",
            f"Kept:         {self.kept}",
            f"Completed:    {self.completed_found}",
        ]
        if self.rejection_counts:
            lines.append("Rejections:")
            for reason, count in sorted(
                self.rejection_counts.items(), key=lambda kv: (-kv[1], kv[0].value)
            ):
                lines.append(f"  {reason.value}: {count}")
        return "\n".join(lines)


class FilteredItinerary(BaseModel):
    """A completed itinerary removed by Pareto or diversity filtering."""

    route: list[str]
    stage: FilterStage
    reason: str
    dominated_by: list[str] | None = None
    similarity: float | None = None


class SearchDebug(BaseModel):
    """The full, structured trace of one planning run."""

    origin_airports: list[str] = Field(default_factory=list)
    start_dates: list[str] = Field(default_factory=list)
    initial_states: int = 0
    effective_beam_width: int = 0
    iterations: list[IterationDebug] = Field(default_factory=list)
    completed_itineraries: int = 0
    scored_itineraries: list[dict] = Field(default_factory=list)
    pareto_input: int = 0
    pareto_kept: int = 0
    diversity_input: int = 0
    diversity_kept: int = 0
    filtered: list[FilteredItinerary] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def total_generated(self) -> int:
        return sum(it.generated for it in self.iterations)

    @property
    def total_rejected(self) -> int:
        return sum(it.rejected for it in self.iterations)

    def render(self) -> str:
        """A readable report of the whole search."""
        blocks = [
            "Search setup",
            "------------------",
            f"Origin airports: {', '.join(self.origin_airports)}",
            f"Start dates:     {', '.join(self.start_dates)}",
            f"Initial states:  {self.initial_states}",
            "",
        ]
        for iteration in self.iterations:
            blocks.append(iteration.render())
            blocks.append("")
        blocks.extend(
            [
                "Post-processing",
                "------------------",
                f"Completed itineraries: {self.completed_itineraries}",
                f"Pareto:    {self.pareto_input} -> {self.pareto_kept}",
                f"Diversity: {self.diversity_input} -> {self.diversity_kept}",
            ]
        )
        return "\n".join(blocks)
