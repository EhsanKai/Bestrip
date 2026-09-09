"""V8 Phase 2: bounded real supply, exercised offline.

Every V7.5 guarantee has to survive a real provider being wired in. These run
`acquire_real_supply` through a fake `HttpClient` - no socket - and check that:

* the optimizer is still handed a snapshot, not a provider;
* a city id is translated to an airport for the call and translated back for
  the snapshot, so legs still connect;
* a route that returned more offers than the cap kept is recorded, not hidden;
* a city with no airport is a disclosed gap, not an empty market;
* the injected expiry-aware cache spares a second identical call.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from detoura.data.destinations import DESTINATIONS
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.providers.cache import ExpiringProviderCache
from detoura.providers.duffel import DuffelTransportProvider
from detoura.providers.failures import ProviderFailureKind
from detoura.providers.http import HttpResponse
from detoura.services.acquisition import (
    ProviderCallBudget,
    SnapshotTransportProvider,
    days_for_request,
)
from detoura.services.real_supply import (
    KNOWN_AIRPORT_CODES,
    acquire_real_supply,
    city_airport_table,
    resolve_airport,
)

from . import duffel_fixtures as fx

TOKEN = "duffel_test_notarealtoken"


class _RouteStub:
    """Answers every Offer Request with a fixture, and counts the calls.

    Optionally keyed by ``(origin, destination)`` from the request body so a
    test can give different routes different supply.
    """

    def __init__(self, default: dict, *, by_route: dict[tuple[str, str], dict] | None = None):
        self.calls = 0
        self.routes: list[tuple[str, str]] = []
        self._default = default
        self._by_route = by_route or {}

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls += 1
        payload = json.loads(body)
        sl = payload["data"]["slices"][0]
        route = (sl["origin"], sl["destination"])
        self.routes.append(route)
        return HttpResponse(status=200, body=json.dumps(self._by_route.get(route, self._default)))


def a_request(**kw) -> TripRequest:
    fields = dict(
        origin="Köln", budget=1500.0, travelers=2, duration_days=4,
        date_from=date(2026, 10, 12), date_to=date(2026, 10, 26),
        preferences=TravelPreferences(history=0.9, culture=0.8),
    )
    fields.update(kw)
    return TripRequest(**fields)


def _duffel(stub) -> DuffelTransportProvider:
    return DuffelTransportProvider(access_token=TOKEN, http_client=stub, max_offers=20)


def _small_budget(**kw) -> ProviderCallBudget:
    fields = dict(max_offer_requests=8, max_destinations=2, max_date_variants=1,
                  max_airport_variants=1)
    fields.update(kw)
    return ProviderCallBudget(**fields)


def _days(request):
    return days_for_request(request, request.candidate_start_dates(), max_days=1)


# ---------------------------------------------------------------------------
# Airport resolution
# ---------------------------------------------------------------------------
def test_every_catalog_city_has_an_airport():
    table = city_airport_table(DESTINATIONS)
    assert len(table) == len(DESTINATIONS)
    assert all(len(code) == 3 and code.isupper() for code in table.values())


def test_resolve_airport_passes_codes_through_and_looks_cities_up():
    table = city_airport_table(DESTINATIONS)
    assert resolve_airport("CGN", table) == "CGN"  # a known origin airport
    assert resolve_airport("Berlin", table) == "BER"  # a catalog city
    assert resolve_airport("LIS", table) == "LIS"  # a bare code we don't catalog
    assert resolve_airport("Atlantis", table) is None


def test_origin_airport_codes_are_known():
    assert "CGN" in KNOWN_AIRPORT_CODES and "AMS" in KNOWN_AIRPORT_CODES


# ---------------------------------------------------------------------------
# The acquisition pass
# ---------------------------------------------------------------------------
def test_the_optimizer_is_handed_a_snapshot_with_no_client():
    request = a_request()
    stub = _RouteStub(fx.DIRECT)
    result = acquire_real_supply(
        request, duffel=_duffel(stub), destinations=DESTINATIONS,
        airports=["CGN"], days=_days(request), budget=_small_budget(),
    )
    served = SnapshotTransportProvider(result.snapshot, travelers=request.travelers)
    for attribute in ("http", "host", "_token", "client", "session"):
        assert not hasattr(served, attribute)
    assert stub.calls > 0, "acquisition is the network stage and it did run"


def test_a_city_is_translated_to_an_airport_and_back():
    request = a_request(preferred_destinations=["Berlin"])
    stub = _RouteStub(fx.DIRECT)
    result = acquire_real_supply(
        request, duffel=_duffel(stub), destinations=DESTINATIONS,
        airports=["CGN"], days=_days(request), budget=_small_budget(max_destinations=1),
    )
    # The provider was asked in airport space...
    assert any("BER" in route for route in stub.routes)
    assert not any("Berlin" in route for route in stub.routes)
    # ...but the snapshot is in city space, so the beam can connect legs.
    for edge, options in result.snapshot.offers_by_edge.items():
        assert "BER" not in (edge.origin, edge.destination) or edge.origin == "BER"
        for option in options:
            assert option.origin == edge.origin
            assert option.destination == edge.destination


def test_more_offers_than_the_cap_is_recorded_on_the_snapshot():
    request = a_request(preferred_destinations=["Berlin"])
    many = fx.response([
        fx._offer(f"off_{i}", [
            fx._slice("CGN", "BER", "PT2H10M", [
                fx._segment("CGN", "BER", "2026-10-15T08:00:00",
                            "2026-10-15T10:10:00", "PT2H10M",
                            flight_number=str(2000 + i)),
            ]),
        ], total_amount=f"{90 + i}.00")
        for i in range(23)
    ])
    stub = _RouteStub(many)
    result = acquire_real_supply(
        request, duffel=_duffel(stub), destinations=DESTINATIONS,
        airports=["CGN"], days=_days(request), budget=_small_budget(max_destinations=1),
    )
    assert result.metrics.offers_truncated >= 3
    kinds = {issue.kind for issue in result.snapshot.issues}
    assert ProviderFailureKind.OFFERS_TRUNCATED in kinds
    assert ProviderFailureKind.OFFERS_TRUNCATED.is_infrastructure


def test_a_city_with_no_airport_is_a_disclosed_gap_not_an_empty_market():
    request = a_request()
    stub = _RouteStub(fx.DIRECT)
    # A catalog stripped of one city's airport.
    catalog = [
        d.model_copy(update={"primary_airport": None}) if d.id == "Berlin" else d
        for d in DESTINATIONS
    ]
    result = acquire_real_supply(
        request, duffel=_duffel(stub), destinations=catalog,
        airports=["CGN"], days=_days(request),
        budget=_small_budget(max_destinations=8, max_offer_requests=60),
    )
    if "Berlin" in result.plan.destinations:
        assert "Berlin" in result.unresolved_nodes
        assert result.snapshot.truncated
        assert any(i.kind is ProviderFailureKind.UNAVAILABLE for i in result.snapshot.issues)


def test_the_injected_cache_spares_a_second_identical_call():
    request = a_request(preferred_destinations=["Berlin"])
    stub = _RouteStub(fx.DIRECT)
    cache = ExpiringProviderCache()
    duffel = _duffel(stub)
    common = dict(
        duffel=duffel, destinations=DESTINATIONS, airports=["CGN"],
        days=_days(request), budget=_small_budget(max_destinations=1), cache=cache,
    )
    acquire_real_supply(request, **common)
    first = stub.calls
    acquire_real_supply(request, **common)
    assert stub.calls == first, "the second pass must be served from cache"
    assert cache.stats.hits > 0


def test_provider_calls_stay_inside_the_plan():
    request = a_request()
    for cap in (5, 10, 25):
        stub = _RouteStub(fx.DIRECT)
        result = acquire_real_supply(
            request, duffel=_duffel(stub), destinations=DESTINATIONS,
            airports=["CGN", "DUS"], days=_days(request),
            budget=_small_budget(max_offer_requests=cap, max_destinations=8,
                                 max_airport_variants=2),
        )
        assert result.plan.planned_request_count <= cap
        assert stub.calls <= cap
