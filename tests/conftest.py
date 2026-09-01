"""Shared fixtures and builders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pytest

from travel_planner.config import PlannerConfig
from travel_planner.data.destinations import DESTINATIONS
from travel_planner.data.synthetic_transport import Connection
from travel_planner.models.search import SearchState
from travel_planner.models.transport import TransportOption, TransportType
from travel_planner.models.trip import TravelPreferences, TripRequest
from travel_planner.providers.destinations import StaticDestinationProvider
from travel_planner.providers.transport import SyntheticTransportDataProvider
from travel_planner.services.origin_resolver import StaticOriginResolver
from travel_planner.services.planner import TravelPlanner

WINDOW_FROM = date(2026, 9, 10)
WINDOW_TO = date(2026, 9, 15)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def leg(
    origin: str,
    destination: str,
    departure: datetime,
    minutes: int,
    price: float,
    transport_type: TransportType = TransportType.FLIGHT,
    leg_id: str | None = None,
) -> TransportOption:
    """Build a single transport leg without going through a provider."""
    return TransportOption(
        id=leg_id
        or f"{origin}-{destination}-{departure:%Y%m%dT%H%M}-{transport_type.value}",
        origin=origin,
        destination=destination,
        departure=departure,
        arrival=departure + timedelta(minutes=minutes),
        price_per_person=price,
        transport_type=transport_type,
        duration_minutes=minutes,
    )


def make_state(
    legs: list[TransportOption],
    *,
    travelers: int = 1,
    completed: bool = True,
    start: datetime | None = None,
) -> SearchState:
    """Assemble a :class:`SearchState` from a chain of legs.

    The last leg is treated as the return leg when ``completed`` is true, and
    stay lengths are derived from the actual dates, exactly as beam search
    does it.
    """
    origin = legs[0].origin
    moment = start or datetime.combine(legs[0].departure.date(), time.min)
    state = SearchState(
        origin_airport=origin,
        current_location=origin,
        start_datetime=moment,
        current_datetime=moment,
    )
    for index, option in enumerate(legs):
        is_return = completed and index == len(legs) - 1
        stay = (
            None
            if index == 0
            else (option.departure.date() - legs[index - 1].arrival.date()).days
        )
        state = state.extend(
            option, travelers=travelers, stay_days=stay, is_return=is_return
        )
    return state


def trip_request(**overrides) -> TripRequest:
    """A valid request with sensible defaults, overridable per test."""
    payload = dict(
        origin="Köln",
        budget=250.0,
        travelers=2,
        duration_days=5,
        date_from=WINDOW_FROM,
        date_to=WINDOW_TO,
        date_flexible=True,
        transport_preferences=[TransportType.FLIGHT, TransportType.TRAIN],
        preferred_destinations=["Madrid"],
        avoid_destinations=["Paris"],
        preferences=TravelPreferences(
            history=0.8,
            nature=0.7,
            nightlife=0.2,
            culture=0.8,
            food=0.6,
            multiple_cities=0.9,
        ),
    )
    payload.update(overrides)
    return TripRequest(**payload)


# ---------------------------------------------------------------------------
# Standard synthetic-network fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def config() -> PlannerConfig:
    return PlannerConfig()


@pytest.fixture
def destinations() -> StaticDestinationProvider:
    return StaticDestinationProvider()


@pytest.fixture
def transport() -> SyntheticTransportDataProvider:
    return SyntheticTransportDataProvider()


@pytest.fixture
def planner(transport, destinations, config) -> TravelPlanner:
    return TravelPlanner(transport, destinations, config=config)


@pytest.fixture
def koln_request() -> TripRequest:
    """The scenario from the spec: Köln, EUR 250, 2 travelers, 5 days."""
    return trip_request()


# ---------------------------------------------------------------------------
# The non-greedy "trap" network
# ---------------------------------------------------------------------------
FLIGHT = TransportType.FLIGHT
BUS = TransportType.BUS

#: A minimal network built around one question: does the optimizer commit to the
#: cheapest first leg?
#:
#: * ``DUS -> London`` at 35 is by far the cheapest way out of DUS ...
#: * ... but every way home from London or Brussels is expensive, so the best
#:   itinerary reachable through London costs 120.
#: * ``DUS -> Prague`` costs 55 up front, yet ``Prague -> Vienna`` (12) and
#:   ``Vienna -> DUS`` (25) bring the complete round trip down to 92.
#:
#: There is no direct ``DUS -> Vienna``: the good trip is *only* reachable by
#: first taking the more expensive leg.
TRAP_CONNECTIONS: tuple[Connection, ...] = (
    Connection("DUS", "London", FLIGHT, 35.0, 90, ("08:00",)),
    Connection("London", "DUS", FLIGHT, 95.0, 90, ("08:00",)),
    Connection("London", "Brussels", FLIGHT, 40.0, 140, ("08:00",)),
    Connection("Brussels", "London", FLIGHT, 42.0, 140, ("08:00",)),
    Connection("Brussels", "DUS", FLIGHT, 45.0, 170, ("08:00",)),
    Connection("DUS", "Prague", FLIGHT, 55.0, 75, ("08:00",)),
    Connection("Prague", "DUS", FLIGHT, 60.0, 75, ("08:00",)),
    Connection("Prague", "Vienna", BUS, 12.0, 270, ("08:00",)),
    Connection("Vienna", "Prague", BUS, 12.0, 270, ("08:00",)),
    Connection("Vienna", "DUS", FLIGHT, 25.0, 95, ("08:00",)),
)

TRAP_CITIES = ("London", "Brussels", "Prague", "Vienna")

#: Complete round trips available in the trap network, cheapest last.
TRAP_ROUTE_COSTS = {
    ("London",): 130.0,
    ("Prague",): 115.0,
    ("London", "Brussels"): 120.0,
    ("Prague", "Vienna"): 92.0,
}


@dataclass(frozen=True)
class TrapSetup:
    planner: TravelPlanner
    transport: SyntheticTransportDataProvider
    destinations: StaticDestinationProvider
    config: PlannerConfig
    request: TripRequest


@pytest.fixture
def trap() -> TrapSetup:
    provider = SyntheticTransportDataProvider(
        TRAP_CONNECTIONS, price_variation=False
    )
    destination_provider = StaticDestinationProvider(
        [d for d in DESTINATIONS if d.id in TRAP_CITIES]
    )
    trap_config = PlannerConfig(
        beam_width=20,
        max_results=5,
        max_cities=3,
        max_city_stay_days=2,
        # Only DUS is within range, so the test is about route choice alone.
        max_origin_distance_km=20.0,
        # This fixture is about the search, not about filling five days.
        min_duration_utilization=0.0,
    )
    request = TripRequest(
        origin="Düsseldorf",
        budget=200.0,
        travelers=1,
        duration_days=5,
        date_from=WINDOW_FROM,
        date_to=WINDOW_TO,
        date_flexible=False,
        transport_preferences=[FLIGHT, BUS],
        preferences=TravelPreferences(multiple_cities=0.5),
    )
    return TrapSetup(
        planner=TravelPlanner(
            provider,
            destination_provider,
            config=trap_config,
            origin_resolver=StaticOriginResolver(),
        ),
        transport=provider,
        destinations=destination_provider,
        config=trap_config,
        request=request,
    )
