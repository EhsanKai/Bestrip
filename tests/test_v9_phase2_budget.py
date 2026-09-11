"""V9 Phase 2 — hard provider-call budget under catalog scale (§24, §25, §26, §36).

The invariant under test: expanding the catalog to ~200 destinations and giving
every one of them a Bootstrap Market Prior must NOT raise the number of real
provider calls a search can make. The funnel picks *which* markets; the
existing :class:`~detoura.services.acquisition.ProviderCallBudget` still binds
*how many* — with or without prior/live knowledge, with multiple date variants,
return legs and inter-city edges all switched on.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from detoura.data.destinations import DESTINATIONS
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.persistence import market_priors as mp
from detoura.persistence.db import Database
from detoura.providers.duffel import DuffelTransportProvider
from detoura.providers.http import HttpResponse
from detoura.search_intel_config import SearchIntelConfig
from detoura.services.acquisition import (
    ProviderCallBudget,
    build_plan,
    days_for_request,
)
from detoura.services.live_search import live_search
from detoura.services.market_prior_source import FixtureMarketPriorSource
from detoura.services.market_prior_import import run_import
from detoura.services.search_intel_recorder import SearchIntelRecorder
from detoura.services.selection_store import SelectionStore
from tests import duffel_fixtures as fx


def _req(**kw) -> TripRequest:
    f = dict(origin="Köln", budget=5000.0, travelers=1, duration_days=4,
             date_from=date(2026, 10, 1), date_to=date(2026, 11, 15),
             preferences=TravelPreferences(history=0.7, culture=0.7))
    f.update(kw)
    return TripRequest(**f)


class _CountingStub:
    """Answers every route with one usable offer; counts real HTTP calls."""

    def __init__(self):
        self.calls = 0

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls += 1
        sl = json.loads(body)["data"]["slices"][0]
        offer = fx._offer(
            f"off_{self.calls:06d}",
            [fx._slice(sl["origin"], sl["destination"], "PT2H", [
                fx._segment(sl["origin"], sl["destination"],
                            "2026-10-14T08:00:00", "2026-10-14T10:00:00",
                            "PT2H", carrier="LH")])],
            total_amount="120.00", total_currency="EUR",
            expires_at="2027-01-01T00:00:00Z",
        )
        return HttpResponse(status=200, body=json.dumps(fx.response([offer])))


def _duffel(stub) -> DuffelTransportProvider:
    return DuffelTransportProvider(access_token="duffel_test_x", http_client=stub, max_offers=20)


def _seed_full_prior(db: Database, dest_airports: list[str], origin="CGN"):
    """A prior row for every single destination airport — the adversarial
    'prior known everywhere' case."""
    markets = tuple((origin, a) for a in dest_airports if a != origin)
    run_import(db, FixtureMarketPriorSource(markets=markets, source_date=date(2026, 5, 1)))


def _big_budget() -> ProviderCallBudget:
    # Deliberately generous per-dimension knobs; the hard ceiling is
    # max_offer_requests, and max_destinations is what actually bounds the
    # inter-city fan-out coming out of the funnel's shortlist.
    return ProviderCallBudget(
        max_offer_requests=100, max_destinations=8,
        max_date_variants=3, max_airport_variants=2, include_inter_city=True,
    )


def _run(db, *, budget, seed_prior: bool) -> tuple[object, _CountingStub]:
    req = _req()
    dest_airports = sorted({d.primary_airport for d in DESTINATIONS if d.primary_airport})
    if seed_prior:
        _seed_full_prior(db, dest_airports)
    rec = SearchIntelRecorder(
        db, req, mode="SMART", provider="duffel",
        provider_call_budget=budget.max_offer_requests, cfg=SearchIntelConfig(),
    )
    stub = _CountingStub()
    days = days_for_request(req, req.candidate_start_dates(), max_days=budget.max_date_variants)
    result = live_search(
        req, duffel=_duffel(stub), selection_store=SelectionStore(),
        destinations=DESTINATIONS,  # the FULL ~200-city catalog, not a hand-picked 8
        airports=["CGN", "DUS"], days=days, budget=budget, recorder=rec,
    )
    return result, stub


# ======================================================================
# §24 / §36 — the hard ceiling holds at ~200-city scale
# ======================================================================
def test_full_catalog_with_prior_everywhere_stays_within_budget():
    assert len(DESTINATIONS) > 150  # sanity: this really is the expanded catalog
    d = Database(":memory:")
    budget = _big_budget()
    result, stub = _run(d, budget=budget, seed_prior=True)
    assert stub.calls <= budget.max_offer_requests
    assert result.search_trace.provider_calls_used <= budget.max_offer_requests


def test_full_catalog_with_zero_prior_and_zero_live_history_stays_within_budget():
    d = Database(":memory:")
    budget = _big_budget()
    result, stub = _run(d, budget=budget, seed_prior=False)
    assert stub.calls <= budget.max_offer_requests
    assert result.search_trace.provider_calls_used <= budget.max_offer_requests


def test_repeated_searches_never_creep_past_the_ceiling():
    # a warmed-up Price Memory (from earlier calls) must not raise the ceiling
    # either — only which markets are chosen changes, never how many.
    d = Database(":memory:")
    budget = _big_budget()
    for _ in range(4):
        _, stub = _run(d, budget=budget, seed_prior=True)
        assert stub.calls <= budget.max_offer_requests


def test_funnel_trace_shortlist_bounded_by_slots_even_over_full_catalog():
    d = Database(":memory:")
    budget = _big_budget()
    result, _ = _run(d, budget=budget, seed_prior=True)
    funnel = result.search_trace.funnel
    assert funnel["catalog_total"] > 150
    assert funnel["shortlisted_total"] <= budget.max_destinations


# ======================================================================
# §25 — no O(catalog^2) inter-city construction
# ======================================================================
def test_build_plan_never_forms_all_pairs_over_the_full_catalog():
    req = _req()
    budget = ProviderCallBudget(
        max_offer_requests=1_000_000,  # deliberately unbounded on this axis
        max_destinations=8, max_date_variants=1, max_airport_variants=1,
        include_inter_city=True,
    )
    plan = build_plan(
        req, destinations=DESTINATIONS, airports=["CGN"],
        days=[date(2026, 10, 14)], budget=budget,
    )
    n = len(DESTINATIONS)
    assert n > 150
    # a naive all-pairs inter-city fan-out over the whole catalog would be
    # ~n*(n-1) ~= 35000+ edges; bounded construction over an 8-city shortlist
    # is at most 8*7 inter-city + 8*2 outbound/return = 72.
    assert plan.planned_request_count < 100
    assert plan.planned_request_count < n * (n - 1) / 100


def test_inter_city_edges_only_connect_chosen_destinations_not_the_catalog():
    req = _req()
    budget = ProviderCallBudget(max_offer_requests=10_000, max_destinations=6,
                                max_date_variants=1, max_airport_variants=1,
                                include_inter_city=True)
    plan = build_plan(req, destinations=DESTINATIONS, airports=["CGN"],
                      days=[date(2026, 10, 14)], budget=budget)
    chosen = set(plan.destinations)
    all_ids = {dst.id for dst in DESTINATIONS}
    assert len(chosen) <= 6
    # every catalog-city endpoint that appears anywhere on the plan is one of
    # the small chosen set — never some other one of the ~200 catalog cities.
    endpoints = {e.origin for e in plan.edges} | {e.destination for e in plan.edges}
    city_endpoints = endpoints & all_ids
    assert city_endpoints <= chosen


# ======================================================================
# §26 — flexible-date bounding; bootstrap horizons are not query dates
# ======================================================================
def test_flexible_wide_date_range_still_bounded_by_max_date_variants():
    req = _req(date_from=date(2026, 10, 1), date_to=date(2027, 3, 1))  # 5 months wide
    budget = ProviderCallBudget(max_offer_requests=100, max_destinations=4,
                                max_date_variants=2, max_airport_variants=1)
    days = days_for_request(req, req.candidate_start_dates(), max_days=budget.max_date_variants)
    plan = build_plan(req, destinations=DESTINATIONS, airports=["CGN"],
                      days=days, budget=budget)
    assert len(plan.days) <= budget.max_date_variants
    assert plan.planned_request_count <= budget.max_offer_requests


def test_bootstrap_horizon_buckets_never_become_query_dates():
    # The prior's horizon buckets (14/30/45/60/90/120 days out) are intelligence
    # only. A plan's actual query days come solely from the caller's `days`.
    d = Database(":memory:")
    dest_airports = ["BCN", "FCO", "MAD"]
    _seed_full_prior(d, dest_airports, origin="CGN")
    req = _req()
    fixed_days = (date(2026, 10, 14),)
    budget = ProviderCallBudget(max_offer_requests=50, max_destinations=3,
                                max_date_variants=1, max_airport_variants=1)
    plan = build_plan(req, destinations=DESTINATIONS, airports=["CGN"],
                      days=fixed_days, budget=budget)
    assert set(plan.days) == set(fixed_days)
    horizon_derived = {date(2026, 10, 14) + timedelta(days=h) for h in (14, 30, 45, 60, 90, 120)}
    assert not (set(plan.days) & horizon_derived - set(fixed_days))
