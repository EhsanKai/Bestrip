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


#: The Travel Value component names, in report order.
#:
#: The first five keep their V2 names for backward compatibility; the spec calls
#: them BudgetEfficiency, DestinationExperience, PreferenceMatch,
#: TimeUtilization and Diversity respectively. The last four are new in V3.
COMPONENTS: tuple[str, ...] = (
    "cost",           # BudgetEfficiency
    "experience",     # DestinationExperience
    "preferences",    # PreferenceMatch
    "time",           # TimeUtilization
    "diversity",      # Diversity
    "city_count",     # CityCountFit          (V3)
    "accommodation",  # AccommodationQuality  (V3)
    "convenience",    # Convenience           (V3)
    "intensity",      # TravelIntensity       (V3)
)


class TravelValueWeights(BaseModel):
    """Relative importance of the nine Travel Value components."""

    model_config = ConfigDict(frozen=True)

    cost: float = Field(default=0.20, ge=0.0)
    experience: float = Field(default=0.20, ge=0.0)
    preferences: float = Field(default=0.15, ge=0.0)
    time: float = Field(default=0.12, ge=0.0)
    diversity: float = Field(default=0.08, ge=0.0)
    city_count: float = Field(default=0.08, ge=0.0)
    accommodation: float = Field(default=0.07, ge=0.0)
    convenience: float = Field(default=0.05, ge=0.0)
    intensity: float = Field(default=0.05, ge=0.0)

    @model_validator(mode="after")
    def _check_nonzero(self) -> "TravelValueWeights":
        if self.total <= 0:
            raise ValueError("at least one Travel Value weight must be positive")
        return self

    @property
    def total(self) -> float:
        return sum(self.as_dict().values())

    def as_dict(self) -> dict[str, float]:
        return {name: float(getattr(self, name)) for name in COMPONENTS}

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

    city_count_bias: int = Field(default=0, ge=-2, le=2)
    """Shifts the ideal city count this profile aims for.

    ADVENTURE aims one city higher than the trip length alone would suggest;
    CHEAPEST one lower, because every extra city is another hotel and another
    ticket.
    """


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
            cost=0.62, experience=0.08, preferences=0.08, time=0.04, diversity=0.03,
            city_count=0.03, accommodation=0.04, convenience=0.04, intensity=0.04,
        ),
        # Cheaper must always beat dearer, so no reward for using the budget.
        budget_utilization_weight=0.0,
        # A cheap short trip is a legitimate answer under this profile.
        min_duration_utilization=0.5,
        city_count_bias=-1,
    ),
    ProfileName.BEST_VALUE: RecommendationProfile(
        name=ProfileName.BEST_VALUE,
        description=(
            "The best trip the money and time can buy: experience and preference "
            "match first, with cost as one factor among several."
        ),
        weights=TravelValueWeights(
            cost=0.19, experience=0.21, preferences=0.15, time=0.11, diversity=0.06,
            city_count=0.08, accommodation=0.07, convenience=0.05, intensity=0.08,
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
            cost=0.13, experience=0.19, preferences=0.11, time=0.07, diversity=0.19,
            city_count=0.12, accommodation=0.03, convenience=0.03, intensity=0.13,
        ),
        budget_utilization_weight=0.40,
        # Different styles of trip matter more here, so accept closer neighbours.
        diversity_similarity_threshold=0.6,
        city_count_bias=1,
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
