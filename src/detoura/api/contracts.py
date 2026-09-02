"""The product API contract (V5.9).

The engine's :class:`~detoura.models.itinerary.PlanResult` is an *engine*
result: it carries `value_breakdown` with nine weighted components, run
metadata with `states_generated` and `beam_rounds`, and a `debug` trace. All of
that is exactly right for the optimizer and exactly wrong for a client.

The rule the spec sets - *"the frontend should NOT understand Beam Search
internals"* - is a coupling rule, not a cosmetic one. If a screen reads
`metadata.beam_rounds`, then changing the search strategy becomes a frontend
release. So this module is one half of a one-way translation - the shapes a
client sees - and nothing declared here names a beam, a Pareto frontier or a
search state. `assembler.py` is the other half, and does the translating.

What survives the translation is everything a traveler could act on. What does
not is everything that only explains *how* we found it.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ..models.destination import EXPERIENCE_ATTRIBUTES
from ..models.freshness import PriceFreshness
from ..models.trip import AccommodationPreference, TransportType
from ..profiles import ProfileName
from ..search_modes import SearchMode
from ..services.confidence import ConfidenceLevel


class IntensityBand(str, Enum):
    """Travel intensity as a person would describe it.

    A band rather than the raw number because 0.071 means nothing to anyone,
    and because the bands are what the copy is written against.
    """

    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"

    @classmethod
    def of(cls, intensity: float) -> "IntensityBand":
        if intensity <= 0.12:
            return cls.LOW
        if intensity < 0.25:
            return cls.MODERATE
        return cls.HIGH


class AvailabilityStatus(str, Enum):
    """What we know about being able to book this.

    ``UNKNOWN`` is a first-class answer and never collapses into ``SOLD_OUT``:
    a provider that does not report inventory has not told us the trip is
    unavailable, and saying otherwise would invent a fact.
    """

    AVAILABLE = "AVAILABLE"
    LIMITED = "LIMITED"
    SOLD_OUT = "SOLD_OUT"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
class TripSearchRequest(BaseModel):
    """What the traveler asked for, in product terms.

    Notice what is absent: no beam width, no adaptive flag, no profile weights.
    The only lever on search effort is :attr:`search_mode`, which is an
    intent ("look harder") rather than a configuration.
    """

    model_config = ConfigDict(frozen=True)

    origin: str = Field(min_length=1, description="City or airport to start from.")
    date_from: str
    date_to: str
    duration_days: int = Field(ge=1, le=30)
    date_flexible: bool = False

    travelers: int = Field(default=2, ge=1, le=12)
    budget: float = Field(gt=0.0, description="Total for the whole party.")

    profile: ProfileName = ProfileName.BEST_VALUE
    search_mode: SearchMode = SearchMode.SMART

    interests: list[str] = Field(default_factory=list)
    disliked: list[str] = Field(default_factory=list)
    preferred_destinations: list[str] = Field(default_factory=list)
    avoided_destinations: list[str] = Field(default_factory=list)
    previously_visited: list[str] = Field(default_factory=list)

    accommodation_preference: AccommodationPreference = AccommodationPreference.BALANCED
    preferred_city_count: int | None = Field(default=None, ge=1, le=6)
    transport: list[TransportType] = Field(
        default_factory=lambda: [TransportType.FLIGHT, TransportType.TRAIN]
    )

    def validated_interests(self) -> list[str]:
        """Drop anything the catalog has never heard of.

        The UI sends a fixed chip set, so an unknown name here means a stale
        client or a hand-rolled request. Dropping rather than rejecting keeps
        an old client working instead of failing its whole search over one
        obsolete tag; the domain model still rejects unknown names when they
        matter.
        """
        known = set(EXPERIENCE_ATTRIBUTES)
        return [name for name in self.interests if name in known]

    def validated_dislikes(self) -> list[str]:
        known = set(EXPERIENCE_ATTRIBUTES)
        chosen = set(self.validated_interests())
        # An interest that is also a dislike is a client bug; the interest wins,
        # because it is the more specific statement.
        return [
            name for name in self.disliked if name in known and name not in chosen
        ]


# ---------------------------------------------------------------------------
# Response pieces
# ---------------------------------------------------------------------------
class CostBreakdownDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    transport: float
    accommodation: float
    ground_transfer: float
    total: float


class LegDTO(BaseModel):
    """One journey, for the timeline."""

    # ``from`` is a Python keyword, so the field is named ``origin`` and
    # aliased: the wire format is what the frontend reads, and "from"/"to" is
    # what a leg is called everywhere else in the product.
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    origin: str = Field(alias="from")
    destination: str = Field(alias="to")
    departure: datetime
    arrival: datetime
    minutes: int
    mode: str
    operator: str
    price_per_person: float
    seats_available: int | None = None


class StayDTO(BaseModel):
    """One stop, for the timeline and the accommodation panel."""

    model_config = ConfigDict(frozen=True)

    city: str
    arrival: datetime
    departure: datetime
    nights: int
    cost: float
    name: str | None = None
    tier: str | None = None
    type: str | None = None
    rating: float | None = None
    location_score: float | None = None
    free_cancellation: bool = False
    usable_minutes: int = 0
    rooms_available: int | None = None
    cheapest_alternative_cost: float | None = None
    premium: float = 0.0
    value_note: str | None = None
    """Prose about the room trade-off, only where the data supports it (V5.5.1).

    ``None`` means say nothing. A note is worse than silence when it is
    manufactured from a premium of zero.
    """


class DestinationMatchDTO(BaseModel):
    """Why this city, in the traveler's own terms (V5.7)."""

    model_config = ConfigDict(frozen=True)

    city: str
    match: float
    quality: float
    stay_quality: float
    usable_days: float
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    disliked_present: list[str] = Field(default_factory=list)
    previously_visited: bool = False
    note: str = ""


class BaselineComparisonDTO(BaseModel):
    """The traveler's own idea, and what else the budget buys.

    The signature Detoura component. It exists to make one point precisely:
    we are not saying the original idea was wrong, we are showing the
    alternative. So it reports the deltas in both directions and lets the UI
    decline to editorialise.
    """

    model_config = ConfigDict(frozen=True)

    destination: str
    total_price: float
    nights: int
    usable_hours: float
    price_delta: float
    """Positive means our suggestion costs more."""
    extra_cities: int
    extra_usable_hours: float
    extra_travel_minutes: int


class ConfidenceReasonDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    positive: bool


class ConfidenceDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: ConfidenceLevel
    label: str
    reasons: list[ConfidenceReasonDTO] = Field(default_factory=list)


class TripRecommendation(BaseModel):
    """One trip, as the product describes it.

    Everything here is either something the traveler can act on or something
    that explains the trip. Nothing here explains the *search*.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    rank: int
    route: str
    route_nodes: list[str]
    cities: list[str]
    origin_airport: str
    return_airport: str

    departure: datetime
    arrival: datetime
    duration_days: float
    nights: list[int]

    total_price: float
    price_per_person: float
    currency: str
    costs: CostBreakdownDTO

    usable_hours: float
    travel_hours: float
    transfer_minutes: int

    travel_intensity: float
    intensity_band: IntensityBand
    experience_score: float
    preference_match: float
    accommodation_score: float
    travel_value: float
    profile: ProfileName

    confidence: ConfidenceDTO
    price_freshness: PriceFreshness
    availability: AvailabilityStatus

    baseline_comparison: BaselineComparisonDTO | None = None
    highlights: list[str] = Field(default_factory=list)
    """Typed explanation factors, already turned into readable phrases."""
    tradeoff: str | None = None
    why_we_like_it: str | None = None

    stays: list[StayDTO] = Field(default_factory=list)
    legs: list[LegDTO] = Field(default_factory=list)
    destination_matches: list[DestinationMatchDTO] = Field(default_factory=list)


class SearchDiagnostics(BaseModel):
    """How the search went, in terms a product can show.

    This is the honest translation of the engine's run metadata: how much was
    considered, how long it took, whether there is more to find. It carries no
    beam, no frontier and no state count, because none of those mean anything
    to the person reading them.
    """

    model_config = ConfigDict(frozen=True)

    mode: SearchMode
    elapsed_seconds: float
    itineraries_considered: int
    alternatives_evaluated: int
    destinations_explored: int
    rounds: int = 1
    deeper_search_available: bool = False
    notes: list[str] = Field(default_factory=list)


class ProviderIssueDTO(BaseModel):
    """Something that went wrong upstream (V5.1.1).

    Present and non-empty means the search ran on incomplete data. It does
    **not** mean nothing matched - that is :class:`NoResultsGuidance`, and
    conflating the two is the exact failure this contract exists to prevent.
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    provider: str
    message: str
    retryable: bool
    occurrences: int = 1


class RelaxationSuggestion(BaseModel):
    """A specific, applicable way to loosen a search that found nothing."""

    model_config = ConfigDict(frozen=True)

    label: str
    description: str
    patch: dict = Field(default_factory=dict)
    """A partial request the client can merge and re-run. Actionable, not advice."""


class NoResultsGuidance(BaseModel):
    """Why nothing matched, and what would help (V5, Part 24)."""

    model_config = ConfigDict(frozen=True)

    reason: str
    closest_price: float | None = None
    requested_budget: float
    suggestions: list[RelaxationSuggestion] = Field(default_factory=list)


class TripSearchResponse(BaseModel):
    """The product's answer to one search."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    origin: str
    origin_airports: list[str]
    currency: str
    profile: ProfileName
    recommendations: list[TripRecommendation] = Field(default_factory=list)
    baseline: BaselineComparisonDTO | None = None
    diagnostics: SearchDiagnostics
    issues: list[ProviderIssueDTO] = Field(default_factory=list)
    no_results: NoResultsGuidance | None = None
