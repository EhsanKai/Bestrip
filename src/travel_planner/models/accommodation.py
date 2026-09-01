"""Accommodation domain model (V2).

V1 assumed a city stay was free, which made long stays and expensive cities
look artificially attractive. Accommodation is now part of the trip economics.

**Room model.** A price is quoted *per room per night*, and a room sleeps
``capacity`` people. The number of rooms is ``ceil(travelers / capacity)``, so
four travelers in double rooms pay for two rooms. Rooms are assumed to be
freely available - the synthetic provider never sells out.
"""

from __future__ import annotations

import math
from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AccommodationTier(str, Enum):
    """Quality bands offered in every city."""

    BUDGET = "budget"
    STANDARD = "standard"
    COMFORT = "comfort"


class AccommodationOption(BaseModel):
    """A bookable stay in one city for a fixed date range."""

    model_config = ConfigDict(frozen=True)

    id: str
    city: str
    name: str = ""

    check_in: date
    check_out: date

    price_per_night: float = Field(ge=0.0)
    """Price of **one room** for one night, for the whole party in that room."""

    capacity: int = Field(default=2, ge=1)
    """How many people one room sleeps."""

    tier: AccommodationTier = AccommodationTier.STANDARD
    rating: float = Field(default=0.7, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_dates(self) -> "AccommodationOption":
        if self.check_out <= self.check_in:
            raise ValueError(
                f"{self.id}: check_out {self.check_out} must be after "
                f"check_in {self.check_in}"
            )
        return self

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    def rooms_required(self, travelers: int) -> int:
        """Rooms needed to sleep ``travelers`` at this option's capacity."""
        if travelers < 1:
            raise ValueError("travelers must be >= 1")
        return math.ceil(travelers / self.capacity)

    def total_price(self, travelers: int) -> float:
        """Full accommodation cost of this stay for the whole party."""
        return round(
            self.price_per_night * self.nights * self.rooms_required(travelers), 2
        )

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return (
            f"{self.city} {self.tier.value} {self.check_in}..{self.check_out} "
            f"({self.nights}n) {self.price_per_night:.2f}/night"
        )
