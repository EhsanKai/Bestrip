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

from .freshness import PriceProvenance


#: Rooms at which a rate stops feeling scarce (V4). Above this, availability
#: carries no information a traveler would act on.
SCARCITY_HORIZON_ROOMS = 10


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


class AccommodationTier(str, Enum):
    """Quality bands offered in every city."""

    BUDGET = "budget"
    STANDARD = "standard"
    COMFORT = "comfort"


class AccommodationType(str, Enum):
    """What kind of place it is."""

    HOSTEL = "hostel"
    APARTMENT = "apartment"
    HOTEL = "hotel"
    BOUTIQUE = "boutique"


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
    accommodation_type: AccommodationType = AccommodationType.HOTEL

    rating: float = Field(default=0.7, ge=0.0, le=1.0)
    """Guest rating, normalized to ``[0, 1]``. See :func:`rating_from_stars`."""
    location_score: float = Field(default=0.6, ge=0.0, le=1.0)
    """How central/convenient the location is, normalized to ``[0, 1]``."""
    free_cancellation: bool = False

    currency: str = "EUR"
    """Currency of :attr:`price_per_night`. Providers normalize before returning."""

    rooms_available: int | None = Field(default=None, ge=0)
    """Rooms left at this rate, or ``None`` when the provider does not say (V4).

    ``None`` is *unknown*, not *unlimited*: the search treats it as available
    because refusing to book anything a provider declines to count would reject
    every real inventory-less feed. ``0`` is a genuine sell-out.
    """

    provenance: "PriceProvenance | None" = None
    """Where this rate came from and when (V5.3). See TransportOption."""

    taxes_included: bool = True
    """Whether :attr:`price_per_night` already carries taxes and fees (V5.5).

    Explicit because a hotel API that quotes ex-tax and one that quotes
    inclusive look identical in a float, and comparing them is how a trip ends
    up 20% more expensive than the search said.
    """

    def has_capacity_for(self, travelers: int) -> bool:
        """Whether enough rooms remain to sleep the whole party (V4)."""
        if self.rooms_available is None:
            return True
        return self.rooms_available >= self.rooms_required(travelers)

    @property
    def scarcity(self) -> float:
        """How close this rate is to selling out, in ``[0, 1]`` (V4).

        ``0.0`` when availability is unknown or plentiful, approaching ``1.0``
        as the last rooms go. Used to price refundability: a booking you might
        not be able to replace is worth more when it can be cancelled.
        """
        if self.rooms_available is None:
            return 0.0
        return clamp01(1.0 - self.rooms_available / SCARCITY_HORIZON_ROOMS)

    @property
    def stars(self) -> float:
        """The rating expressed on the familiar 5-point scale."""
        return round(self.rating * 5.0, 2)

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
            f"({self.nights}n) {self.price_per_night:.2f}/night "
            f"rating {self.stars:.1f} location {self.location_score:.2f}"
        )


def rating_from_stars(stars: float) -> float:
    """Convert a 0-5 guest rating into the normalized internal form.

    Real APIs quote stars; the optimizer works in ``[0, 1]`` throughout, and
    this is the single place that conversion lives.
    """
    if not 0.0 <= stars <= 5.0:
        raise ValueError(f"stars must be within 0..5, got {stars}")
    return stars / 5.0
