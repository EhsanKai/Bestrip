"""User-facing request models."""

from __future__ import annotations

from datetime import date, timedelta
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..profiles import ProfileName
from .destination import ATTRIBUTES, EXPERIENCE_ATTRIBUTES
from .transport import TransportType


class TravelStyle(str, Enum):
    """How hard the traveler wants to be pushed.

    Scales the tolerance for time in transit and for city hopping; it does not
    change any hard constraint.
    """

    RELAXED = "relaxed"
    BALANCED = "balanced"
    PACKED = "packed"


class AccommodationPreference(str, Enum):
    """What the traveler wants out of a room."""

    CHEAPEST = "cheapest"
    BALANCED = "balanced"
    QUALITY = "quality"


class TravelPreferences(BaseModel):
    """Normalized user preferences.

    ``0.0`` means "irrelevant", ``1.0`` means "extremely important".
    ``multiple_cities`` is deliberately *not* one of the destination
    attributes: it controls how strongly the optimizer rewards visiting more
    than one city, not what a city is like.
    """

    model_config = ConfigDict(frozen=True)

    history: float = Field(default=0.5, ge=0.0, le=1.0)
    nature: float = Field(default=0.5, ge=0.0, le=1.0)
    nightlife: float = Field(default=0.5, ge=0.0, le=1.0)
    culture: float = Field(default=0.5, ge=0.0, le=1.0)
    food: float = Field(default=0.5, ge=0.0, le=1.0)
    multiple_cities: float = Field(default=0.5, ge=0.0, le=1.0)

    # --- V3 attributes -------------------------------------------------
    # These default to None rather than 0.5, meaning "the user did not say".
    # Unspecified attributes are left out of the match entirely, so adding
    # dimensions in V3 cannot silently change a V1-era request's scores.
    architecture: float | None = Field(default=None, ge=0.0, le=1.0)
    shopping: float | None = Field(default=None, ge=0.0, le=1.0)
    museums: float | None = Field(default=None, ge=0.0, le=1.0)
    beaches: float | None = Field(default=None, ge=0.0, le=1.0)
    family_friendly: float | None = Field(default=None, ge=0.0, le=1.0)
    romance: float | None = Field(default=None, ge=0.0, le=1.0)
    adventure: float | None = Field(default=None, ge=0.0, le=1.0)

    def attribute_weights(self) -> dict[str, float]:
        """Preference weights for the five V1 destination attributes."""
        return {name: float(getattr(self, name)) for name in ATTRIBUTES}

    def experience_weights(self) -> dict[str, float]:
        """Weights for every attribute the user actually expressed an opinion on.

        Unspecified V3 attributes are omitted, so a request that only talks
        about history and food is matched on history and food.
        """
        weights: dict[str, float] = {}
        for name in EXPERIENCE_ATTRIBUTES:
            value = getattr(self, name)
            if value is not None:
                weights[name] = float(value)
        return weights


class TripRequest(BaseModel):
    """Everything the planner needs in order to search for itineraries."""

    model_config = ConfigDict(frozen=True)

    origin: str
    budget: float = Field(gt=0.0)
    travelers: int = Field(default=1, ge=1)
    duration_days: int = Field(ge=1)

    date_from: date
    date_to: date

    date_flexible: bool = False

    transport_preferences: list[TransportType] = Field(
        default_factory=lambda: list(TransportType)
    )

    must_visit: list[str] = Field(default_factory=list)
    preferred_destinations: list[str] = Field(default_factory=list)
    avoid_destinations: list[str] = Field(default_factory=list)

    preferences: TravelPreferences = Field(default_factory=TravelPreferences)

    # --- V3 structured preferences -------------------------------------
    preferred_experiences: list[str] = Field(default_factory=list)
    """Attributes the traveler is actively looking for, e.g. ``["food", "culture"]``.

    Shorthand for setting those weights high; anything listed here is treated
    as strongly wanted regardless of the numeric ``preferences``.
    """
    disliked_experiences: list[str] = Field(default_factory=list)
    """Attributes the traveler wants to avoid. Cities strong in these are
    penalized rather than merely un-rewarded."""

    previously_visited: list[str] = Field(default_factory=list)
    """Cities the traveler has already seen. Not forbidden - just worth less,
    so novelty competes fairly against a known-good destination."""

    preferred_city_count: int | None = Field(default=None, ge=1)
    """How many cities the traveler wants. ``None`` lets the planner infer a
    sensible number from the trip length."""

    accommodation_preference: AccommodationPreference = AccommodationPreference.BALANCED
    travel_style: TravelStyle = TravelStyle.BALANCED

    profile: ProfileName | None = None
    """Which recommendation profile to optimize for.

    ``None`` defers to ``PlannerConfig.profile`` (BEST_VALUE by default), so
    requests written against V1 keep working unchanged.
    """

    currency: str = "EUR"

    @model_validator(mode="after")
    def _check_window(self) -> "TripRequest":
        if self.date_to < self.date_from:
            raise ValueError("date_to must not be earlier than date_from")
        window_days = (self.date_to - self.date_from).days + 1
        if self.duration_days > window_days:
            raise ValueError(
                f"duration_days={self.duration_days} does not fit in the "
                f"{window_days}-day window {self.date_from}..{self.date_to}"
            )
        if not self.transport_preferences:
            raise ValueError("transport_preferences must not be empty")
        overlap = {d.casefold() for d in self.must_visit} & {
            d.casefold() for d in self.avoid_destinations
        }
        if overlap:
            raise ValueError(
                f"destinations cannot be both mandatory and avoided: {sorted(overlap)}"
            )
        known = set(EXPERIENCE_ATTRIBUTES)
        for field, values in (
            ("preferred_experiences", self.preferred_experiences),
            ("disliked_experiences", self.disliked_experiences),
        ):
            unknown = [v for v in values if v not in known]
            if unknown:
                raise ValueError(
                    f"{field} contains unknown experience(s) {unknown}; "
                    f"known: {sorted(known)}"
                )
        both = set(self.preferred_experiences) & set(self.disliked_experiences)
        if both:
            raise ValueError(
                f"experiences cannot be both preferred and disliked: {sorted(both)}"
            )
        return self

    @property
    def max_trip_minutes(self) -> int:
        """Maximum elapsed wall-clock time of the whole trip, in minutes."""
        return self.duration_days * 24 * 60

    def candidate_start_dates(self) -> list[date]:
        """Departure dates the trip may start on.

        A trip of ``duration_days`` days starting on ``d`` occupies the dates
        ``d .. d + duration_days - 1``; that span must stay inside the
        requested window. When the user is not flexible, only ``date_from``
        is offered.
        """
        span = timedelta(days=self.duration_days - 1)
        if not self.date_flexible:
            candidates = [self.date_from]
        else:
            candidates = []
            day = self.date_from
            while day + span <= self.date_to:
                candidates.append(day)
                day += timedelta(days=1)
        return [d for d in candidates if d + span <= self.date_to]
