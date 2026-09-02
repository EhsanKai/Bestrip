"""Engine result -> product contract (V5.9).

The one-way door. Everything the frontend sees is built here, from an engine
result plus the run's failure log, and nothing on the other side of it names a
beam or a frontier.

Two responsibilities beyond field mapping, both of which the spec calls out
explicitly and neither of which belongs in a React component:

* **Copy.** "Why we like it" and the trade-off sentence are assembled from
  typed factors and real numbers, here, where the numbers are. A frontend
  writing that prose would be a frontend inventing facts.
* **Honesty.** Whether an empty result means "nothing matched" or "a provider
  is down" is decided here, because only here is it known.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ..models.freshness import PriceFreshness, combine
from ..models.itinerary import ExplanationFactor, Itinerary, PlanResult
from ..models.trip import TripRequest
from ..providers.failures import FailureLog
from ..search_modes import SearchMode, deeper_than
from ..services.confidence import RecommendationConfidence, SearchQuality, assess
from .contracts import (
    AvailabilityStatus,
    BaselineComparisonDTO,
    ConfidenceDTO,
    ConfidenceReasonDTO,
    CostBreakdownDTO,
    DestinationMatchDTO,
    IntensityBand,
    LegDTO,
    NoResultsGuidance,
    ProviderIssueDTO,
    RelaxationSuggestion,
    SearchDiagnostics,
    StayDTO,
    TripRecommendation,
    TripSearchResponse,
)

#: Explanation factors worth showing, in the words a traveler would use.
#: Factors not listed are real but not interesting to read - they exist for
#: tests and tuning, and a UI that printed all of them would be noise.
FACTOR_PHRASES: dict[ExplanationFactor, str] = {
    ExplanationFactor.GOOD_BUDGET_USAGE: "Uses your budget well",
    ExplanationFactor.LEAVES_BUDGET_UNUSED: "Comes in well under budget",
    ExplanationFactor.CHEAPER_THAN_BASELINE: "Cheaper than your original idea",
    ExplanationFactor.STRONG_PREFERENCE_MATCH: "Strong match for your interests",
    ExplanationFactor.GREAT_DESTINATION_MATCH: "Excellent match for your interests",
    ExplanationFactor.HIGH_DESTINATION_QUALITY: "Highly rated destinations",
    ExplanationFactor.REASONABLE_TRAVEL_TIME: "Sensible amount of travel",
    ExplanationFactor.HEAVY_TRAVEL_TIME: "A lot of time in transit",
    ExplanationFactor.GOOD_USE_OF_WINDOW: "Makes the most of your days",
    ExplanationFactor.LOW_TRAVEL_INTENSITY: "Relaxed pace",
    ExplanationFactor.HIGH_TRAVEL_INTENSITY: "Fast pace",
    ExplanationFactor.GOOD_ACCOMMODATION_VALUE: "Good rooms for the money",
    ExplanationFactor.BASIC_ACCOMMODATION: "Basic accommodation",
    ExplanationFactor.ROOM_UPGRADE_WORTH_IT: "Room upgrade worth paying for",
    ExplanationFactor.ROOM_UPGRADE_POOR_VALUE: "Room upgrade of doubtful value",
    ExplanationFactor.CHEAPEST_ROOMS_TAKEN: "Cheapest rooms throughout",
    ExplanationFactor.LIMITED_AVAILABILITY: "Limited availability",
    ExplanationFactor.FULLY_REFUNDABLE: "Free cancellation on every stay",
    ExplanationFactor.IDEAL_CITY_COUNT: "The right number of cities for the time",
    ExplanationFactor.VISITS_PREFERRED_DESTINATION: "Includes a place you asked for",
    ExplanationFactor.LATE_ARRIVAL: "Late arrival on one leg",
    ExplanationFactor.EARLY_DEPARTURE: "Early departure on one leg",
    ExplanationFactor.LONG_TRANSFER_TIME: "Long airport transfers",
    ExplanationFactor.CONTAINS_DISLIKED_EXPERIENCE: "Includes something you avoid",
    ExplanationFactor.REVISITS_KNOWN_CITY: "Somewhere you have already been",
}

#: How many phrases to show. More than four and the card stops being scannable.
MAX_HIGHLIGHTS = 4


def _availability(itinerary: Itinerary) -> AvailabilityStatus:
    """What we can honestly claim about booking this.

    ``UNKNOWN`` when nothing reported inventory. Never ``SOLD_OUT`` on absent
    data - a recommendation only exists because the search found it bookable,
    so the only way to be sold out here is for a provider to have said so.
    """
    counted = [s.rooms_available for s in itinerary.stays if s.rooms_available is not None]
    seats = [leg.seats_available for leg in itinerary.legs if leg.seats_available is not None]
    if not counted and not seats:
        return AvailabilityStatus.UNKNOWN
    smallest = min(counted + seats)
    if smallest <= 0:
        return AvailabilityStatus.SOLD_OUT
    if smallest <= 3:
        return AvailabilityStatus.LIMITED
    return AvailabilityStatus.AVAILABLE


def _freshness(itinerary: Itinerary, now: datetime) -> PriceFreshness:
    """The itinerary's freshness: the worst of its parts."""
    parts: list[PriceFreshness | None] = []
    for leg in itinerary.legs:
        parts.append(leg.provenance.freshness_at(now) if leg.provenance else None)
    for stay in itinerary.stays:
        parts.append(stay.provenance.freshness_at(now) if stay.provenance else None)
    return combine(*parts)


def _value_note(stay) -> str | None:
    """"You paid EUR 22 more for a significantly better-rated room." (V5.5.1)

    Only where the data supports it. No premium, no note - and a note is not
    manufactured from a rating difference we cannot see.
    """
    premium = getattr(stay, "accommodation_premium", 0.0) or 0.0
    if premium <= 1.0:
        return None
    rating = stay.accommodation_rating
    if rating is None:
        return f"{premium:.0f} EUR more than the cheapest room offered here."
    stars = rating * 5.0
    return (
        f"{premium:.0f} EUR more than the cheapest room here, "
        f"for a {stars:.1f}-star stay."
    )


def _why_we_like_it(itinerary: Itinerary) -> str:
    """One sentence, built from numbers that exist.

    Deliberately templated rather than generated: this is the card's headline
    claim, and it must be true by construction. The LLM explainer is available
    for richer prose and is guarded separately.
    """
    cities = itinerary.cities
    strengths: list[str] = []
    for insight in itinerary.destination_insights:
        strengths.extend(insight.strengths)
    top = list(dict.fromkeys(strengths))[:2]

    if len(cities) == 1:
        opening = f"{cities[0]} on its own"
    elif len(cities) == 2:
        opening = f"{cities[0]} and {cities[1]} together"
    else:
        opening = f"{len(cities)} cities in one trip"

    if top:
        middle = f", strong on {' and '.join(top)}"
    else:
        middle = ""

    hours = itinerary.usable_destination_minutes / 60
    transit = itinerary.total_transport_minutes / 60
    return (
        f"{opening}{middle}, with {hours:.0f} hours on the ground "
        f"against {transit:.1f} hours travelling."
    )


def _tradeoff(itinerary: Itinerary) -> str | None:
    """What this costs relative to the traveler's own idea.

    ``None`` when there is no baseline: inventing a comparison would be worse
    than showing none.
    """
    comparison = itinerary.baseline_comparison
    if comparison is None:
        return None
    delta = -comparison.money_saved
    hours = itinerary.usable_destination_minutes / 60
    if delta > 0:
        gains = []
        if comparison.additional_cities > 0:
            plural = "city" if comparison.additional_cities == 1 else "cities"
            gains.append(f"{comparison.additional_cities} more {plural}")
        gains.append(f"{hours:.0f} hours on the ground")
        return (
            f"{delta:.0f} EUR more than your {comparison.baseline_destination} "
            f"idea, for {' and '.join(gains)}."
        )
    return (
        f"{abs(delta):.0f} EUR less than your {comparison.baseline_destination} idea."
    )


def _confidence_dto(confidence: RecommendationConfidence) -> ConfidenceDTO:
    return ConfidenceDTO(
        level=confidence.level,
        label=confidence.level.label,
        reasons=[
            ConfidenceReasonDTO(label=r.label, positive=r.positive)
            for r in confidence.reasons
        ],
    )


def _baseline_dto(result: PlanResult, itinerary: Itinerary | None) -> BaselineComparisonDTO | None:
    baseline = result.baseline
    if baseline is None:
        return None
    if itinerary is None:
        return BaselineComparisonDTO(
            destination=baseline.destination,
            total_price=baseline.total_cost,
            nights=baseline.nights,
            usable_hours=round(baseline.usable_destination_minutes / 60, 1),
            price_delta=0.0,
            extra_cities=0,
            extra_usable_hours=0.0,
            extra_travel_minutes=0,
        )
    comparison = itinerary.baseline_comparison
    baseline_hours = round(baseline.usable_destination_minutes / 60, 1)
    ours_hours = round(itinerary.usable_destination_minutes / 60, 1)
    return BaselineComparisonDTO(
        destination=baseline.destination,
        total_price=baseline.total_cost,
        nights=baseline.nights,
        usable_hours=baseline_hours,
        price_delta=round(-comparison.money_saved, 2) if comparison else 0.0,
        extra_cities=comparison.additional_cities if comparison else 0,
        extra_usable_hours=round(ours_hours - baseline_hours, 1),
        extra_travel_minutes=comparison.additional_travel_minutes if comparison else 0,
    )


def recommendation_dto(
    itinerary: Itinerary,
    result: PlanResult,
    quality: SearchQuality,
    *,
    travelers: int,
    now: datetime,
) -> TripRecommendation:
    """One itinerary, translated."""
    value = itinerary.value_breakdown
    freshness = _freshness(itinerary, now)
    confidence = assess(itinerary, quality, freshness=freshness, dominated=False)

    highlights = [
        FACTOR_PHRASES[factor]
        for factor in itinerary.explanation_factors
        if factor in FACTOR_PHRASES
    ][:MAX_HIGHLIGHTS]

    return TripRecommendation(
        id=f"{itinerary.rank}-" + "-".join(itinerary.route_nodes).lower().replace(" ", ""),
        rank=itinerary.rank,
        route=itinerary.route_label(),
        route_nodes=itinerary.route_nodes,
        cities=itinerary.cities,
        origin_airport=itinerary.origin_airport,
        return_airport=itinerary.return_airport,
        departure=itinerary.departure,
        arrival=itinerary.arrival,
        duration_days=round(itinerary.duration_days, 2),
        nights=itinerary.stay_days,
        total_price=itinerary.total_cost,
        price_per_person=round(itinerary.total_cost / max(travelers, 1), 2),
        currency=itinerary.currency,
        costs=CostBreakdownDTO(
            transport=itinerary.cost_breakdown.transport,
            accommodation=itinerary.cost_breakdown.accommodation,
            ground_transfer=itinerary.cost_breakdown.ground_transfer,
            total=itinerary.cost_breakdown.total,
        ),
        usable_hours=round(itinerary.usable_destination_minutes / 60, 1),
        travel_hours=round(itinerary.total_transport_minutes / 60, 1),
        transfer_minutes=itinerary.ground_transfer_minutes,
        travel_intensity=value.travel_intensity if value else 0.0,
        intensity_band=IntensityBand.of(value.travel_intensity if value else 0.0),
        experience_score=value.experience if value else 0.0,
        preference_match=value.preferences if value else 0.0,
        accommodation_score=value.accommodation if value else 0.0,
        travel_value=itinerary.score,
        profile=itinerary.profile or result.profile,
        confidence=_confidence_dto(confidence),
        price_freshness=freshness,
        availability=_availability(itinerary),
        baseline_comparison=_baseline_dto(result, itinerary),
        highlights=highlights,
        tradeoff=_tradeoff(itinerary),
        why_we_like_it=_why_we_like_it(itinerary),
        stays=[
            StayDTO(
                city=stay.city,
                arrival=stay.arrival,
                departure=stay.departure,
                nights=stay.nights,
                cost=stay.accommodation_cost,
                name=stay.accommodation_name,
                tier=stay.accommodation_tier,
                type=stay.accommodation_type,
                rating=stay.accommodation_rating,
                location_score=stay.accommodation_location_score,
                free_cancellation=stay.free_cancellation,
                usable_minutes=stay.usable_minutes,
                rooms_available=stay.rooms_available,
                cheapest_alternative_cost=stay.cheapest_alternative_cost,
                premium=stay.accommodation_premium,
                value_note=_value_note(stay),
            )
            for stay in itinerary.stays
        ],
        legs=[
            LegDTO(
                **{"from": leg.origin, "to": leg.destination},
                departure=leg.departure,
                arrival=leg.arrival,
                minutes=leg.duration_minutes,
                mode=leg.transport_type.value,
                operator=leg.operator,
                price_per_person=leg.price_per_person,
                seats_available=leg.seats_available,
            )
            for leg in itinerary.legs
        ],
        destination_matches=[
            DestinationMatchDTO(
                city=insight.city,
                match=insight.score,
                quality=insight.quality,
                stay_quality=insight.stay_quality,
                usable_days=insight.usable_days,
                strengths=insight.strengths,
                weaknesses=insight.weaknesses,
                disliked_present=insight.dislikes_present,
                previously_visited=insight.previously_visited,
                note=insight.stay_note,
            )
            for insight in itinerary.destination_insights
        ],
    )


def relaxations(request: TripRequest, closest: float | None) -> list[RelaxationSuggestion]:
    """Concrete ways to loosen a search that found nothing (Part 24).

    Each carries a patch the client can merge and re-run, so "try a bigger
    budget" is a button rather than advice. Ordered by how little they ask the
    traveler to give up.
    """
    suggestions: list[RelaxationSuggestion] = []

    if closest is not None and closest > request.budget:
        # Ask for exactly what is needed, rounded up to something a person
        # would actually type, rather than a generic "+10%".
        needed = int((closest - request.budget) // 10 + 1) * 10
        suggestions.append(
            RelaxationSuggestion(
                label=f"+{needed} EUR budget",
                description=(
                    f"The closest trip we found costs {closest:.0f} EUR."
                ),
                patch={"budget": request.budget + needed},
            )
        )

    if not request.date_flexible:
        suggestions.append(
            RelaxationSuggestion(
                label="Flexible dates",
                description="Let us shift your start date within the window.",
                patch={"date_flexible": True},
            )
        )

    if request.avoid_destinations:
        suggestions.append(
            RelaxationSuggestion(
                label="Allow more destinations",
                description=(
                    f"You excluded {', '.join(request.avoid_destinations)}."
                ),
                patch={"avoided_destinations": []},
            )
        )

    if request.preferred_city_count and request.preferred_city_count > 1:
        suggestions.append(
            RelaxationSuggestion(
                label="One fewer city",
                description="Fewer cities means less transport to pay for.",
                patch={"preferred_city_count": request.preferred_city_count - 1},
            )
        )

    if request.duration_days > 3:
        suggestions.append(
            RelaxationSuggestion(
                label="A shorter trip",
                description="Fewer nights is the fastest way to fit a budget.",
                patch={"duration_days": request.duration_days - 1},
            )
        )
    return suggestions


def build_response(
    result: PlanResult,
    request: TripRequest,
    api_request,
    *,
    mode: SearchMode,
    failures: FailureLog | None = None,
    closest_price: float | None = None,
    now: datetime | None = None,
) -> TripSearchResponse:
    """Assemble the whole product response.

    The important branch is at the bottom: an empty recommendation list means
    one of two completely different things, and this is the only place that
    knows which.
    """
    moment = now or datetime.now()
    log = failures or FailureLog()
    metadata = result.metadata
    rounds = len(metadata.beam_rounds) or 1

    winner_stable = True
    if len(metadata.beam_rounds) > 1:
        # The winner is stable when the final widening did not improve on it.
        winner_stable = metadata.beam_rounds[-1]["improvement"] <= 0

    quality = SearchQuality(
        rounds=rounds,
        winner_stable=winner_stable,
        frontier_size=metadata.pareto_kept,
        completed=metadata.completed_itineraries,
        alternatives_returned=len(result.recommendations),
        degraded=log.degraded,
        deep=mode is SearchMode.DEEP,
    )

    recommendations = [
        recommendation_dto(
            itinerary, result, quality, travelers=request.travelers, now=moment
        )
        for itinerary in result.recommendations
    ]

    diagnostics = SearchDiagnostics(
        mode=mode,
        elapsed_seconds=metadata.elapsed_seconds,
        itineraries_considered=metadata.completed_itineraries,
        alternatives_evaluated=metadata.pareto_kept,
        destinations_explored=len(
            {city for r in recommendations for city in r.cities}
        ),
        rounds=rounds,
        deeper_search_available=deeper_than(mode) is not None,
        notes=list(metadata.warnings),
    )

    issues = [
        ProviderIssueDTO(
            kind=str(entry["kind"]),
            provider=str(entry["provider"]),
            message=str(entry["message"]),
            retryable=bool(entry["retryable"]),
            occurrences=int(entry["occurrences"]),  # type: ignore[arg-type]
        )
        for entry in log.summary()
    ]

    # The branch this whole module exists for. An empty list plus an
    # infrastructure failure is NOT "no trips found" - the search never
    # completed, and offering budget advice for a provider outage would be
    # both useless and misleading. `issues` carries the truth; `no_results`
    # stays None so the UI renders an error state rather than guidance.
    guidance = None
    if not recommendations and not log.degraded:
        guidance = NoResultsGuidance(
            reason=(
                "No trip fits all of your constraints."
                if closest_price is None
                else "No trip fits your budget."
            ),
            closest_price=closest_price,
            requested_budget=request.budget,
            suggestions=relaxations(request, closest_price),
        )

    return TripSearchResponse(
        request_id=uuid.uuid4().hex[:12],
        origin=request.origin,
        origin_airports=list(metadata.origin_airports),
        currency=metadata.currency,
        profile=result.profile,
        recommendations=recommendations,
        baseline=_baseline_dto(
            result, result.recommendations[0] if result.recommendations else None
        ),
        diagnostics=diagnostics,
        issues=issues,
        no_results=guidance,
    )
