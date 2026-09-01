"""FastAPI adapter.

This module is a thin translation layer: HTTP in, :class:`TripRequest` out,
:class:`PlanResult` back. All decisions live in
:class:`~travel_planner.services.planner.TravelPlanner`, which is fully usable
without importing FastAPI at all.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import PlannerConfig
from ..models.itinerary import PlanResult
from ..models.trip import TripRequest
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
    planner: TravelPlanner = Depends(get_planner),
) -> PlanResult:
    """Plan a trip.

    Returns the naive single-destination baseline (when a preferred destination
    was given), the ranked recommendations, and run metadata.
    """
    try:
        return planner.plan(request, debug=debug)
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


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "data_source": "synthetic"}
