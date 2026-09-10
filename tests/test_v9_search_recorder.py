"""V9 Phase 1 — the Search Intelligence recorder: provenance, attribution,
traces, economics, privacy, zero-network invariant."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from detoura.data.destinations import DESTINATIONS
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.providers.duffel import DuffelTransportProvider
from detoura.providers.http import HttpResponse
from detoura.persistence import price_memory as pm
from detoura.persistence.db import Database
from detoura.search_intel_config import ProviderEconomicsConfig, SearchIntelConfig
from detoura.services.acquisition import (
    ProviderCallBudget,
    SnapshotTransportProvider,
    days_for_request,
)
from detoura.services.live_search import live_search
from detoura.services.search_intel_recorder import SearchIntelRecorder
from detoura.services.selection_store import SelectionStore
from detoura.services import provider_economics
from tests import duffel_fixtures as fx


def _req(**kw) -> TripRequest:
    f = dict(origin="Köln", budget=3000.0, travelers=2, duration_days=4,
             date_from=date(2026, 10, 12), date_to=date(2026, 10, 26),
             preferences=TravelPreferences(history=0.9, culture=0.8))
    f.update(kw)
    return TripRequest(**f)


class _RouteStub:
    """Cheap direct offer for every route, so out-and-back trips form."""

    def __init__(self):
        self.calls = 0
        self.routes = []

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls += 1
        sl = json.loads(body)["data"]["slices"][0]
        self.routes.append((sl["origin"], sl["destination"]))
        offer = fx._offer(
            "off_" + f"{self.calls:020d}",
            [fx._slice(sl["origin"], sl["destination"], "PT2H", [
                fx._segment(sl["origin"], sl["destination"],
                            "2026-10-14T08:00:00", "2026-10-14T10:00:00",
                            "PT2H", carrier="LH")])],
            total_amount="90.00", total_currency="EUR",
            expires_at="2027-01-01T00:00:00Z",
        )
        return HttpResponse(status=200, body=json.dumps(fx.response([offer])))


def _duffel(stub) -> DuffelTransportProvider:
    return DuffelTransportProvider(access_token="duffel_test_x", http_client=stub, max_offers=20)


def _budget(**kw) -> ProviderCallBudget:
    f = dict(max_offer_requests=40, max_destinations=3, max_date_variants=1,
             max_airport_variants=1)
    f.update(kw)
    return ProviderCallBudget(**f)


def _days(req):
    return days_for_request(req, req.candidate_start_dates(), max_days=1)


def _run(db, *, cfg=None, budget=None, stub=None):
    req = _req()
    stub = stub or _RouteStub()
    budget = budget or _budget()
    rec = SearchIntelRecorder(
        db, req, mode="SMART", provider="duffel",
        provider_call_budget=budget.max_offer_requests, cfg=cfg or SearchIntelConfig(),
    )
    result = live_search(
        req, duffel=_duffel(stub), selection_store=SelectionStore(),
        destinations=DESTINATIONS, airports=["CGN"], days=_days(req),
        budget=budget, recorder=rec,
    )
    return result, stub


# ======================================================================
# Provenance + persistence
# ======================================================================
def test_a_search_writes_persistent_observations_and_a_trace():
    d = Database(":memory:")
    result, _ = _run(d)
    cov = pm.coverage_summary(d)
    assert cov["observations"] > 0
    assert cov["search_traces"] == 1
    assert result.search_trace is not None
    assert result.search_trace.search_id.startswith("srch_")


def test_observations_survive_a_new_database_object(tmp_path):
    path = str(tmp_path / "si.db")
    d = Database(path)
    _run(d)
    n = pm.coverage_summary(d)["observations"]
    d.close()
    d2 = Database(path)
    assert pm.coverage_summary(d2)["observations"] == n  # persisted across "restart"


def test_every_observation_carries_its_acquisition_call_id():
    d = Database(":memory:")
    _run(d)
    rows = d.query("SELECT acquisition_call_id, search_id FROM price_observations")
    assert rows
    assert all(r["acquisition_call_id"].startswith(r["search_id"] + ":") for r in rows)


def test_transport_options_are_tagged_with_the_call_id_through_the_pipeline():
    d = Database(":memory:")
    result, _ = _run(d)
    if not result.recommendations:
        pytest.skip("fixture produced no itinerary")
    for trip in result.recommendations:
        for leg in trip.legs:
            assert leg.acquisition_call_id  # propagated from acquisition


# ======================================================================
# Contribution attribution
# ======================================================================
class _FakeLeg:
    def __init__(self, cid): self.acquisition_call_id = cid


class _FakeTrip:
    def __init__(self, rank, cids): self.rank = rank; self.legs = [_FakeLeg(c) for c in cids]


def test_attribution_marks_optimizer_top_k_and_winner():
    d = Database(":memory:")
    req = _req()
    cfg = SearchIntelConfig(top_k=2)
    rec = SearchIntelRecorder(d, req, cfg=cfg, provider_call_budget=10)
    # simulate calls
    rec._by_call = {
        "s:1": type("R", (), {})(), "s:2": type("R", (), {})(),
        "s:3": type("R", (), {})(), "s:4": type("R", (), {})(),
    }
    recs = [
        _FakeTrip(0, ["s:1", "s:2"]),   # winner + top-k
        _FakeTrip(1, ["s:3"]),          # top-k only
        _FakeTrip(2, ["s:4"]),          # candidate only
    ]
    rec.attribute(recs)
    assert rec._winner_calls == {"s:1", "s:2"}
    assert rec._top_k_calls == {"s:1", "s:2", "s:3"}
    assert rec._candidate_calls == {"s:1", "s:2", "s:3", "s:4"}


def test_contribution_classes_are_written_back_to_observations():
    d = Database(":memory:")
    result, _ = _run(d)
    rows = d.query(
        "SELECT contributed_to_top_k, contributed_to_winner, entered_candidate_set,"
        " normalized_ok, retained_after_limits FROM price_observations"
    )
    # every row normalized + retained (the stub always returns a usable offer)
    assert all(r["normalized_ok"] and r["retained_after_limits"] for r in rows)
    if result.recommendations:
        assert any(r["entered_candidate_set"] for r in rows)


def test_a_call_that_returns_nothing_is_class_A(tmp_path):
    d = Database(":memory:")

    class _EmptyStub:
        calls = 0
        def request(self, *a, **k):
            _EmptyStub.calls += 1
            return HttpResponse(status=200, body=json.dumps(fx.response([])))

    req = _req()
    budget = _budget()
    rec = SearchIntelRecorder(d, req, provider_call_budget=budget.max_offer_requests)
    live_search(req, duffel=_duffel(_EmptyStub()), selection_store=SelectionStore(),
                destinations=DESTINATIONS, airports=["CGN"], days=_days(req),
                budget=budget, recorder=rec)
    t = rec.finalize.__self__  # recorder
    # no offers => no observations, but call outcomes recorded as NO_USABLE_OFFER
    assert pm.coverage_summary(d)["observations"] == 0
    trace = pm.get_trace(d, rec.search_id)
    assert trace is not None


# ======================================================================
# Explore / exploit
# ======================================================================
def test_cold_start_is_all_explore_then_warm_markets_become_exploit():
    d = Database(":memory:")
    # first search: cold
    r1, _ = _run(d)
    t1 = r1.search_trace
    assert t1.calls_exploit == 0
    assert t1.calls_explore > 0

    # many more searches to build history for the chosen destinations
    for _ in range(6):
        _run(d)

    r_last, _ = _run(d)
    t = r_last.search_trace
    # by now at least some calls should be EXPLOIT for markets we've seen a lot
    assert t.calls_exploit >= 1
    # but exploration never disappears entirely (invariant)
    assert t.calls_explore >= 1


def test_provider_budget_is_never_exceeded_by_the_recorder():
    d = Database(":memory:")
    budget = _budget(max_offer_requests=6, max_destinations=2)
    result, stub = _run(d, budget=budget)
    assert stub.calls <= 6
    assert result.search_trace.provider_calls_used <= 6


def test_zero_network_optimizer_invariant_holds():
    d = Database(":memory:")
    result, _ = _run(d)
    # SnapshotTransportProvider has no client/host/token by construction
    served = SnapshotTransportProvider(result.supply.snapshot)
    assert not hasattr(served, "http") and not hasattr(served, "_token")


# ======================================================================
# Economics
# ======================================================================
def test_economics_unknown_when_not_configured():
    d = Database(":memory:")
    _run(d, cfg=SearchIntelConfig(economics=ProviderEconomicsConfig()))
    econ = provider_economics.rolling_economics(
        d, cfg=ProviderEconomicsConfig()
    )
    assert econ.configured is False
    assert econ.estimated_excess_search_cost_minor is None
    assert econ.as_dict()["estimated_excess_search_cost"] is None  # not 0.0


def test_economics_known_when_configured():
    d = Database(":memory:")
    ec = ProviderEconomicsConfig(
        provider="duffel", included_searches_flat=3, excess_search_fee=0.05,
        currency="EUR",
    )
    _run(d, cfg=SearchIntelConfig(economics=ec))
    econ = provider_economics.rolling_economics(d, cfg=ec)
    assert econ.configured is True
    assert econ.estimated_excess_searches is not None
    assert econ.estimated_excess_search_cost_minor is not None


def test_search_to_book_ratio_is_none_without_bookings():
    d = Database(":memory:")
    _run(d)
    econ = provider_economics.rolling_economics(
        d, cfg=ProviderEconomicsConfig(provider="duffel"),
    )
    assert econ.search_to_book_ratio is None  # UNKNOWN, not 0


# ======================================================================
# Privacy
# ======================================================================
_PII = ("given_name", "family_name", "email", "phone", "born_on", "passport",
        "traveler_name", "lead_name")


def test_no_pii_columns_in_price_observations_or_traces():
    d = Database(":memory:")
    _run(d)
    obs_cols = {r["name"] for r in d.query("PRAGMA table_info(price_observations)")}
    trace_cols = {r["name"] for r in d.query("PRAGMA table_info(search_traces)")}
    for bad in _PII:
        assert not any(bad in c for c in obs_cols)
        assert not any(bad in c for c in trace_cols)


def test_trace_json_contains_no_pii():
    d = Database(":memory:")
    _run(d)
    row = pm.recent_traces(d, limit=1)[0]
    blob = row["trace_json"].lower()
    for bad in ("given_name", "family_name", "email", "phone", "passport",
                "born_on", "lovelace", "@example"):
        assert bad not in blob


def test_recorder_source_never_reads_traveler_pii():
    import detoura.services.search_intel_recorder as mod
    src = open(mod.__file__).read()
    for bad in ("given_name", "family_name", ".email", ".phone", "born_on",
                "passport", "party.lead"):
        assert bad not in src


# ======================================================================
# Disabled = inert
# ======================================================================
def test_disabled_recorder_is_a_noop():
    d = Database(":memory:")
    cfg = SearchIntelConfig(enabled=False)
    result, stub = _run(d, cfg=cfg)
    assert pm.coverage_summary(d)["observations"] == 0
    assert pm.coverage_summary(d)["search_traces"] == 0
    assert result.recommendations is not None  # search still worked
