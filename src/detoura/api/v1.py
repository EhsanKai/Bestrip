"""The Detoura product API (V5.9).

`/api/v1` is what the frontend talks to. The engine's own routes stay mounted
at the root for development and for anyone who wants the full trace, but no
screen uses them: this router is the contract, and it is deliberately narrower
than what the engine can say.

The most important thing in this file is not an endpoint, it is the
distinction the search endpoint refuses to blur. Empty recommendations plus a
provider outage returns issues and no guidance; empty recommendations with
every provider healthy returns guidance and no issues. The UI renders those as
completely different screens, and it can only do that because the backend
decided which one is true.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from ..models.trip import TravelPreferences, TripRequest
from ..profiles import PROFILES, ProfileName
from ..providers.failures import FailureLog
from ..search_modes import MODE_SETTINGS, SearchMode, apply_mode
from ..services.budget_sensitivity import analyze_budget_sensitivity
from ..services.planner import TravelPlanner
from .assembler import build_response
from .contracts import TripSearchRequest, TripSearchResponse

router = APIRouter(prefix="/api/v1", tags=["detoura"])

_planner = TravelPlanner()


def get_planner() -> TravelPlanner:
    """Dependency hook so tests can inject a planner over synthetic fixtures."""
    return _planner


def to_trip_request(body: TripSearchRequest) -> TripRequest:
    """Product request -> engine request.

    The interest chips become preference weights. A chip is a statement of
    interest, not a measurement, so it maps to a single high weight rather than
    a fabricated distribution: pretending the traveler said "culture 0.83"
    would be inventing precision from a click.
    """
    interests = body.validated_interests()
    dislikes = body.validated_dislikes()
    weights = {name: 0.85 for name in interests}
    for name in dislikes:
        weights[name] = 0.0

    try:
        start = date.fromisoformat(body.date_from)
        end = date.fromisoformat(body.date_to)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    if end <= start:
        end = start + timedelta(days=body.duration_days)

    return TripRequest(
        origin=body.origin,
        budget=body.budget,
        travelers=body.travelers,
        duration_days=body.duration_days,
        date_from=start,
        date_to=end,
        date_flexible=body.date_flexible,
        transport_preferences=body.transport,
        preferred_destinations=body.preferred_destinations,
        avoid_destinations=body.avoided_destinations,
        previously_visited=body.previously_visited,
        preferred_experiences=interests,
        disliked_experiences=dislikes,
        preferred_city_count=body.preferred_city_count,
        accommodation_preference=body.accommodation_preference,
        preferences=TravelPreferences(**weights) if weights else TravelPreferences(),
        profile=body.profile,
    )


def _closest_price(planner: TravelPlanner, request: TripRequest) -> float | None:
    """The cheapest trip that exists if the budget were not binding.

    Only called when nothing matched, and only to answer the one question a
    traveler actually has at that moment: *how far off was I?* Re-planning at a
    much larger budget is the honest way to find out; guessing would not be.
    """
    try:
        probe = request.model_copy(update={"budget": request.budget * 3})
        result = planner.plan(probe)
    except ValueError:
        return None
    if not result.recommendations:
        return None
    return min(item.total_cost for item in result.recommendations)


@router.post("/search", response_model=TripSearchResponse)
def search(
    body: TripSearchRequest,
    planner: TravelPlanner = Depends(get_planner),
) -> TripSearchResponse:
    """Find trips worth taking.

    The search mode decides how hard we look; it is the only search control the
    product exposes, and it maps to engine configuration here rather than
    leaking a beam width into the client.
    """
    request = to_trip_request(body)
    mode = body.search_mode
    failures = FailureLog()

    # A per-request planner only when the mode actually needs different
    # configuration, so the default SMART path keeps the shared warm caches.
    settings = apply_mode(planner.config, mode)
    if settings == planner.config:
        active = planner
    else:
        active = TravelPlanner(
            transport_provider=planner.transport.inner,
            destination_provider=planner.destinations,
            config=settings,
            accommodation_provider=planner.accommodation.inner,
            ground_transfer_provider=planner.ground_transfer.inner,
        )

    try:
        result = active.plan(request, profile=body.profile)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    closest = None
    if not result.recommendations and not failures.degraded:
        closest = _closest_price(active, request)

    return build_response(
        result,
        request,
        body,
        mode=mode,
        failures=failures,
        closest_price=closest,
    )


@router.post("/budget-sensitivity")
def budget_sensitivity(
    body: TripSearchRequest,
    steps: int = Query(default=6, ge=2, le=10),
    planner: TravelPlanner = Depends(get_planner),
) -> dict:
    """What would another fifty euros unlock? (Part 16)"""
    request = to_trip_request(body)
    try:
        analysis = analyze_budget_sensitivity(
            planner, request, steps=steps, profile=body.profile
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    thresholds = {step.budget for step in analysis.thresholds}
    return {
        "currency": analysis.currency,
        "profile": analysis.profile.value,
        "minimum_feasible_budget": analysis.minimum_feasible_budget,
        "steps": [
            {
                "budget": step.budget,
                "feasible": step.feasible,
                "trips_found": len(step.city_sets),
                "best_price": step.best_cost if step.feasible else None,
                "best_route": step.best_route if step.feasible else None,
                "unlocks": [sorted(cities) for cities in step.unlocked],
                "is_threshold": step.budget in thresholds,
            }
            for step in analysis.steps
        ],
    }


@router.get("/origins/{query}")
def origins(query: str, planner: TravelPlanner = Depends(get_planner)) -> dict:
    """Departure airports for a place, with what it costs to reach each.

    The Discover screen shows these under the origin field, so the traveler
    learns early that Detoura counts the ride to the airport - which is one of
    the things that makes its answers differ from a flight search.
    """
    try:
        candidates = planner.origin_resolver.resolve(query, planner.config)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    airports = []
    for candidate in candidates:
        options = planner.ground_transfer.search(query, candidate.code)
        cheapest = min(options, key=lambda o: o.price_per_person, default=None)
        airports.append(
            {
                "code": candidate.code,
                "name": candidate.name,
                "city": candidate.city,
                "distance_km": candidate.distance_km,
                "transfer_price": cheapest.price_per_person if cheapest else None,
                "transfer_minutes": cheapest.duration_minutes if cheapest else None,
            }
        )
    return {"origin": query, "airports": airports}


@router.get("/destinations")
def destinations(planner: TravelPlanner = Depends(get_planner)) -> list[dict]:
    """The catalog, described in interests rather than raw attributes (V5.7)."""
    from ..models.destination import EXPERIENCE_ATTRIBUTES

    catalog = []
    for destination in planner.destinations.all():
        scores = {name: getattr(destination, name) for name in EXPERIENCE_ATTRIBUTES}
        strengths = sorted(
            (name for name, value in scores.items() if value >= 0.75),
            key=lambda name: -scores[name],
        )[:4]
        catalog.append(
            {
                "id": destination.id,
                "name": destination.name,
                "country": destination.country,
                "recommended_min_days": destination.recommended_min_days,
                "recommended_max_days": destination.recommended_max_days,
                "strengths": strengths,
            }
        )
    return catalog


@router.get("/profiles")
def profiles() -> list[dict]:
    """The three ways of answering "what is the best trip?"."""
    labels = {
        ProfileName.CHEAPEST: ("Cheapest", "Spend as little as the trip allows."),
        ProfileName.BEST_VALUE: (
            "Best value",
            "The best trip your money and time can buy.",
        ),
        ProfileName.ADVENTURE: ("Adventure", "See more places, without the slog."),
    }
    return [
        {
            "name": name.value,
            "label": labels[name][0],
            "description": labels[name][1],
        }
        for name in PROFILES
    ]


@router.get("/search-modes")
def search_modes() -> list[dict]:
    """How hard Detoura can look, and roughly what each costs in seconds."""
    return [
        {
            "name": mode.value,
            "label": mode.label,
            "description": mode.blurb,
            "estimated_seconds": list(mode.estimated_seconds),
            "is_default": mode is SearchMode.SMART,
        }
        for mode in MODE_SETTINGS
    ]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "product": "Detoura", "data_source": "synthetic"}
