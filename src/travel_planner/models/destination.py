"""Destination metadata model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Attributes shared between :class:`Destination` and ``TravelPreferences``.
#: The scoring engine matches user weights against destination values using
#: exactly these keys, so adding an attribute only requires touching this tuple
#: plus the two models.
ATTRIBUTES: tuple[str, ...] = ("history", "nature", "nightlife", "culture", "food")


class Destination(BaseModel):
    """A city that can be visited during a trip.

    Attribute values are normalized to ``[0, 1]`` where ``1.0`` means the city
    is outstanding for that dimension.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    country: str

    history: float = Field(ge=0.0, le=1.0)
    nature: float = Field(ge=0.0, le=1.0)
    nightlife: float = Field(ge=0.0, le=1.0)
    culture: float = Field(ge=0.0, le=1.0)
    food: float = Field(ge=0.0, le=1.0)

    recommended_min_days: float = Field(default=1.0, gt=0.0)
    recommended_max_days: float = Field(default=4.0, gt=0.0)

    @model_validator(mode="after")
    def _check_days(self) -> "Destination":
        if self.recommended_max_days < self.recommended_min_days:
            raise ValueError(
                f"{self.id}: recommended_max_days must be >= recommended_min_days"
            )
        return self

    def attribute_vector(self) -> dict[str, float]:
        """The destination's profile as an ``attribute -> value`` mapping."""
        return {name: float(getattr(self, name)) for name in ATTRIBUTES}
