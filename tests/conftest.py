"""Shared fixtures and builders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pytest

from travel_planner.config import PlannerConfig
from travel_planner.data.destinations import DESTINATIONS
from travel_planner.data.synthetic_transport import Connection
from travel_planner.models.accommodation import AccommodationOption
from travel_planner.models.search import SearchState
from travel_planner.models.transfer import GroundTransferMode, GroundTransferOption
from travel_planner.models.transport import TransportOption, TransportType
from travel_planner.models.trip import TravelPreferences, TripRequest
from travel_planner.profiles import ProfileName
from travel_planner.providers.accommodation import (
    NoAccommodationProvider,
    SyntheticAccommodationDataProvider,
)
from travel_planner.providers.destinations import StaticDestinationProvider
from travel_planner.providers.ground_transfer import (
    FreeGroundTransferProvider,
    SyntheticGroundTransferProvider,
)
from travel_planner.providers.transport import SyntheticTransportDataProvider
from travel_planner.services.origin_resolver import StaticOriginResolver
from travel_planner.services.planner import TravelPlanner
from travel_planner.usable_time import usable_minutes

WINDOW_FROM = date(2026, 9, 10)
WINDOW_TO = date(2026, 9, 15)

#: V1 priced only transport, so EUR 250 bought a five-day trip for two. With
#: accommodation and ground transfers in the model that budget buys nothing at
#: all in the synthetic network - the cheapest complete trip for two is around
#: EUR 300 - so the shared fixture budget moves up. Tests that care about the
#: old number set it explicitly.
V2_DEFAULT_BUDGET = 450.0


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
    rooms: dict[str, AccommodationOption] | None = None,
    cheapest_rooms: dict[str, AccommodationOption] | None = None,
    outbound_transfer: GroundTransferOption | None = None,
    return_transfer: GroundTransferOption | None = None,
) -> SearchState:
    """Assemble a :class:`SearchState` from a chain of legs.

    The last leg is treated as the return leg when ``completed`` is true, and
    stay lengths, usable time and accommodation are derived from the actual
    dates exactly as beam search does it. ``rooms`` maps a city to the
    accommodation booked there; cities left out of it stay free, which keeps
    V1-era tests focused on transport economics.

    ``cheapest_rooms`` (V4) maps a city to the cheapest room the provider would
    have offered there, which is the baseline the value-for-money diagnostic
    measures a premium against. Left out, the booked room is its own baseline
    and the premium is zero - the same default beam search applies.
    """
    origin = legs[0].origin
    moment = start or datetime.combine(legs[0].departure.date(), time.min)
    state = SearchState(
        origin_airport=origin,
        current_location=origin,
        start_datetime=moment,
        current_datetime=moment,
    ).with_outbound_transfer(outbound_transfer, travelers=travelers)
    for index, option in enumerate(legs):
        is_return = completed and index == len(legs) - 1
        state = state.extend(
            option,
            travelers=travelers,
            is_return=is_return,
            accommodation=(rooms or {}).get(state.current_location),
            cheapest_alternative=(cheapest_rooms or {}).get(state.current_location),
            usable_minutes=(
                0
                if index == 0
                else usable_minutes(state.current_datetime, option.departure)
            ),
            return_transfer=return_transfer if is_return else None,
        )
    return state


def room(
    city: str,
    check_in: date,
    check_out: date,
    price_per_night: float,
    capacity: int = 2,
) -> AccommodationOption:
    """Build one accommodation option without going through a provider."""
    return AccommodationOption(
        id=f"{city}-{check_in:%Y%m%d}-{check_out:%Y%m%d}-{price_per_night:g}",
        city=city,
        name=f"{city} test stay",
        check_in=check_in,
        check_out=check_out,
        price_per_night=price_per_night,
        capacity=capacity,
    )


def transfer(
    origin: str, airport: str, price_per_person: float, minutes: int
) -> GroundTransferOption:
    """Build one ground-transfer option without going through a provider."""
    return GroundTransferOption(
        id=f"{origin}-{airport}",
        origin=origin,
        airport=airport,
        price_per_person=price_per_person,
        duration_minutes=minutes,
    )


def completed_states(planner: TravelPlanner, request: TripRequest):
    """Every complete itinerary the beam search found, before filtering.

    Pareto and diversity legitimately remove itineraries the search *did*
    discover, so tests about discovery must look here rather than at the final
    five.
    """
    from travel_planner.algorithms.beam_search import BeamSearchOptimizer
    from travel_planner.algorithms.scoring import ScoringEngine
    from travel_planner.constraints.validator import ConstraintValidator
    from travel_planner.profiles import get_profile
    from travel_planner.services.return_estimator import CachedReturnEstimator

    candidates = planner.origin_resolver.resolve(request.origin, planner.config)
    airports = [c.code for c in candidates]
    start_dates = request.candidate_start_dates()
    window = planner._window_dates(request)
    transfers = planner._ground_transfers(request.origin, airports)
    cheapest_transfer = min(
        (t.price_per_person for t in transfers.values()), default=0.0
    )
    estimator = CachedReturnEstimator(
        planner.transport,
        origin_airports=airports,
        dates=window,
        allowed_transport_types=[t.value for t in request.transport_preferences],
        min_return_transfer_price_per_person=cheapest_transfer,
    )
    validator = ConstraintValidator(
        planner.config,
        origin_airports=airports,
        destination_ids=[d.id for d in planner.destinations.all()],
        return_estimator=estimator,
        accommodation_estimator=planner.accommodation_estimator,
    )
    optimizer = BeamSearchOptimizer(
        planner.config,
        transport_provider=planner.transport,
        destination_provider=planner.destinations,
        validator=validator,
        scoring=ScoringEngine(planner.config, planner.destinations),
        return_estimator=estimator,
        travel_value=planner.travel_value,
        profile=get_profile(request.profile or planner.config.profile),
        accommodation_provider=planner.accommodation,
        accommodation_estimator=planner.accommodation_estimator,
        ground_transfers=transfers,
    )
    return optimizer.search(
        request, origin_airports=airports, start_dates=start_dates
    )


def trip_request(**overrides) -> TripRequest:
    """A valid request with sensible defaults, overridable per test."""
    payload = dict(
        origin="Köln",
        budget=V2_DEFAULT_BUDGET,
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
def accommodation() -> SyntheticAccommodationDataProvider:
    return SyntheticAccommodationDataProvider()


@pytest.fixture
def ground_transfer() -> SyntheticGroundTransferProvider:
    return SyntheticGroundTransferProvider()


@pytest.fixture
def planner(transport, destinations, config, accommodation, ground_transfer) -> TravelPlanner:
    return TravelPlanner(
        transport,
        destinations,
        config=config,
        accommodation_provider=accommodation,
        ground_transfer_provider=ground_transfer,
    )


@pytest.fixture
def v1_planner(transport, destinations) -> TravelPlanner:
    """A planner with the V2 economics switched off - the V1 model exactly."""
    return TravelPlanner(
        transport,
        destinations,
        config=PlannerConfig(enable_accommodation=False, enable_ground_transfer=False),
        accommodation_provider=NoAccommodationProvider(),
        ground_transfer_provider=FreeGroundTransferProvider(),
    )


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
        # The V1 non-greedy proof is about transport economics only; the
        # accommodation trap has its own fixture below.
        enable_accommodation=False,
        enable_ground_transfer=False,
        profile=ProfileName.CHEAPEST,
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
            accommodation_provider=NoAccommodationProvider(),
            ground_transfer_provider=FreeGroundTransferProvider(),
        ),
        transport=provider,
        destinations=destination_provider,
        config=trap_config,
        request=request,
    )


# ---------------------------------------------------------------------------
# The accommodation trap (spec section 37)
# ---------------------------------------------------------------------------
#: Transport alone says London; the complete trip says Prague.
#:
#: * ``DUS <-> London`` is the cheapest transport in the network (35/pp each
#:   way), but a London room costs 120 a night.
#: * ``DUS <-> Prague`` costs 55/pp each way, yet a Prague room is 40 a night.
#:
#: For one traveler over two nights that is 310 for London against 190 for
#: Prague - so an optimizer that prices only transport picks exactly the wrong
#: trip. Vienna is reachable onward from Prague to give the search a genuine
#: multi-city alternative too.
STAY_TRAP_CONNECTIONS: tuple[Connection, ...] = (
    Connection("DUS", "London", FLIGHT, 35.0, 90, ("08:00",)),
    Connection("London", "DUS", FLIGHT, 35.0, 90, ("08:00",)),
    Connection("DUS", "Prague", FLIGHT, 55.0, 75, ("08:00",)),
    Connection("Prague", "DUS", FLIGHT, 55.0, 75, ("08:00",)),
    Connection("Prague", "Vienna", FLIGHT, 25.0, 90, ("08:00",)),
    Connection("Vienna", "DUS", FLIGHT, 30.0, 95, ("08:00",)),
)

STAY_TRAP_RATES = {"London": 120.0, "Prague": 40.0, "Vienna": 50.0}


@dataclass(frozen=True)
class StayTrapSetup:
    planner: TravelPlanner
    transport: SyntheticTransportDataProvider
    accommodation: SyntheticAccommodationDataProvider
    config: PlannerConfig
    request: TripRequest


@pytest.fixture
def stay_trap() -> StayTrapSetup:
    provider = SyntheticTransportDataProvider(
        STAY_TRAP_CONNECTIONS, price_variation=False
    )
    rooms = SyntheticAccommodationDataProvider(
        STAY_TRAP_RATES, date_variation=False
    )
    destination_provider = StaticDestinationProvider(
        [d for d in DESTINATIONS if d.id in STAY_TRAP_RATES]
    )
    config = PlannerConfig(
        beam_width=20,
        max_cities=2,
        min_city_stay_days=2,
        max_city_stay_days=2,
        max_origin_distance_km=20.0,
        min_duration_utilization=0.0,
        enable_ground_transfer=False,
        # Only the cheapest room, so the arithmetic in the test is exact.
        accommodation_options_per_stay=1,
        profile=ProfileName.CHEAPEST,
    )
    request = TripRequest(
        origin="Düsseldorf",
        budget=400.0,
        travelers=1,
        duration_days=5,
        date_from=WINDOW_FROM,
        date_to=WINDOW_TO,
        date_flexible=False,
        transport_preferences=[FLIGHT],
    )
    return StayTrapSetup(
        planner=TravelPlanner(
            provider,
            destination_provider,
            config=config,
            accommodation_provider=rooms,
            ground_transfer_provider=FreeGroundTransferProvider(),
        ),
        transport=provider,
        accommodation=rooms,
        config=config,
        request=request,
    )


# ---------------------------------------------------------------------------
# The ground-transfer trap (spec section 38)
# ---------------------------------------------------------------------------
#: The cheaper flight leaves from the dearer airport.
#:
#:   DUS: 30 to reach + 35 to fly = 65 one way
#:   CGN:  5 to reach + 45 to fly = 50 one way
#:
#: Transport alone picks DUS; the complete journey picks CGN.
TRANSFER_TRAP_CONNECTIONS: tuple[Connection, ...] = (
    Connection("DUS", "London", FLIGHT, 35.0, 90, ("08:00",)),
    Connection("London", "DUS", FLIGHT, 35.0, 90, ("08:00",)),
    Connection("CGN", "London", FLIGHT, 45.0, 95, ("08:00",)),
    Connection("London", "CGN", FLIGHT, 45.0, 95, ("08:00",)),
)

TRANSFER_TRAP_TABLE = {
    "koln": {
        "CGN": (GroundTransferMode.TRAIN, 5.0, 20),
        "DUS": (GroundTransferMode.TRAIN, 30.0, 55),
    }
}


@dataclass(frozen=True)
class TransferTrapSetup:
    planner: TravelPlanner
    transport: SyntheticTransportDataProvider
    transfers: SyntheticGroundTransferProvider
    config: PlannerConfig
    request: TripRequest


@pytest.fixture
def transfer_trap() -> TransferTrapSetup:
    provider = SyntheticTransportDataProvider(
        TRANSFER_TRAP_CONNECTIONS, price_variation=False
    )
    transfers = SyntheticGroundTransferProvider(TRANSFER_TRAP_TABLE)
    destination_provider = StaticDestinationProvider(
        [d for d in DESTINATIONS if d.id == "London"]
    )
    config = PlannerConfig(
        beam_width=20,
        max_cities=1,
        min_city_stay_days=2,
        max_city_stay_days=2,
        max_origin_distance_km=60.0,
        min_duration_utilization=0.0,
        # Isolate the ground transfer: the hotel is the same either way.
        enable_accommodation=False,
        profile=ProfileName.CHEAPEST,
    )
    request = TripRequest(
        origin="Köln",
        budget=400.0,
        travelers=1,
        duration_days=5,
        date_from=WINDOW_FROM,
        date_to=WINDOW_TO,
        date_flexible=False,
        transport_preferences=[FLIGHT],
    )
    return TransferTrapSetup(
        planner=TravelPlanner(
            provider,
            destination_provider,
            config=config,
            accommodation_provider=NoAccommodationProvider(),
            ground_transfer_provider=transfers,
        ),
        transport=provider,
        transfers=transfers,
        config=config,
        request=request,
    )
