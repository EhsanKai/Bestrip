"""Planner configuration.

Every tunable number in the optimizer lives here so the algorithms stay free of
magic constants.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .usable_time import DEFAULT_DAY_END, DEFAULT_DAY_START, usable_day_minutes
from .profiles import DEFAULT_PROFILE, ProfileName


class ScoreWeights(BaseModel):
    """Relative importance of the six scoring components."""

    model_config = ConfigDict(frozen=True)

    budget: float = Field(default=0.40, ge=0.0)
    preference: float = Field(default=0.20, ge=0.0)
    destination: float = Field(default=0.15, ge=0.0)
    convenience: float = Field(default=0.10, ge=0.0)
    time: float = Field(default=0.10, ge=0.0)
    diversity: float = Field(default=0.05, ge=0.0)

    @model_validator(mode="after")
    def _check_nonzero(self) -> "ScoreWeights":
        if self.total <= 0:
            raise ValueError("at least one score weight must be positive")
        return self

    @property
    def total(self) -> float:
        return (
            self.budget
            + self.preference
            + self.destination
            + self.convenience
            + self.time
            + self.diversity
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "budget": self.budget,
            "preference": self.preference,
            "destination": self.destination,
            "convenience": self.convenience,
            "time": self.time,
            "diversity": self.diversity,
        }

    def normalized(self) -> dict[str, float]:
        """Weights rescaled to sum to 1.0, so the total score stays in [0, 1]."""
        total = self.total
        return {name: value / total for name, value in self.as_dict().items()}


class PlannerConfig(BaseModel):
    """Search, scoring and filtering configuration."""

    model_config = ConfigDict(frozen=True)

    # --- beam search -------------------------------------------------
    beam_width: int = Field(default=20, ge=1)
    max_results: int = Field(default=5, ge=1)
    max_cities: int = Field(default=4, ge=1)
    beam_slots_per_route: int = Field(default=2, ge=1)
    """Cap on beam slots sharing one city sequence.

    Without it the beam fills with the same trip departing from four airports on
    two dates at three times of day, and the search degenerates into a very
    expensive single-branch walk.
    """

    # --- stay durations ----------------------------------------------
    min_city_stay_days: int = Field(default=1, ge=1)
    max_city_stay_days: int = Field(default=4, ge=1)
    min_duration_utilization: float = Field(default=0.6, ge=0.0, le=1.0)
    """Fraction of the requested duration a finished itinerary must actually use.

    ``duration_days`` is an upper bound on elapsed time, but a user asking for
    five days does not want a 40-hour trip back. Set to ``0.0`` to disable and
    accept any itinerary that fits the window.
    """

    # --- origin discovery --------------------------------------------
    max_origin_distance_km: float = Field(default=250.0, gt=0.0)
    max_origin_airports: int = Field(default=4, ge=1)

    # --- scoring ------------------------------------------------------
    score_weights: ScoreWeights = Field(default_factory=ScoreWeights)
    max_travel_time_fraction: float = Field(default=0.25, gt=0.0, le=1.0)
    """Above this fraction of the trip spent in transit, TimeScore hits zero."""
    preferred_destination_bonus: float = Field(default=0.5, ge=0.0, le=1.0)
    must_visit_bonus: float = Field(default=0.3, ge=0.0, le=1.0)

    # --- filtering ----------------------------------------------------
    enable_pareto: bool = True
    enable_diversity: bool = True
    diversity_similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    """Two itineraries are "the same trip" above this Jaccard city overlap."""

    # --- accommodation (V2) -------------------------------------------
    enable_accommodation: bool = True
    """Charge for city stays. ``False`` restores the V1 "stays are free" model."""
    accommodation_options_per_stay: int = Field(default=1, ge=1)
    """How many accommodation tiers each stay branches on.

    ``1`` takes the cheapest sufficient room, which keeps the state space the
    same size as V1. Raising it lets the search trade room quality against
    everything else, at a multiplicative cost in states.
    """

    # --- ground transfer (V2) -----------------------------------------
    enable_ground_transfer: bool = True
    """Charge for getting from the user's origin to the departure airport."""

    # --- usable destination time (V2) ---------------------------------
    usable_day_start: time = Field(default=DEFAULT_DAY_START)
    usable_day_end: time = Field(default=DEFAULT_DAY_END)
    """The window in which a traveler can actually do something with their day.

    Arriving after ``usable_day_end`` or departing before ``usable_day_start``
    therefore contributes nothing to the trip's usable time.
    """

    # --- travel value (V2) --------------------------------------------
    profile: ProfileName = DEFAULT_PROFILE
    """Default recommendation profile when the request does not name one."""
    budget_utilization_target: float = Field(default=0.6, gt=0.0, le=1.0)
    """Budget share a trip may use before CostScore starts falling meaningfully."""
    comfortable_days_per_city: float = Field(default=2.0, gt=0.0)
    """Days per city at or above which an itinerary's pace stops being rushed."""

    # --- misc ---------------------------------------------------------
    currency: str = "EUR"
    debug_example_limit: int = Field(default=5, ge=0)
    """How many example rejected/pruned states to retain per iteration."""

    @model_validator(mode="after")
    def _check_stays(self) -> "PlannerConfig":
        if self.max_city_stay_days < self.min_city_stay_days:
            raise ValueError("max_city_stay_days must be >= min_city_stay_days")
        return self

    @model_validator(mode="after")
    def _check_usable_day(self) -> "PlannerConfig":
        if self.usable_day_end <= self.usable_day_start:
            raise ValueError("usable_day_end must be later than usable_day_start")
        return self

    @property
    def stay_day_options(self) -> tuple[int, ...]:
        """Candidate stay lengths generated for every visited city."""
        return tuple(range(self.min_city_stay_days, self.max_city_stay_days + 1))

    @property
    def usable_day_minutes(self) -> int:
        """Minutes in one fully usable day."""
        return usable_day_minutes(self.usable_day_start, self.usable_day_end)
