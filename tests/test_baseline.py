"""Baseline round trip and the comparison attached to every recommendation."""

from __future__ import annotations

from datetime import date, datetime

from travel_planner.models.itinerary import BaselineResult
from travel_planner.services.baseline import BaselinePlanner, compare_to_baseline
from travel_planner.services.planner import TravelPlanner

from .conftest import WINDOW_FROM, trip_request

START_DATES = [date(2026, 9, 10), date(2026, 9, 11)]
AIRPORTS = ["CGN", "DUS", "EIN", "FRA"]


def test_baseline_is_a_simple_round_trip_to_the_preferred_destination(
    config, transport, destinations
):
    planner = BaselinePlanner(
        config, transport_provider=transport, destination_provider=destinations
    )
    baseline = planner.compute(
        trip_request(), origin_airports=AIRPORTS, start_dates=START_DATES
    )
    assert baseline is not None
    assert baseline.destination == "Madrid"
    assert len(baseline.legs) == 2
    assert baseline.legs[0].destination == "Madrid"
    assert baseline.legs[1].origin == "Madrid"
    assert baseline.legs[0].origin == baseline.legs[1].destination
    assert baseline.total_cost <= 250
    assert baseline.currency == "EUR"


def test_baseline_uses_the_party_total_not_the_per_person_price(
    config, transport, destinations
):
    planner = BaselinePlanner(
        config, transport_provider=transport, destination_provider=destinations
    )
    solo = planner.compute(
        trip_request(travelers=1, budget=1000),
        origin_airports=AIRPORTS,
        start_dates=START_DATES,
    )
    pair = planner.compute(
        trip_request(travelers=2, budget=1000),
        origin_airports=AIRPORTS,
        start_dates=START_DATES,
    )
    assert pair.total_cost == solo.total_cost * 2


def test_no_preferred_destination_means_no_baseline(config, transport, destinations):
    """The planner must not invent a destination the user never named."""
    planner = BaselinePlanner(
        config, transport_provider=transport, destination_provider=destinations
    )
    assert (
        planner.compute(
            trip_request(preferred_destinations=[]),
            origin_airports=AIRPORTS,
            start_dates=START_DATES,
        )
        is None
    )


def test_unknown_preferred_destination_yields_no_baseline(
    config, transport, destinations
):
    planner = BaselinePlanner(
        config, transport_provider=transport, destination_provider=destinations
    )
    assert (
        planner.compute(
            trip_request(preferred_destinations=["Atlantis"]),
            origin_airports=AIRPORTS,
            start_dates=START_DATES,
        )
        is None
    )


def test_baseline_respects_the_budget(config, transport, destinations):
    planner = BaselinePlanner(
        config, transport_provider=transport, destination_provider=destinations
    )
    assert (
        planner.compute(
            trip_request(budget=20),
            origin_airports=AIRPORTS,
            start_dates=START_DATES,
        )
        is None
    )


def test_planner_still_works_without_a_baseline(transport, destinations, config):
    planner = TravelPlanner(transport, destinations, config=config)
    result = planner.plan(trip_request(preferred_destinations=[]))
    assert result.baseline is None
    assert result.recommendations
    for itinerary in result.recommendations:
        assert itinerary.baseline_comparison is None


def test_comparison_reports_savings_and_extra_cities():
    baseline = BaselineResult(
        destination="Madrid",
        total_cost=200.0,
        duration_days=5.0,
        total_travel_minutes=350,
    )

    class _Itinerary:
        total_cost = 150.0
        cities = ["Prague", "Vienna"]
        total_travel_minutes = 440

    comparison = compare_to_baseline(_Itinerary(), baseline)
    assert comparison.baseline_destination == "Madrid"
    assert comparison.baseline_cost == 200.0
    assert comparison.money_saved == 50.0
    assert comparison.additional_cities == 1
    assert comparison.additional_travel_minutes == 90


def test_comparison_reports_a_negative_saving_when_dearer():
    baseline = BaselineResult(
        destination="Madrid", total_cost=100.0, duration_days=5.0, total_travel_minutes=350
    )

    class _Itinerary:
        total_cost = 150.0
        cities = ["Rome"]
        total_travel_minutes = 300

    comparison = compare_to_baseline(_Itinerary(), baseline)
    assert comparison.money_saved == -50.0
    assert comparison.additional_cities == 0
    assert comparison.additional_travel_minutes == -50


def test_no_baseline_means_no_comparison():
    class _Itinerary:
        total_cost = 150.0
        cities = ["Rome"]
        total_travel_minutes = 300

    assert compare_to_baseline(_Itinerary(), None) is None


def test_every_recommendation_carries_a_comparison(planner, koln_request):
    result = planner.plan(koln_request)
    assert result.baseline is not None
    for itinerary in result.recommendations:
        comparison = itinerary.baseline_comparison
        assert comparison is not None
        assert comparison.baseline_cost == result.baseline.total_cost
        assert comparison.money_saved == round(
            result.baseline.total_cost - itinerary.total_cost, 2
        )
        assert comparison.additional_cities == len(itinerary.cities) - 1


def test_baseline_legs_stay_inside_the_window(config, transport, destinations):
    planner = BaselinePlanner(
        config, transport_provider=transport, destination_provider=destinations
    )
    request = trip_request()
    baseline = planner.compute(
        request, origin_airports=AIRPORTS, start_dates=START_DATES
    )
    for option in baseline.legs:
        assert request.date_from <= option.departure.date() <= request.date_to
        assert request.date_from <= option.arrival.date() <= request.date_to
    assert baseline.legs[0].departure >= datetime.combine(
        WINDOW_FROM, datetime.min.time()
    )
