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

    # --- V9 Phase 2: geography + catalog governance ---------------------
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    """ISO 3166-1 alpha-2, e.g. ``"FR"``. ``None`` for the legacy core catalog
    entries written before this field existed."""
    region: str = "Europe"
    subregion: str | None = None
    """A broad grouping used only for the diversity adjustment — e.g.
    ``"Iberia"``, ``"Balkans"``, ``"Nordics"``, ``"Central Europe"``."""
    timezone: str | None = None
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    secondary_airports: tuple[str, ...] = ()
    """Retained for reference. Acquisition never fans out to these — the
    primary airport is the single deterministic query node (V9 §18)."""
    tags: tuple[str, ...] = ()
    """Broad, truthful descriptors ("history", "beach", "nightlife",
    "budget", "capital", "island", ...). The catalog derives the 12
    experience attributes from these for entries that do not set them
    explicitly, so we never fabricate fine-grained personality scores
    (V9 §17)."""
    enabled: bool = True
    """A disabled destination is not offered anywhere."""
    acquisition_eligible: bool = True
    """Whether Detoura may spend a live provider request discovering this
    market. Requires a usable ``primary_airport``."""
    metadata_source: str = "synthetic"
    """Where this row's data came from — ``"synthetic"``, ``"curated"``, an
    import source id. Never presented as an authoritative external dataset."""

    primary_airport: str | None = Field(default=None, min_length=3, max_length=3)
    """The IATA code a real transport provider is queried with for this city (V8).

    Synthetic like the rest of the catalog - a real deployment would carry a
    proper city-to-airports table with alternates - but the codes themselves
    are the genuine primary airport for each city, because a made-up code would
    just make Duffel return 422. ``None`` means "not wired for a real provider":
    beam search over the synthetic graph keys on the city ``id`` and never needs
    this, so a catalog entry without one still works everywhere except real
    acquisition.
    """

    # --- V1 attributes -------------------------------------------------
    # Default to "average" so a ~200-city catalog built from broad tags
    # constructs without fabricating precise personality scores; the core
    # 16 cities still set every value explicitly (V9 Phase 2 §17).
    history: float = Field(default=0.5, ge=0.0, le=1.0)
    nature: float = Field(default=0.5, ge=0.0, le=1.0)
    nightlife: float = Field(default=0.5, ge=0.0, le=1.0)
    culture: float = Field(default=0.5, ge=0.0, le=1.0)
    food: float = Field(default=0.5, ge=0.0, le=1.0)

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

    @model_validator(mode="after")
    def _acq_needs_airport(self) -> "Destination":
        if self.acquisition_eligible and self.enabled and not self.primary_airport:
            raise ValueError(
                f"{self.id}: acquisition_eligible requires a primary_airport"
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
