"""Detoura - AI travel discovery and optimization.

A deterministic, multi-objective route optimizer over a synthetic European
transport network. See ``README.md`` for the architecture and algorithm.
"""

from .config import PlannerConfig, ScoreWeights
from .models import (
    BaselineComparison,
    BaselineResult,
    Destination,
    Itinerary,
    PlanResult,
    SearchState,
    TransportOption,
    TransportType,
    TravelPreferences,
    TripRequest,
)
from .providers import (
    StaticDestinationProvider,
    SyntheticTransportDataProvider,
)
from .services import TravelPlanner

__version__ = "0.1.0"

__all__ = [
    "BaselineComparison",
    "BaselineResult",
    "Destination",
    "Itinerary",
    "PlanResult",
    "PlannerConfig",
    "ScoreWeights",
    "SearchState",
    "StaticDestinationProvider",
    "SyntheticTransportDataProvider",
    "TransportOption",
    "TransportType",
    "TravelPlanner",
    "TravelPreferences",
    "TripRequest",
    "__version__",
]
