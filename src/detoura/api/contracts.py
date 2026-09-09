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

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models.baggage import (
    BaggageKind,
    BaggageRequirement,
    BaggageStatus,
    PriceCompleteness,
)
from ..models.booking import BookingState
from ..models.commercial import ServiceTier
from ..models.destination import EXPERIENCE_ATTRIBUTES
from ..models.freshness import PriceFreshness
from ..models.revalidation import OfferRevalidationStatus, RevalidationStatus
from ..models.travel_pass import PassMode, PassStatus
from ..services.booking_orchestrator import BookingPhase
from ..models.trip import AccommodationPreference, TransportType
from ..profiles import ProfileName
from ..search_modes import SearchMode
from ..services.confidence import ConfidenceLevel
from ..services.feedback import FeedbackAction
from ..services.recheck import ComponentState, RecheckStatus
from ..models.patch import TripPatch
from ..services.trip_comparison import ComparisonVerdict, Favours


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
# ---------------------------------------------------------------------------
# Request bounds (V6.5)
# ---------------------------------------------------------------------------
# Every limit below is deliberately far above any real traveller's request and
# far below what makes one request expensive. They exist because three of them
# were demonstrated, against a running server, to turn a single well-formed
# POST into tens of seconds of CPU or tens of thousands of uncached upstream
# calls. Bounding is the whole fix: none of these caps changes the result of a
# request a person would actually make.

#: The catalog defines ~12 interest attributes and 16 destinations. These caps
#: leave room for the catalog to grow several times over.
MAX_INTERESTS = 32
MAX_DESTINATION_NAMES = 64
MAX_TRANSPORT_MODES = 8
MAX_ORIGIN_LENGTH = 120

#: A trip is capped at 6 cities, so a real itinerary has well under a dozen
#: legs. These are per-request structural caps, not product limits.
MAX_RECHECK_LEGS = 64
MAX_RECHECK_STAYS = 32
MAX_RECHECK_TRANSFERS = 8

#: The search window a flexible request may span.
#:
#: Measured, not guessed: search cost plateaus with window size (a 180-day
#: window costs about the same as a 30-day one, because the beam is bounded by
#: mode, not by window). The cost that does not plateau is enumerating one
#: candidate start date per day - a 200-year window built ~73,000 of them and
#: took 75 seconds. A year is longer than anyone plans a city break and keeps
#: that enumeration trivially small.
MAX_SEARCH_WINDOW_DAYS = 366

class TripSearchRequest(BaseModel):
    """What the traveler asked for, in product terms.

    Notice what is absent: no beam width, no adaptive flag, no profile weights.
    The only lever on search effort is :attr:`search_mode`, which is an
    intent ("look harder") rather than a configuration.
    """

    model_config = ConfigDict(frozen=True)

    origin: str = Field(
        min_length=1, max_length=MAX_ORIGIN_LENGTH,
        description="City or airport to start from.",
    )
    date_from: str
    date_to: str
    duration_days: int = Field(ge=1, le=30)
    date_flexible: bool = False

    travelers: int = Field(default=2, ge=1, le=12)
    budget: float = Field(gt=0.0, description="Total for the whole party.")

    profile: ProfileName = ProfileName.BEST_VALUE
    search_mode: SearchMode = SearchMode.SMART

    interests: list[str] = Field(default_factory=list, max_length=MAX_INTERESTS)
    disliked: list[str] = Field(default_factory=list, max_length=MAX_INTERESTS)
    preferred_destinations: list[str] = Field(
        default_factory=list, max_length=MAX_DESTINATION_NAMES
    )
    avoided_destinations: list[str] = Field(
        default_factory=list, max_length=MAX_DESTINATION_NAMES
    )
    previously_visited: list[str] = Field(
        default_factory=list, max_length=MAX_DESTINATION_NAMES
    )

    accommodation_preference: AccommodationPreference = AccommodationPreference.BALANCED
    baggage: BaggageRequirement = BaggageRequirement.NONE
    """What the traveller needs to bring (V7 Phase 3).

    Omitted means "they did not say", which leaves results identical to every
    earlier release. Naming a bag does not change which trips are found - it
    changes what Detoura is willing to claim their price covers.
    """
    preferred_city_count: int | None = Field(default=None, ge=1, le=6)
    transport: list[TransportType] = Field(
        default_factory=lambda: [TransportType.FLIGHT, TransportType.TRAIN],
        max_length=MAX_TRANSPORT_MODES,
    )

    @model_validator(mode="after")
    def _bounded_window(self) -> "TripSearchRequest":
        """Reject a search window wider than a year.

        Rejected here at the contract rather than clamped deeper in: silently
        narrowing someone's window would answer a question they did not ask.
        A 422 tells them what to change.

        Dates that do not parse are left alone - the domain model already
        reports those, and duplicating the message would mean maintaining two
        of them.
        """
        from datetime import date as _date

        try:
            start = _date.fromisoformat(self.date_from)
            end = _date.fromisoformat(self.date_to)
        except ValueError:
            return self
        span = (end - start).days + 1
        if span > MAX_SEARCH_WINDOW_DAYS:
            raise ValueError(
                f"search window of {span} days exceeds the maximum of "
                f"{MAX_SEARCH_WINDOW_DAYS}; narrow date_from..date_to"
            )
        return self

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
    """Transport + accommodation + ground transfer. Equals ``total_price``.

    Deliberately **excludes** ``baggage``: the three named components above sum
    to exactly this figure, and it is the number the optimizer ranked on. A
    breakdown whose parts do not add up to its own total is worse than none.
    """
    baggage: float = 0.0
    """Baggage fees actually quoted, reported outside :attr:`total` (V7).

    Zero means "nothing quoted" *or* "nothing requested" - see
    ``TripRecommendation.baggage.completeness`` before showing it as a price.
    """


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
    """The bare fare. Baggage is never folded in here (V7 Phase 3)."""
    seats_available: int | None = None
    baggage: list[BaggageAllowanceDTO] = Field(default_factory=list)
    """What this fare says about each kind of bag. Empty when it said nothing."""


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


class BaggageAllowanceDTO(BaseModel):
    """What one fare says about one kind of bag (V7 Phase 3)."""

    model_config = ConfigDict(frozen=True)

    kind: BaggageKind
    status: BaggageStatus
    price_per_person: float | None = None
    """``null`` means *not quoted*. It never means free.

    A client that renders this as 0 has reintroduced the exact bug this phase
    exists to remove; ``status`` is the field to branch on.
    """


class BaggageDTO(BaseModel):
    """What the requested baggage costs across the whole trip.

    ``known_total`` and ``total_for_display`` are deliberately different
    fields. The first is a real sum of real quotes and may be shown as such;
    the second is ``null`` whenever anything is unquoted, so a partial figure
    cannot be rendered as a final price by a client that simply reached for the
    total.
    """

    model_config = ConfigDict(frozen=True)

    requirement: BaggageRequirement
    known_total: float
    currency: str
    completeness: PriceCompleteness
    unknown_legs: int
    unavailable_legs: int
    satisfiable: bool
    total_for_display: float | None
    """``null`` when any required bag is unpriced. Not a total in that case."""
    note: str
    """One line about the bags, assembled from the numbers above."""


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
    """Difference in **intercity** transit only - ground transfers excluded.

    Documented rather than renamed: the shipped frontend reads this field, so
    changing its name or its meaning would be a breaking change for a naming
    improvement. Note that it is deliberately *not* the same quantity as
    :attr:`TripComparisonDTO.transit_time_delta`, which is door-to-door and
    therefore usually larger. Two fields that look like they measure the same
    thing and do not is how V7 shipped its first comparison defect, so the
    difference is stated here rather than left for a reader to discover.
    """


class ComparisonMetricDTO(BaseModel):
    """One axis of the your-idea-versus-ours comparison (V7)."""

    model_config = ConfigDict(frozen=True)

    metric: str
    label: str
    original: float
    detoura: float
    delta: float
    """Always ``detoura - original``, whichever direction is better."""
    favours: Favours
    material: bool


class TripComparisonDTO(BaseModel):
    """The traveler's own idea against this recommendation (V7).

    Distinct from :class:`BaselineComparisonDTO`, which reports three raw
    deltas. This one carries the full per-metric breakdown, a verdict that can
    come out ``ORIGINAL_BETTER``, and an explicit list of what it could not
    establish. The UI is expected to render ``tradeoffs`` whenever it renders
    ``advantages``; showing only one side would misrepresent a MIXED verdict as
    a recommendation.
    """

    model_config = ConfigDict(frozen=True)

    original_destination: str
    original_price: float
    detoura_price: float
    currency: str

    price_delta: float
    city_count_delta: int
    usable_time_delta: float
    transit_time_delta: float
    experience_delta: float
    preference_match_delta: float
    accommodation_delta: float
    baggage_delta: float | None = None
    """``None`` means unknown, never "the same". No baggage model exists yet."""

    verdict: ComparisonVerdict
    metrics: list[ComparisonMetricDTO] = Field(default_factory=list)
    advantages: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    added_cities: list[str] = Field(default_factory=list)
    dropped_original_destination: bool = False
    summary: str = ""
    unknowns: list[str] = Field(default_factory=list)


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
    """The fare-based total: transport, rooms and transfers. Excludes baggage.

    Equals ``costs.total``. The all-in figure, when one honestly exists, is
    :attr:`total_with_known_baggage`.
    """
    total_with_known_baggage: float | None = None
    """Fare plus every quoted baggage fee, or ``null`` when no honest total
    exists (V7 Phase 3).

    ``null`` whenever a required bag is unpriced or unbuyable on any leg -
    which is a different statement from "baggage is free", and the reason this
    is a separate nullable field rather than a silently larger
    :attr:`total_price`.
    """
    price_per_person: float
    currency: str
    costs: CostBreakdownDTO

    usable_hours: float
    travel_hours: float
    transfer_minutes: int
    intercity_minutes: int = 0
    """Time on intercity legs only, in whole minutes (V7).

    Named for what it measures rather than "travel", because ``travel_hours``
    above is the *door-to-door* figure - transfers included - and two fields
    called travel-something meaning different things is precisely the confusion
    that let Phase 1 compare a gate-to-gate journey against a door-to-door one.
    ``intercity_minutes + transfer_minutes == total_transit_minutes``.
    """
    total_transit_minutes: int = 0
    """Door-to-door transit in whole minutes: the exact form of ``travel_hours``.

    Exposed because ``travel_hours`` is rounded to a tenth for display, so a
    client sending a trip back to be edited could only return an approximation
    - and the change diff would then report a transit difference of half an
    hour that nobody had caused.
    """
    usable_minutes: int = 0
    """Usable destination time in whole minutes, for the same reason."""

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

    baggage: BaggageDTO | None = None
    """``null`` when no baggage requirement was given (V7 Phase 3)."""
    baseline_comparison: BaselineComparisonDTO | None = None
    comparison: TripComparisonDTO | None = None
    """Your idea versus this one, in full (V7).

    Additive: ``baseline_comparison`` is unchanged and still populated, because
    the shipped frontend reads it.
    """
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


# ---------------------------------------------------------------------------
# Re-checking a saved trip (V6.1)
# ---------------------------------------------------------------------------
class RecheckLeg(BaseModel):
    """One saved leg, in the terms needed to find it again.

    Narrower than :class:`LegDTO` on purpose. The client sends back what
    identifies the leg and what it paid, not the whole rendered object, so the
    request does not break every time the display shape gains a field.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    origin: str = Field(alias="from")
    destination: str = Field(alias="to")
    departure: datetime
    operator: str = ""
    price_per_person: float = Field(ge=0.0)


class RecheckStay(BaseModel):
    """One saved stay, in the terms needed to find it again."""

    model_config = ConfigDict(frozen=True)

    city: str
    arrival: datetime
    departure: datetime
    cost: float = Field(ge=0.0)
    name: str | None = None


class RecheckTransfer(BaseModel):
    """The trip's ground transfers, at the price that was saved.

    Sent so the totals reconcile, not so they can be re-quoted: a trip's
    transfers are reported as one figure, which is not enough to name the
    individual options. Omitting it would be the dangerous choice - the
    re-checked total would come out low by exactly this amount and an unchanged
    trip would be announced as a saving.
    """

    model_config = ConfigDict(frozen=True)

    cost: float = Field(ge=0.0)
    label: str = "Airport transfers"


class TripRecheckRequest(BaseModel):
    """Re-price a trip the traveler saved.

    The trip travels with the request because Detoura stores nothing: saved
    trips live in the browser. That keeps this endpoint free of accounts, a
    database and a session model, none of which the product has decided on yet.
    """

    model_config = ConfigDict(frozen=True)

    trip_id: str
    travelers: int = Field(ge=1, le=12)
    saved_price: float = Field(gt=0.0)
    saved_at: datetime | None = None
    legs: list[RecheckLeg] = Field(min_length=1, max_length=MAX_RECHECK_LEGS)
    stays: list[RecheckStay] = Field(
        default_factory=list, max_length=MAX_RECHECK_STAYS
    )
    transfers: list[RecheckTransfer] = Field(
        default_factory=list, max_length=MAX_RECHECK_TRANSFERS
    )


class RecheckComponentDTO(BaseModel):
    """What became of one leg or one stay."""

    model_config = ConfigDict(frozen=True)

    label: str
    state: ComponentState
    saved_price: float
    current_price: float | None = None
    change: float | None = None
    detail: str = ""


class TripRecheckResponse(BaseModel):
    """The answer to "is this trip still there, at that price?".

    ``status`` is the field to render on. In particular ``UNVERIFIABLE`` must
    not be drawn as bad news about the trip - it is the absence of news, and
    the copy in :data:`RECHECK_MESSAGES` says so.
    """

    model_config = ConfigDict(frozen=True)

    trip_id: str
    status: RecheckStatus
    message: str
    checked_at: datetime

    saved_price: float
    current_price: float | None = None
    price_change: float | None = None
    price_change_pct: float | None = None

    price_freshness: PriceFreshness
    legs: list[RecheckComponentDTO] = Field(default_factory=list)
    stays: list[RecheckComponentDTO] = Field(default_factory=list)
    transfers: list[RecheckComponentDTO] = Field(default_factory=list)
    issues: list[ProviderIssueDTO] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Real-time personalization from explicit feedback (V6)
# ---------------------------------------------------------------------------
class TripValueBreakdownDTO(BaseModel):
    """The nine Travel Value components for one trip.

    Field-for-field the same shape as
    :class:`~detoura.profiles.TravelValueWeights` and the ``value_breakdown``
    :mod:`detoura.learning`'s ``Observation`` is built from - what kind of
    trip this was, not a rating of it. All nine are required: a heuristic that
    nudges a weight vector needs to know where every component of the trip
    stood, not just the ones the client happened to keep around.
    """

    model_config = ConfigDict(frozen=True)

    cost: float = Field(ge=0.0)
    experience: float = Field(ge=0.0)
    preferences: float = Field(ge=0.0)
    time: float = Field(ge=0.0)
    diversity: float = Field(ge=0.0)
    city_count: float = Field(ge=0.0)
    accommodation: float = Field(ge=0.0)
    convenience: float = Field(ge=0.0)
    intensity: float = Field(ge=0.0)

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class TripFeedbackRequest(BaseModel):
    """One explicit signal about one trip.

    Stateless like :class:`TripRecheckRequest`: there is no trip database, so
    the client sends back the components of the trip it is reacting to rather
    than an id this endpoint would have to look up. ``session_id`` is
    client-generated - there is no account system in this repo - and may be
    omitted; an unknown or empty value still gets a session rather than an
    error, because a personalization endpoint that fails a first-time visitor
    would defeat its own purpose.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str = ""
    action: FeedbackAction
    declared_profile: ProfileName | None = None
    """Set only when the traveler's search explicitly named a profile.

    Omitted (the default) leaves an existing session's declared profile
    untouched; a value here is the *only* way ``declared`` ever changes -
    never as a side effect of ``action``.
    """
    value_breakdown: TripValueBreakdownDTO


class SessionProfileDTO(BaseModel):
    """The nine weights of one profile, as the client renders them."""

    model_config = ConfigDict(frozen=True)

    cost: float
    experience: float
    preferences: float
    time: float
    diversity: float
    city_count: float
    accommodation: float
    convenience: float
    intensity: float


class TripFeedbackResponse(BaseModel):
    """The current personalization state for one session, after one signal.

    ``declared`` and ``observed`` are reported separately and are never
    merged into one number here - see :mod:`detoura.services.feedback` for
    why, and :func:`detoura.services.feedback.blend` for the one place a
    caller that genuinely needs a single blended profile should go.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    trip_id: str
    declared_profile: ProfileName
    declared: SessionProfileDTO
    observed: SessionProfileDTO
    confidence: float
    signal_count: int
    explanation: str


# ---------------------------------------------------------------------------
# Editing a selected trip (V7 Phase 2)
# ---------------------------------------------------------------------------
class SelectedTripDTO(BaseModel):
    """The trip being edited, sent back by the client.

    Stateless like :class:`TripRecheckRequest`, and for the same reason:
    Detoura stores nothing, so the trip travels with the request. It carries
    what preservation is measured against - cities, airports, legs, stays and
    the three scores - rather than the whole rendered object, so the request
    does not break every time the display shape gains a field.
    """

    model_config = ConfigDict(frozen=True)

    cities: list[str] = Field(min_length=1, max_length=MAX_DESTINATION_NAMES)
    origin_airport: str = Field(min_length=1, max_length=16)
    return_airport: str = Field(min_length=1, max_length=16)
    departure: datetime
    arrival: datetime
    duration_days: float = Field(ge=0.0)
    total_price: float = Field(ge=0.0)

    intercity_minutes: int = Field(default=0, ge=0)
    """Legs only. With ``transfer_minutes`` this reconstructs door-to-door
    transit exactly, which is what the change diff compares."""
    transfer_minutes: int = Field(default=0, ge=0)
    usable_minutes: int = Field(default=0, ge=0)

    experience_score: float = Field(default=0.0, ge=0.0, le=1.0)
    preference_match: float = Field(default=0.0, ge=0.0, le=1.0)
    accommodation_score: float = Field(default=0.0, ge=0.0, le=1.0)

    legs: list[RecheckLeg] = Field(default_factory=list, max_length=MAX_RECHECK_LEGS)
    stays: list[RecheckStay] = Field(
        default_factory=list, max_length=MAX_RECHECK_STAYS
    )


class TripReoptimizeRequest(BaseModel):
    """Edit one selected trip and re-optimize what was left mutable."""

    model_config = ConfigDict(frozen=True)

    trip_id: str = Field(min_length=1, max_length=200)
    search: TripSearchRequest
    """The search the trip came from. Re-optimization is a constrained version
    of that search, not an unrelated new one."""
    trip: SelectedTripDTO
    patch: TripPatch


class SimilarityDTO(BaseModel):
    """How much of the original trip survived the edit."""

    model_config = ConfigDict(frozen=True)

    city_preservation: float
    city_order: float
    stay_duration: float
    transport: float
    accommodation: float
    airports: float
    dates: float
    price: float
    duration: float
    total: float


class ChangeDiffDTO(BaseModel):
    """What was asked, what changed, and what it cost.

    ``improvements`` and ``costs`` are separate lists rather than one signed
    set, so a client cannot render the gains and omit the losses without
    deciding to.
    """

    model_config = ConfigDict(frozen=True)

    requested: list[str] = Field(default_factory=list)
    cities_kept: list[str] = Field(default_factory=list)
    cities_removed: list[str] = Field(default_factory=list)
    cities_added: list[str] = Field(default_factory=list)
    order_preserved: bool = True
    departure_airport_changed: bool = False
    return_airport_changed: bool = False
    previous_departure_airport: str = ""
    departure_airport: str = ""
    previous_return_airport: str = ""
    return_airport: str = ""
    metrics: list[ComparisonMetricDTO] = Field(default_factory=list)
    price_delta: float = 0.0
    transit_delta_hours: float = 0.0
    usable_delta_hours: float = 0.0
    experience_delta: float = 0.0
    preference_delta: float = 0.0
    accommodation_delta: float = 0.0
    improvements: list[str] = Field(default_factory=list)
    costs: list[str] = Field(default_factory=list)
    summary: str = ""


class TripReoptimizeResponse(BaseModel):
    """The edited trip, or an honest account of why there isn't one."""

    model_config = ConfigDict(frozen=True)

    trip_id: str
    trip: TripRecommendation | None = None
    """``None`` means the edit was coherent but nothing satisfied it. That is
    a different answer from a contradictory edit, which is a 422, and the
    client needs to tell them apart."""
    alternatives: list[TripRecommendation] = Field(default_factory=list)
    similarity: SimilarityDTO | None = None
    diff: ChangeDiffDTO | None = None

    locked: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    unsupported_operations: list[str] = Field(default_factory=list)
    """Operations the patch declared that this release does not carry out.

    Named rather than dropped: an instruction that is silently ignored looks,
    from the traveler's side, exactly like one that was obeyed and did nothing.
    """
    considered: int = 0
    issues: list[ProviderIssueDTO] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


#: What each outcome is called in front of a traveler. Here rather than in the
#: client for the same reason the failure copy is: the backend is the only
#: party that knows which of these is true.
RECHECK_MESSAGES: dict[RecheckStatus, str] = {
    RecheckStatus.UNCHANGED: "Still available at the price you saved.",
    RecheckStatus.PRICE_CHANGED: "Still available, but the price has moved.",
    RecheckStatus.PARTIALLY_UNAVAILABLE: (
        "Part of this trip is no longer available."
    ),
    RecheckStatus.UNAVAILABLE: "This trip is no longer available.",
    RecheckStatus.UNVERIFIABLE: (
        "We couldn't check this trip just now — that's a problem on our side, "
        "not a sign the trip has gone."
    ),
}


# ---------------------------------------------------------------------------
# Revalidation (V8 Phase 3)
#
# `/api/v1/trips/revalidate` re-checks a selected itinerary against the real
# provider before any move toward booking. The request references a
# server-issued `selection_id` and nothing else - the server holds the
# discovered price and terms, so the client cannot assert them.
# ---------------------------------------------------------------------------


class RevalidateRequest(BaseModel):
    """Re-check the itinerary behind one server-issued selection id."""

    model_config = ConfigDict(frozen=True)

    selection_id: str = Field(min_length=1, max_length=200)
    tolerance_absolute: float = Field(
        default=0.0, ge=0.0, le=100_000.0,
        description="Euros of price increase to tolerate without reconfirmation.",
    )
    tolerance_percentage: float = Field(
        default=0.0, ge=0.0, le=100.0,
        description="Percent of the discovered total to tolerate. Both bounds apply.",
    )


class OfferChangeDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    discovered: str
    current: str
    severity: str
    detail: str = ""


class RevalidatedOfferDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    offer_id: str
    leg: str
    status: OfferRevalidationStatus
    discovered_amount: float
    current_amount: float | None = None
    delta: float | None = None
    discovered_currency: str
    current_currency: str | None = None
    changes: list[OfferChangeDTO] = Field(default_factory=list)
    detail: str = ""


class RevalidateResponse(BaseModel):
    """Whether the selected itinerary may proceed toward booking, and why."""

    model_config = ConfigDict(frozen=True)

    selection_id: str
    status: RevalidationStatus
    bookable: bool
    may_proceed: bool
    """True only for READY / READY_WITH_MINOR_CHANGE. The single flag a client
    gates the confirm button on."""
    message: str
    discovered_total: float
    current_total: float | None = None
    delta: float | None = None
    delta_pct: float | None = None
    currency: str
    tolerance_absolute: float
    tolerance_percentage: float
    offers: list[RevalidatedOfferDTO] = Field(default_factory=list)
    checked_at: datetime


REVALIDATION_MESSAGES: dict[RevalidationStatus, str] = {
    RevalidationStatus.READY: "Still bookable at the price you saw.",
    RevalidationStatus.READY_WITH_MINOR_CHANGE: (
        "Still bookable. The price moved slightly, within tolerance — details below."
    ),
    RevalidationStatus.USER_RECONFIRMATION_REQUIRED: (
        "Something you agreed to has changed. Please review and confirm again."
    ),
    RevalidationStatus.NOT_BOOKABLE: (
        "This itinerary can't proceed as selected — see which part and why."
    ),
}


# ---------------------------------------------------------------------------
# Booking flow (V8 Phase 4)
#
# One user confirmation for a whole Detoura journey. No real payment. A
# SANDBOX_BOOKED run creates a Duffel Test Mode Order per leg; a DEMO_ONLY run
# creates none and the resulting pass says so. The server owns every price,
# status and the journey reference - the client sends intent, not truth.
# ---------------------------------------------------------------------------


class DemoLegInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    origin: str = Field(min_length=2, max_length=40)
    destination: str = Field(min_length=2, max_length=40)
    departure: datetime
    arrival: datetime
    carrier: str = Field(default="", max_length=40)
    flight_number: str = Field(default="", max_length=12)
    price_per_person: float = Field(ge=0.0, le=100_000.0)
    cabin: str = "unknown"
    checked: str = "unknown"


class CreateBookingIntentRequest(BaseModel):
    """Start a booking from a server-issued selection, or a demo trip.

    ``service_tier`` and ``promo_code`` are the customer's *choice*, not a
    price. The server computes every amount; a price, fee, markup or discount
    in this body is ignored because there is no field for one.
    """

    model_config = ConfigDict(frozen=True)

    selection_id: str | None = Field(default=None, max_length=200)
    demo_trip_label: str | None = Field(default=None, max_length=120)
    demo_currency: str = Field(default="EUR", max_length=3)
    demo_total: float = Field(default=0.0, ge=0.0, le=1_000_000.0)
    demo_travelers: int = Field(default=1, ge=1, le=9)
    demo_legs: list[DemoLegInput] = Field(default_factory=list, max_length=8)
    service_tier: ServiceTier = ServiceTier.BASIC
    promo_code: str | None = Field(default=None, max_length=40)


class SetCommercialOptionsRequest(BaseModel):
    """Change the tier and/or promo on a booking that has not been confirmed,
    and get the re-priced breakdown back."""

    model_config = ConfigDict(frozen=True)

    service_tier: ServiceTier | None = None
    promo_code: str | None = Field(default=None, max_length=40)
    clear_promo: bool = False


class PriceBreakdownDTO(BaseModel):
    """A transparent, itemised price. Every unavoidable Detoura amount is here
    and consistent with what search showed - the supplier figure is never
    quietly inflated."""

    model_config = ConfigDict(frozen=True)

    currency: str
    supplier_transport: float
    supplier_baggage: float
    supplier_fees: float
    supplier_total: float
    detoura_service_fee: float
    detoura_markup: float
    detoura_revenue_gross: float
    discount: float
    tax: float
    customer_total: float
    explanation: list[str] = Field(default_factory=list)


class ServiceTierOptionDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    tier: ServiceTier
    label: str
    summary: str
    detoura_fee: float
    customer_total: float
    selected: bool


class CommercialSummaryDTO(BaseModel):
    """The commercial view of a booking: the chosen tier, the price breakdown,
    the promo outcome, and the tier options with their prices so the customer
    can choose with the numbers in front of them."""

    model_config = ConfigDict(frozen=True)

    service_tier: ServiceTier
    service_tier_label: str
    breakdown: PriceBreakdownDTO
    markup_policy: str
    promo_code: str | None = None
    promo_accepted: bool = False
    promo_message: str = ""
    tier_options: list[ServiceTierOptionDTO] = Field(default_factory=list)
    test_mode: bool = True


class TravelerInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    given_name: str = Field(min_length=1, max_length=60)
    family_name: str = Field(min_length=1, max_length=60)
    born_on: date
    email: str = Field(min_length=3, max_length=120)
    phone: str = Field(min_length=6, max_length=20)
    title: str | None = Field(default=None, max_length=8)
    gender: str | None = Field(default=None, max_length=1)
    nationality: str | None = Field(default=None, max_length=2)


class SubmitTravelersRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    travelers: list[TravelerInput] = Field(min_length=1, max_length=9)


class ConfirmBookingRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    tolerance_absolute: float = Field(default=25.0, ge=0.0, le=100_000.0)
    tolerance_percentage: float = Field(default=0.0, ge=0.0, le=100.0)


class BookingItemStateDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    sequence: int
    origin_city: str
    origin_airport: str
    destination_city: str
    destination_airport: str
    departure: datetime
    arrival: datetime
    carrier: str = ""
    flight_number: str = ""
    cabin_baggage: str = "unknown"
    checked_baggage: str = "unknown"
    price_per_person: float
    current_price: float | None = None
    currency: str
    state: BookingState
    detail: str = ""
    provider_order_id: str | None = None


class BookingIntentResponse(BaseModel):
    """The whole booking, as the server sees it. Polled while it runs."""

    model_config = ConfigDict(frozen=True)

    booking_id: str
    journey_reference: str
    mode: PassMode
    phase: BookingPhase
    trip_label: str
    route_cities: list[str]
    currency: str
    discovered_total: float
    current_total: float | None = None
    reconfirm_note: str = ""
    party_size: int
    travelers_submitted: bool
    items: list[BookingItemStateDTO]
    pass_available: bool
    """True once the run reached a terminal phase and a pass can be fetched."""
    commercial: CommercialSummaryDTO | None = None
    """The transparent price: supplier fare vs. Detoura's own fee, the chosen
    service tier and the promo outcome. Present once the intent is priced."""


class TravelPassTicketDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    sequence: int
    origin_city: str
    origin_airport: str
    destination_city: str
    destination_airport: str
    departure: datetime
    arrival: datetime
    carrier: str = ""
    flight_number: str = ""
    cabin_baggage: str = "unknown"
    checked_baggage: str = "unknown"
    price_per_person: float
    currency: str
    booking_state: BookingState
    confirmed: bool
    provider_order_id: str | None = None


class TravelPassResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    journey_reference: str
    booking_id: str
    mode: PassMode
    status: PassStatus
    traveler_name: str
    party_size: int
    route_cities: list[str]
    travel_dates: list[str]
    tickets: list[TravelPassTicketDTO]
    tickets_prepared: int
    trip_total: float
    currency: str
    baggage_complete: bool
    unknowns: list[str]
    provider_order_ids: list[str]
    disclaimer: dict
    headline: str
    mode_note: str
    generated_at: datetime
