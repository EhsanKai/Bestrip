"""Recommendation profiles (V2).

There is no single right answer to "what is the best trip?" - it depends on
what the traveler is optimizing for. Rather than one universal scoring
function, the planner ships three named profiles, and every profile-specific
constant lives here instead of being scattered through the algorithms.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProfileName(str, Enum):
    """The profiles the planner can be asked for."""

    CHEAPEST = "CHEAPEST"
    BEST_VALUE = "BEST_VALUE"
    ADVENTURE = "ADVENTURE"


class TravelValueWeights(BaseModel):
    """Relative importance of the five Travel Value components."""

    model_config = ConfigDict(frozen=True)

    cost: float = Field(default=0.25, ge=0.0)
    experience: float = Field(default=0.30, ge=0.0)
    preferences: float = Field(default=0.20, ge=0.0)
    time: float = Field(default=0.15, ge=0.0)
    diversity: float = Field(default=0.10, ge=0.0)

    @model_validator(mode="after")
    def _check_nonzero(self) -> "TravelValueWeights":
        if self.total <= 0:
            raise ValueError("at least one Travel Value weight must be positive")
        return self

    @property
    def total(self) -> float:
        return self.cost + self.experience + self.preferences + self.time + self.diversity

    def as_dict(self) -> dict[str, float]:
        return {
            "cost": self.cost,
            "experience": self.experience,
            "preferences": self.preferences,
            "time": self.time,
            "diversity": self.diversity,
        }

    def normalized(self) -> dict[str, float]:
        """Weights rescaled to sum to 1.0, so the total stays in ``[0, 1]``."""
        total = self.total
        return {name: value / total for name, value in self.as_dict().items()}


class RecommendationProfile(BaseModel):
    """A named way of trading the five components off against each other."""

    model_config = ConfigDict(frozen=True)

    name: ProfileName
    description: str = ""
    weights: TravelValueWeights = Field(default_factory=TravelValueWeights)

    budget_utilization_weight: float = Field(default=0.35, ge=0.0, le=1.0)
    """How much of CostScore comes from *using* the budget rather than saving it.

    ``0.0`` makes CostScore pure efficiency (cheaper is always better), which is
    what CHEAPEST needs. Above zero, spending up to
    ``PlannerConfig.budget_utilization_target`` costs almost nothing, so a
    better trip is free to be dearer.
    """

    min_duration_utilization: float | None = Field(default=None, ge=0.0, le=1.0)
    """Optional per-profile override of the config's minimum trip length."""

    diversity_similarity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    """Optional per-profile override of how different results must be."""


#: The built-in profiles. Weights are starting points tuned against the
#: synthetic dataset; the architecture matters more than the exact numbers.
PROFILES: dict[ProfileName, RecommendationProfile] = {
    ProfileName.CHEAPEST: RecommendationProfile(
        name=ProfileName.CHEAPEST,
        description=(
            "Minimize what the trip costs, while still respecting the budget, "
            "duration, mandatory and avoided destinations."
        ),
        weights=TravelValueWeights(
            cost=0.70, experience=0.10, preferences=0.10, time=0.05, diversity=0.05
        ),
        # Cheaper must always beat dearer, so no reward for using the budget.
        budget_utilization_weight=0.0,
        # A cheap short trip is a legitimate answer under this profile.
        min_duration_utilization=0.5,
    ),
    ProfileName.BEST_VALUE: RecommendationProfile(
        name=ProfileName.BEST_VALUE,
        description=(
            "The best trip the money and time can buy: experience and preference "
            "match first, with cost as one factor among several."
        ),
        weights=TravelValueWeights(
            cost=0.25, experience=0.30, preferences=0.20, time=0.15, diversity=0.10
        ),
        budget_utilization_weight=0.35,
    ),
    ProfileName.ADVENTURE: RecommendationProfile(
        name=ProfileName.ADVENTURE,
        description=(
            "See more places: several cities and varied destinations, still "
            "inside the budget and without an absurd amount of time in transit."
        ),
        weights=TravelValueWeights(
            cost=0.20, experience=0.25, preferences=0.15, time=0.10, diversity=0.30
        ),
        budget_utilization_weight=0.40,
        # Different styles of trip matter more here, so accept closer neighbours.
        diversity_similarity_threshold=0.6,
    ),
}

#: Used whenever the caller does not ask for a specific profile.
DEFAULT_PROFILE = ProfileName.BEST_VALUE


def get_profile(name: ProfileName | str | None) -> RecommendationProfile:
    """Look up a profile, falling back to :data:`DEFAULT_PROFILE`."""
    if name is None:
        return PROFILES[DEFAULT_PROFILE]
    if isinstance(name, RecommendationProfile):  # pragma: no cover - defensive
        return name
    key = ProfileName(name) if not isinstance(name, ProfileName) else name
    return PROFILES[key]
