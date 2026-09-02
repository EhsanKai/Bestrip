"""Detoura domain models."""

from .debug import (
    FilteredItinerary,
    FilterStage,
    IterationDebug,
    PrunedState,
    RejectedState,
    RejectionReason,
    SearchDebug,
)
from .destination import ATTRIBUTES, Destination
from .itinerary import (
    BaselineComparison,
    BaselineResult,
    Itinerary,
    PlannerMetadata,
    PlanResult,
    ScoreBreakdown,
)
from .search import SearchState
from .transport import TransportOption, TransportType
from .trip import TravelPreferences, TripRequest

__all__ = [
    "ATTRIBUTES",
    "BaselineComparison",
    "BaselineResult",
    "Destination",
    "FilterStage",
    "FilteredItinerary",
    "Itinerary",
    "IterationDebug",
    "PlanResult",
    "PlannerMetadata",
    "PrunedState",
    "RejectedState",
    "RejectionReason",
    "ScoreBreakdown",
    "SearchDebug",
    "SearchState",
    "TransportOption",
    "TransportType",
    "TravelPreferences",
    "TripRequest",
]
