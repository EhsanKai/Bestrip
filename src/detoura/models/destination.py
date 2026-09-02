"""Destination metadata model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The five original attributes, matched by the V1
#: :class:`~detoura.algorithms.scoring.ScoringEngine`.
#:
#: Kept as its own tuple so the legacy engine's arithmetic is unchanged by V3's
#: richer model - its tests assert exact averages over exactly these five.
ATTRIBUTES: tuple[str, ...] = ("history", "nature", "nightlife", "culture", "food")

#: The attributes V3 added on top.
V3_ATTRIBUTES: tuple[str, ...] = (
    "architecture",
    "shopping",
    "museums",
    "beaches",
    "family_friendly",
    "romance",
    "adventure",
)

#: Everything the V3 experience model reasons about.
EXPERIENCE_ATTRIBUTES: tuple[str, ...] = ATTRIBUTES + V3_ATTRIBUTES


class Destination(BaseModel):
    """A city that can be visited during a trip.

    Attribute values are normalized to ``[0, 1]`` where ``1.0`` means the city
    is outstanding for that dimension. The V3 attributes default to ``0.5`` so a
    catalog written against V1 still constructs, and simply reads as "average"
    on the new dimensions.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    country: str

    # --- V1 attributes -------------------------------------------------
    history: float = Field(ge=0.0, le=1.0)
    nature: float = Field(ge=0.0, le=1.0)
    nightlife: float = Field(ge=0.0, le=1.0)
    culture: float = Field(ge=0.0, le=1.0)
    food: float = Field(ge=0.0, le=1.0)

    # --- V3 attributes -------------------------------------------------
    architecture: float = Field(default=0.5, ge=0.0, le=1.0)
    shopping: float = Field(default=0.5, ge=0.0, le=1.0)
    museums: float = Field(default=0.5, ge=0.0, le=1.0)
    beaches: float = Field(default=0.0, ge=0.0, le=1.0)
    family_friendly: float = Field(default=0.5, ge=0.0, le=1.0)
    romance: float = Field(default=0.5, ge=0.0, le=1.0)
    adventure: float = Field(default=0.5, ge=0.0, le=1.0)

    recommended_min_days: float = Field(default=1.0, gt=0.0)
    recommended_max_days: float = Field(default=4.0, gt=0.0)

    experience_richness: float | None = Field(default=None, ge=0.0, le=1.0)
    """How much there is to do here overall.

    ``None`` means "derive it from the attributes", which is almost always what
    you want; set it explicitly only to override a city whose headline appeal is
    not the average of its parts.
    """

    @model_validator(mode="after")
    def _check_days(self) -> "Destination":
        if self.recommended_max_days < self.recommended_min_days:
            raise ValueError(
                f"{self.id}: recommended_max_days must be >= recommended_min_days"
            )
        return self

    def attribute_vector(self) -> dict[str, float]:
        """The five V1 attributes, for the legacy scoring engine."""
        return {name: float(getattr(self, name)) for name in ATTRIBUTES}

    def experience_vector(self) -> dict[str, float]:
        """The full V3 profile as an ``attribute -> value`` mapping."""
        return {name: float(getattr(self, name)) for name in EXPERIENCE_ATTRIBUTES}

    @property
    def richness(self) -> float:
        """Overall depth of things to do, in ``[0, 1]``.

        Derived as the mean of the attribute profile unless the catalog set
        ``experience_richness`` explicitly. Beaches are excluded from the mean:
        a landlocked city should not read as "poor" for lacking a coastline it
        was never going to have.
        """
        if self.experience_richness is not None:
            return self.experience_richness
        values = [
            getattr(self, name) for name in EXPERIENCE_ATTRIBUTES if name != "beaches"
        ]
        return sum(values) / len(values)

    def recommended_days(self) -> tuple[float, float]:
        return (self.recommended_min_days, self.recommended_max_days)
