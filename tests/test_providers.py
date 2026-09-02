"""Providers, synthetic dataset and origin discovery."""

from __future__ import annotations

from datetime import date

import pytest

from detoura.config import PlannerConfig
from detoura.data.destinations import (
    ALL_NODES,
    DESTINATIONS,
    ORIGIN_AIRPORTS,
    canonical_key,
)
from detoura.data.synthetic_transport import CONNECTIONS, NETWORK_END, NETWORK_START
from detoura.models.transport import TransportType
from detoura.providers.transport import (
    RealTransportDataProvider,
    SyntheticTransportDataProvider,
    TransportDataProvider,
)
from detoura.services.origin_resolver import StaticOriginResolver

REQUIRED_NODES = {
    "CGN",
    "DUS",
    "FRA",
    "London",
    "Brussels",
    "Paris",
    "Amsterdam",
    "Prague",
    "Vienna",
    "Madrid",
    "Barcelona",
    "Milan",
    "Rome",
    "Dublin",
    "Copenhagen",
    "Budapest",
    "Berlin",
    "Munich",
    "Zurich",
}


# ---------------------------------------------------------------------------
# Dataset coverage
# ---------------------------------------------------------------------------
def test_the_required_network_nodes_are_present():
    assert REQUIRED_NODES <= set(ALL_NODES)


def test_every_transport_mode_is_represented():
    modes = {connection.transport_type for connection in CONNECTIONS}
    assert modes == set(TransportType)


def test_destination_catalog_is_well_formed():
    ids = [destination.id for destination in DESTINATIONS]
    assert len(ids) == len(set(ids))
    for destination in DESTINATIONS:
        assert destination.recommended_min_days <= destination.recommended_max_days


def test_provider_satisfies_the_protocol(transport):
    assert isinstance(transport, TransportDataProvider)


def test_search_returns_nothing_for_a_missing_connection(transport):
    assert transport.search("Dublin", "Budapest", date(2026, 9, 10)) == []


def test_search_returns_nothing_outside_the_timetable(transport):
    assert transport.search("DUS", "Prague", date(2025, 1, 1)) == []
    assert transport.search("DUS", "Prague", NETWORK_START) != []
    assert transport.search("DUS", "Prague", NETWORK_END) != []


def test_search_is_deterministic_and_cached(transport):
    first = transport.search("DUS", "Prague", date(2026, 9, 10))
    calls = transport.search_calls
    second = transport.search("DUS", "Prague", date(2026, 9, 10))
    assert first == second
    assert transport.search_calls == calls + 1  # counted, but recomputed from cache


def test_multiple_dates_are_covered(transport):
    for day in range(10, 16):
        assert transport.search("DUS", "Prague", date(2026, 9, day))


def test_options_carry_consistent_prices_and_durations(transport):
    for option in transport.search("CGN", "Madrid", date(2026, 9, 10)):
        assert option.price_per_person > 0
        assert option.duration_minutes > 0
        assert option.arrival > option.departure
        assert option.origin == "CGN" and option.destination == "Madrid"


# ---------------------------------------------------------------------------
# The critical dataset property (spec section 9)
# ---------------------------------------------------------------------------
def test_cheapest_first_leg_is_not_the_cheapest_round_trip(transport):
    """DUS -> London is cheap; getting back from London is not.

    This is the property the whole optimizer is built to exploit, so the
    dataset is asserted directly rather than trusted.
    """
    day = date(2026, 9, 10)
    out_london = min(o.price_per_person for o in transport.search("DUS", "London", day))
    out_prague = min(o.price_per_person for o in transport.search("DUS", "Prague", day))
    assert out_london < out_prague

    back = date(2026, 9, 13)
    home_london = min(o.price_per_person for o in transport.search("London", "DUS", back))
    prague_vienna = min(
        o.price_per_person for o in transport.search("Prague", "Vienna", back)
    )
    home_vienna = min(o.price_per_person for o in transport.search("Vienna", "DUS", back))

    assert out_london + home_london > out_prague + prague_vienna + home_vienna


def test_real_provider_is_an_unimplemented_placeholder():
    """The MVP must not pretend to have live data."""
    with pytest.raises(NotImplementedError, match="placeholder"):
        RealTransportDataProvider()


def test_price_variation_can_be_switched_off():
    varying = SyntheticTransportDataProvider()
    fixed = SyntheticTransportDataProvider(price_variation=False)
    day_a, day_b = date(2026, 9, 10), date(2026, 9, 12)
    assert {o.price_per_person for o in varying.search("DUS", "Prague", day_a)} != {
        o.price_per_person for o in varying.search("DUS", "Prague", day_b)
    }
    assert {o.price_per_person for o in fixed.search("DUS", "Prague", day_a)} == {
        o.price_per_person for o in fixed.search("DUS", "Prague", day_b)
    } == {55.0}


# ---------------------------------------------------------------------------
# Destination provider
# ---------------------------------------------------------------------------
def test_destination_lookup_tolerates_spellings(destinations):
    for spelling in ("Vienna", "vienna", "WIEN", "wien "):
        assert destinations.get(spelling).id == "Vienna"
    assert destinations.get("Prag").id == "Prague"
    assert destinations.get("nowhere") is None


def test_destinations_are_returned_in_a_stable_order(destinations):
    assert destinations.ids() == sorted(destinations.ids())
    assert destinations.all() == destinations.all()


# ---------------------------------------------------------------------------
# Origin resolution
# ---------------------------------------------------------------------------
def test_koln_resolves_to_several_departure_airports():
    """The origin is a region, not a single airport."""
    resolver = StaticOriginResolver()
    codes = {
        candidate.code
        for candidate in resolver.resolve("Köln", PlannerConfig(max_origin_airports=5))
    }
    assert {"CGN", "DUS"} <= codes


def test_origin_radius_is_configurable():
    resolver = StaticOriginResolver()
    near = {
        c.code
        for c in resolver.resolve(
            "Köln", PlannerConfig(max_origin_distance_km=50, max_origin_airports=5)
        )
    }
    far = {
        c.code
        for c in resolver.resolve(
            "Köln", PlannerConfig(max_origin_distance_km=300, max_origin_airports=5)
        )
    }
    assert near == {"CGN", "DUS"}
    assert {"CGN", "DUS", "FRA", "EIN", "AMS"} == far


def test_origin_airport_count_is_capped():
    resolver = StaticOriginResolver()
    candidates = resolver.resolve(
        "Köln", PlannerConfig(max_origin_distance_km=500, max_origin_airports=2)
    )
    assert len(candidates) == 2
    assert {c.code for c in candidates} == {"CGN", "DUS"}  # the two nearest


def test_origin_spellings_resolve():
    resolver = StaticOriginResolver()
    for spelling in ("Köln", "Koeln", "cologne", "KÖLN"):
        assert canonical_key(spelling) == "koln"
        assert resolver.resolve(spelling, PlannerConfig())


def test_airport_code_can_be_used_directly():
    resolver = StaticOriginResolver()
    candidates = resolver.resolve("DUS", PlannerConfig())
    assert [c.code for c in candidates] == ["DUS"]


def test_unknown_origin_raises():
    resolver = StaticOriginResolver()
    with pytest.raises(ValueError, match="unknown origin"):
        resolver.resolve("Atlantis", PlannerConfig())


def test_resolution_is_deterministic():
    resolver = StaticOriginResolver()
    config = PlannerConfig()
    assert resolver.resolve("Köln", config) == resolver.resolve("Köln", config)


def test_origin_airports_are_distinct_nodes_from_destination_cities():
    """AMS the airport is not Amsterdam the destination - flying out of
    Schiphol must never count as having visited the city."""
    airport_codes = {airport.code for airport in ORIGIN_AIRPORTS}
    destination_ids = {destination.id for destination in DESTINATIONS}
    assert not airport_codes & destination_ids
