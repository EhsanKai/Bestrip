"""V3 adversarial scenarios: where greedy logic gets it wrong.

Each fixture is a small, exact network built so that the naive answer and the
right answer differ, and so that the *reason* they differ is a V3 concept -
experience, accommodation quality, intensity or city-count fit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import pytest

from travel_planner.config import PlannerConfig
from travel_planner.data.destinations import DESTINATIONS
from travel_planner.data.synthetic_transport import Connection
from travel_planner.models.transport import TransportType
from travel_planner.models.trip import TravelPreferences, TripRequest
from travel_planner.profiles import ProfileName
from travel_planner.providers.accommodation import SyntheticAccommodationDataProvider
from travel_planner.providers.destinations import StaticDestinationProvider
from travel_planner.providers.ground_transfer import FreeGroundTransferProvider
from travel_planner.providers.transport import SyntheticTransportDataProvider
from travel_planner.services.planner import TravelPlanner

from .conftest import WINDOW_FROM, WINDOW_TO, completed_states, trip_request

FLIGHT = TransportType.FLIGHT


@dataclass(frozen=True)
class Scenario:
    planner: TravelPlanner
    request: TripRequest


def _scenario(
    connections: tuple[Connection, ...],
    rates: dict[str, float],
    *,
    budget: float,
    config: PlannerConfig | None = None,
    **request_overrides,
) -> Scenario:
    cities = sorted(rates)
    planner = TravelPlanner(
        SyntheticTransportDataProvider(connections, price_variation=False),
        StaticDestinationProvider([d for d in DESTINATIONS if d.id in cities]),
        config=config
        or PlannerConfig(
            max_cities=2,
            min_city_stay_days=2,
            max_city_stay_days=2,
            max_origin_distance_km=20.0,
            min_duration_utilization=0.0,
            accommodation_options_per_stay=1,
            enable_ground_transfer=False,
        ),
        accommodation_provider=SyntheticAccommodationDataProvider(
            rates, date_variation=False
        ),
        ground_transfer_provider=FreeGroundTransferProvider(),
    )
    payload = dict(
        origin="Düsseldorf",
        budget=budget,
        travelers=1,
        duration_days=5,
        date_from=WINDOW_FROM,
        date_to=WINDOW_TO,
        date_flexible=False,
        transport_preferences=[FLIGHT],
        preferred_destinations=[],
        avoid_destinations=[],
    )
    payload.update(request_overrides)
    return Scenario(planner=planner, request=TripRequest(**payload))


# ---------------------------------------------------------------------------
# 1. Cheap flight + dear hotel + poor experience loses to the opposite
# ---------------------------------------------------------------------------
#: Zurich is cheap to reach and dear to sleep in, and its profile is nature and
#: adventure. Prague costs more to reach, is cheap to sleep in, and is history
#: and architecture. A traveler who asked for history and architecture must be
#: sent to Prague, and greedy-on-the-first-flight sends them to Zurich.
EXPERIENCE_TRAP = (
    Connection("DUS", "Zurich", FLIGHT, 30.0, 70, ("08:00",)),
    Connection("Zurich", "DUS", FLIGHT, 30.0, 70, ("08:00",)),
    Connection("DUS", "Prague", FLIGHT, 55.0, 75, ("08:00",)),
    Connection("Prague", "DUS", FLIGHT, 55.0, 75, ("08:00",)),
)
EXPERIENCE_RATES = {"Zurich": 95.0, "Prague": 40.0}


@pytest.fixture
def experience_trap() -> Scenario:
    return _scenario(
        EXPERIENCE_TRAP,
        EXPERIENCE_RATES,
        budget=400,
        preferred_experiences=["history", "architecture"],
        preferences=TravelPreferences(multiple_cities=0.0),
    )


def test_the_cheapest_flight_leads_to_the_worse_trip(experience_trap):
    """Precondition: Zurich really is the cheapest way out."""
    day = date(2026, 9, 10)
    transport = experience_trap.planner.transport
    zurich = transport.search("DUS", "Zurich", day)[0].price_per_person
    prague = transport.search("DUS", "Prague", day)[0].price_per_person
    assert zurich == 30.0 < prague == 55.0


def test_experience_and_hotel_together_beat_the_cheap_flight(experience_trap):
    """Route B wins: dearer flight, cheaper bed, far better match."""
    result = experience_trap.planner.plan(
        experience_trap.request, profile=ProfileName.BEST_VALUE
    )
    assert result.recommendations
    best = result.recommendations[0]
    assert best.cities == ["Prague"]

    by_city = {tuple(s.cities): s for s in completed_states(
        experience_trap.planner, experience_trap.request
    )}
    assert ("Zurich",) in by_city, "the cheap option must still be discovered"
    assert by_city[("Zurich",)].transport_cost < best.cost_breakdown.transport
    assert by_city[("Zurich",)].total_cost > best.total_cost


def test_the_win_is_experience_not_only_price(experience_trap):
    """Even judged purely on the traveler, Prague is the better city here."""
    from travel_planner.algorithms.experience import ExperienceEngine

    engine = ExperienceEngine(
        experience_trap.planner.config, experience_trap.planner.destinations
    )
    prague = engine.assess_city("Prague", 2.0, experience_trap.request)
    zurich = engine.assess_city("Zurich", 2.0, experience_trap.request)
    assert prague.preference_match > zurich.preference_match
    assert prague.score > zurich.score
    assert {"history", "architecture"} <= set(prague.strengths)
    assert not set(zurich.strengths) & {"history", "architecture"}


def test_cheapest_still_takes_the_cheap_flight_when_the_beds_are_equal():
    """Isolate the hotel: with equal rates, CHEAPEST goes to Zurich."""
    scenario = _scenario(
        EXPERIENCE_TRAP,
        {"Zurich": 40.0, "Prague": 40.0},
        budget=400,
        preferences=TravelPreferences(multiple_cities=0.0),
    )
    result = scenario.planner.plan(scenario.request, profile=ProfileName.CHEAPEST)
    assert result.recommendations[0].cities == ["Zurich"]


# ---------------------------------------------------------------------------
# 2. Four cheap cities vs two civilised ones
# ---------------------------------------------------------------------------
#: Everything is cheap; what differs is how much of the trip is spent moving.
#: Berlin and Prague are an easy pair; the four-city loop adds Rome and Madrid
#: at the cost of four long flights.
INTENSITY_TRAP = (
    Connection("DUS", "Berlin", FLIGHT, 25.0, 70, ("08:00",)),
    Connection("Berlin", "DUS", FLIGHT, 25.0, 70, ("18:00",)),
    Connection("Berlin", "Prague", FLIGHT, 20.0, 60, ("08:00",)),
    Connection("Prague", "DUS", FLIGHT, 25.0, 75, ("18:00",)),
    Connection("Prague", "Rome", FLIGHT, 20.0, 300, ("08:00",)),
    Connection("Rome", "Madrid", FLIGHT, 20.0, 320, ("08:00",)),
    Connection("Madrid", "DUS", FLIGHT, 25.0, 340, ("08:00",)),
)
INTENSITY_RATES = {
    "Berlin": 40.0, "Prague": 40.0, "Rome": 40.0, "Madrid": 40.0,
}


@pytest.fixture
def intensity_trap() -> Scenario:
    return _scenario(
        INTENSITY_TRAP,
        INTENSITY_RATES,
        budget=600,
        config=PlannerConfig(
            max_cities=4,
            min_city_stay_days=1,
            max_city_stay_days=2,
            max_origin_distance_km=20.0,
            min_duration_utilization=0.0,
            accommodation_options_per_stay=1,
            enable_ground_transfer=False,
        ),
        preferences=TravelPreferences(multiple_cities=0.6),
    )


def test_best_value_prefers_two_civilised_cities_to_four_rushed_ones(intensity_trap):
    result = intensity_trap.planner.plan(
        intensity_trap.request, profile=ProfileName.BEST_VALUE
    )
    assert result.recommendations
    best = result.recommendations[0]
    assert len(best.cities) <= 2
    assert best.travel_intensity < 0.15

    # The four-city loop exists, is cheap, and still loses.
    four_city = [s for s in completed_states(
        intensity_trap.planner, intensity_trap.request
    ) if len(s.cities) == 4]
    assert four_city, "the four-city loop must be discovered"


def test_adventure_leans_further_towards_exploration(intensity_trap):
    """Same network, more appetite for cities - but still not a pure slog."""
    adventure = intensity_trap.planner.plan(
        intensity_trap.request, profile=ProfileName.ADVENTURE
    )
    best_value = intensity_trap.planner.plan(
        intensity_trap.request, profile=ProfileName.BEST_VALUE
    )
    assert adventure.recommendations and best_value.recommendations
    adventure_cities = sum(len(i.cities) for i in adventure.recommendations)
    best_value_cities = sum(len(i.cities) for i in best_value.recommendations)
    assert adventure_cities >= best_value_cities
    for itinerary in adventure.recommendations:
        assert itinerary.total_cost <= intensity_trap.request.budget


# ---------------------------------------------------------------------------
# 3. Admissible bounds must never prune a feasible better answer
# ---------------------------------------------------------------------------
def test_pruning_never_removes_a_reachable_itinerary():
    """The safety property behind every lower bound in the search.

    Run once with pruning bounds active and once with them disabled; anything
    the unpruned search finds and can afford must also be found by the pruned
    one. A bound that overestimates would silently delete good trips.
    """
    from travel_planner.algorithms.beam_search import BeamSearchOptimizer
    from travel_planner.algorithms.scoring import ScoringEngine
    from travel_planner.constraints.validator import ConstraintValidator
    from travel_planner.profiles import get_profile
    from travel_planner.services.accommodation_estimator import (
        CachedAccommodationEstimator,
        ZeroAccommodationEstimator,
    )
    from travel_planner.services.return_estimator import CachedReturnEstimator

    planner = TravelPlanner(config=PlannerConfig(beam_width=60, max_cities=2))
    request = trip_request(budget=420, travelers=2)
    airports = [
        c.code for c in planner.origin_resolver.resolve(request.origin, planner.config)
    ]
    window = planner._window_dates(request)
    transfers = planner._ground_transfers(request.origin, airports)
    cheapest = min((t.price_per_person for t in transfers.values()), default=0.0)

    def run(*, bounded: bool):
        estimator = CachedReturnEstimator(
            planner.transport,
            origin_airports=airports,
            dates=window,
            allowed_transport_types=[t.value for t in request.transport_preferences],
            min_return_transfer_price_per_person=cheapest,
        )
        validator = ConstraintValidator(
            planner.config,
            origin_airports=airports,
            destination_ids=[d.id for d in planner.destinations.all()],
            return_estimator=estimator if bounded else None,
            accommodation_estimator=(
                CachedAccommodationEstimator(planner.accommodation)
                if bounded
                else ZeroAccommodationEstimator()
            ),
        )
        optimizer = BeamSearchOptimizer(
            planner.config,
            transport_provider=planner.transport,
            destination_provider=planner.destinations,
            validator=validator,
            scoring=ScoringEngine(planner.config, planner.destinations),
            return_estimator=estimator,
            travel_value=planner.travel_value,
            profile=get_profile(planner.config.profile),
            accommodation_provider=planner.accommodation,
            accommodation_estimator=CachedAccommodationEstimator(planner.accommodation),
            ground_transfers=transfers,
        )
        return optimizer.search(
            request,
            origin_airports=airports,
            start_dates=request.candidate_start_dates(),
        )

    pruned = {s.signature() for s in run(bounded=True)}
    unpruned = {s.signature() for s in run(bounded=False)}
    assert unpruned, "the control run must find something"
    assert unpruned <= pruned, (
        f"{len(unpruned - pruned)} feasible itineraries were pruned away"
    )


def test_the_accommodation_bound_is_never_an_overestimate(accommodation):
    """The pruning bound must not exceed any bookable stay."""
    from travel_planner.services.accommodation_estimator import (
        CachedAccommodationEstimator,
    )

    estimator = CachedAccommodationEstimator(accommodation)
    for city in ("Prague", "London", "Zurich", "Budapest", "Copenhagen"):
        for travelers in (1, 2, 4):
            for nights in (1, 3):
                check_out = date(2026, 9, 10 + nights)
                bound = estimator.min_stay_cost(city, nights, travelers)
                options = accommodation.search(
                    city, date(2026, 9, 10), check_out, travelers
                )
                assert bound <= min(o.total_price(travelers) for o in options) + 1e-9


def test_the_return_bound_is_never_an_overestimate(transport):
    """No real way home may be cheaper or faster than the bound."""
    from datetime import timedelta

    from travel_planner.services.return_estimator import CachedReturnEstimator

    window = [WINDOW_FROM + timedelta(days=n) for n in range(6)]
    airports = ["CGN", "DUS"]
    estimator = CachedReturnEstimator(
        transport, origin_airports=airports, dates=window
    )
    for city in ("Prague", "Vienna", "London", "Madrid"):
        bound_price = estimator.min_return_price_per_person(city)
        bound_minutes = estimator.min_return_minutes(city)
        for airport in airports:
            for day in window:
                for option in transport.search(city, airport, day):
                    assert bound_price <= option.price_per_person + 1e-9
                    assert bound_minutes <= option.duration_minutes


# ---------------------------------------------------------------------------
# 4. Determinism and the hard constraints, under the V3 model
# ---------------------------------------------------------------------------
def test_the_v3_planner_is_deterministic():
    planner = TravelPlanner()
    request = trip_request(
        budget=600,
        preferred_experiences=["culture", "food"],
        disliked_experiences=["beaches"],
        previously_visited=["Prague"],
    )
    for profile in ProfileName:
        runs = [planner.plan(request, profile=profile) for _ in range(3)]
        assert runs[0].recommendations == runs[1].recommendations == runs[2].recommendations


@pytest.mark.parametrize("profile", list(ProfileName))
def test_v3_preferences_never_break_the_hard_constraints(profile):
    planner = TravelPlanner()
    request = trip_request(
        budget=600,
        must_visit=["Vienna"],
        avoid_destinations=["Paris"],
        preferred_experiences=["nightlife"],
        disliked_experiences=["beaches"],
        preferred_city_count=2,
    )
    result = planner.plan(request, profile=profile)
    assert result.recommendations
    for itinerary in result.recommendations:
        assert itinerary.total_cost <= request.budget
        assert itinerary.duration_days <= request.duration_days
        assert "Vienna" in itinerary.cities
        assert "Paris" not in itinerary.cities
        assert itinerary.route_nodes[-1] in result.metadata.origin_airports
