"""FastAPI adapter.

This module is a thin translation layer: HTTP in, :class:`TripRequest` out,
:class:`PlanResult` back. All decisions live in
:class:`~detoura.services.planner.TravelPlanner`, which is fully usable
without importing FastAPI at all.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import PlannerConfig
from ..models.itinerary import PlanResult
from ..models.trip import TripRequest
from ..profiles import PROFILES, ProfileName, RecommendationProfile
from ..services.budget_sensitivity import analyze_budget_sensitivity
from ..services.planner import TravelPlanner

router = APIRouter()

#: One planner instance is shared across requests: it is stateless apart from
#: the provider's read-only timetable cache, which is exactly what we want warm.
_planner = TravelPlanner()


def get_planner() -> TravelPlanner:
    """Dependency hook so tests can inject a planner over synthetic fixtures."""
    return _planner


@router.post("/plan-trip", response_model=PlanResult, response_model_exclude_none=True)
def plan_trip(
    request: TripRequest,
    debug: bool = Query(
        default=False,
        description="Return the full structured search trace (development mode).",
    ),
    profile: ProfileName | None = Query(
        default=None,
        description=(
            "Which recommendation profile to optimize for. Overrides the "
            "request body's `profile`. Defaults to BEST_VALUE."
        ),
    ),
    planner: TravelPlanner = Depends(get_planner),
) -> PlanResult:
    """Plan a trip.

    Returns the naive single-destination baseline (when a preferred destination
    was given), the ranked recommendations, and run metadata. The same request
    under a different profile is a different question, and gets a different
    answer: CHEAPEST minimizes spend, BEST_VALUE (the default) maximizes the
    trip the money buys, ADVENTURE favours seeing more places.
    """
    try:
        return planner.plan(request, debug=debug, profile=profile)
    except ValueError as error:
        # Unknown origin, no departure airport in range, ... - a client problem.
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/destinations")
def list_destinations(planner: TravelPlanner = Depends(get_planner)) -> list[dict]:
    """The synthetic destination catalog the optimizer searches over."""
    return [destination.model_dump() for destination in planner.destinations.all()]


@router.get("/config", response_model=PlannerConfig)
def get_config(planner: TravelPlanner = Depends(get_planner)) -> PlannerConfig:
    """The active planner configuration."""
    return planner.config


@router.post("/budget-sensitivity")
def budget_sensitivity(
    request: TripRequest,
    steps: int = Query(default=5, ge=1, le=12, description="Budget levels to try."),
    span: float = Query(
        default=0.6,
        gt=0.0,
        le=2.0,
        description="Sweep width as a fraction of the requested budget.",
    ),
    profile: ProfileName | None = Query(default=None),
    planner: TravelPlanner = Depends(get_planner),
) -> dict:
    """What would a bigger budget buy?

    Plans the same request at several budgets and reports the levels where a
    materially different trip becomes possible. Costs one planning run per
    step; they share the planner's warm provider caches.
    """
    try:
        analysis = analyze_budget_sensitivity(
            planner, request, steps=steps, span=span, profile=profile
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "profile": analysis.profile.value,
        "currency": analysis.currency,
        "minimum_feasible_budget": analysis.minimum_feasible_budget,
        "steps": [
            {
                "budget": step.budget,
                "feasible": step.feasible,
                "best_score": step.best_score,
                "best_cost": step.best_cost,
                "best_route": step.best_route,
                "city_sets": [sorted(cities) for cities in step.city_sets],
                "unlocks": [sorted(cities) for cities in step.unlocked],
            }
            for step in analysis.steps
        ],
    }


@router.get("/profiles", response_model=list[RecommendationProfile])
def list_profiles() -> list[RecommendationProfile]:
    """The recommendation profiles `/plan-trip` accepts, and their weights."""
    return list(PROFILES.values())


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "data_source": "synthetic"}
