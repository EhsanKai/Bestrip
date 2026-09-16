"""V9 Search Intelligence Slice 1.5 — wiring the existing Phase 3
recorder/portfolio/attractiveness/diversity/market-prior machinery into the
consumer `/api/v1/search` live-search path (§9/§10/§27/§28).

Two layers of proof, deliberately kept separate:

* **Direct service-level tests** (Groups B-G) - call
  `services.live_search.live_search()` directly with a real, isolated
  in-memory `Database`, exactly matching the established pattern in
  `tests/test_v9_search_recorder.py`/`tests/test_v9_phase3_portfolio.py`.
  These prove the *behavioral* claims (attractiveness/diversity/market-prior
  genuinely change outcomes, not merely "code executed") with full control
  over the fixture.
* **API-level tests** (Group A) - through the real `/api/v1/search` route
  via `TestClient`, with `api.v1.get_db` monkeypatched to an isolated
  in-memory database (never the real `detoura.db` this repo's local dev
  environment happens to have on disk - see
  `docs/V9_SEARCH_INTELLIGENCE_SLICE_1_5_REPORT.md`'s Market Prior Coverage
  Audit for why that file's *contents* must never leak into a test
  assertion). These prove the *consumer endpoint itself* reaches the
  integration seam, not just that `live_search()` supports it.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

import pytest

from detoura.data.destinations import DESTINATIONS
from detoura.models.attractiveness import (
    AttractivenessConfidence,
    AttractivenessProvenance,
    DestinationAttractivenessProfile,
)
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.persistence import attractiveness as attractiveness_store
from detoura.persistence import price_memory as pm
from detoura.persistence.db import Database
from detoura.providers.duffel import DuffelTransportProvider
from detoura.providers.http import HttpResponse
from detoura.search_intel_config import SearchIntelConfig
from detoura.services.acquisition import ProviderCallBudget, days_for_request
from detoura.services.live_search import live_search
from detoura.services.origin_resolver import CatalogOriginResolver
from detoura.services.search_intel_recorder import SearchIntelRecorder
from detoura.services.selection_store import SelectionStore
from tests import duffel_fixtures as fx

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from detoura.api.app import create_app  # noqa: E402
import detoura.api.v1 as v1  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_BY_ID = {d.id: d for d in DESTINATIONS}
# A tight Central European cluster (Prague/Vienna/Budapest, ~250-350km apart)
# plus one geographically distant outlier (Barcelona, ~1500km+ from all
# three) - the same kind of "several strong-but-redundant picks vs one
# diverse one" scenario `tests/test_v9_phase3_portfolio.py` already uses,
# driven this time through the full `live_search(recorder=, portfolio_db=)`
# integration rather than `select_portfolio` directly.
_CLUSTER = [_BY_ID["Prague"], _BY_ID["Vienna"], _BY_ID["Budapest"]]
_OUTLIER = _BY_ID["Barcelona"]
_DIVERSITY_CATALOG = _CLUSTER + [_OUTLIER]


def _req(**kw) -> TripRequest:
    f = dict(origin="Köln", budget=4000.0, travelers=2, duration_days=4,
              date_from=date(2026, 10, 12), date_to=date(2026, 10, 26),
              preferences=TravelPreferences(history=0.9, culture=0.8))
    f.update(kw)
    return TripRequest(**f)


class _PriceByDestinationStub:
    """Offline Duffel HTTP double whose price depends only on the
    destination - lets a test control which destination "looks cheapest"
    without any randomness."""

    def __init__(self, price_by_destination: dict[str, float], default: float = 90.0):
        self.calls = 0
        self.routes: list[tuple[str, str]] = []
        self._prices = price_by_destination
        self._default = default

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls += 1
        sl = json.loads(body)["data"]["slices"][0]
        origin, destination = sl["origin"], sl["destination"]
        self.routes.append((origin, destination))
        price = self._prices.get(destination, self._default)
        # The offer's own departure/arrival must fall on the date actually
        # requested (`sl["departure_date"]`) - a hardcoded date here would
        # make the outbound and return legs mutually inconsistent (e.g. a
        # return "departing" before the outbound "arrives"), which the
        # planner then silently rejects as no valid itinerary rather than
        # erroring - the exact failure mode this fixture design is built to
        # avoid.
        dep_date = sl["departure_date"]
        offer = fx._offer(
            f"off_{self.calls:020d}",
            [fx._slice(origin, destination, "PT2H", [
                fx._segment(origin, destination, f"{dep_date}T08:00:00",
                            f"{dep_date}T10:00:00", "PT2H", carrier="LH")])],
            total_amount=f"{price:.2f}", total_currency="EUR",
            expires_at="2027-01-01T00:00:00Z",
        )
        return HttpResponse(status=200, body=json.dumps(fx.response([offer])))


def _duffel(stub) -> DuffelTransportProvider:
    return DuffelTransportProvider(access_token="duffel_test_x", http_client=stub, max_offers=20)


def _budget(**kw) -> ProviderCallBudget:
    # max_date_variants > 1 (unlike the minimal `test_v9_search_recorder.py`
    # fixture, which deliberately does not need a complete itinerary and
    # guards every recommendation-count assertion accordingly) - a valid
    # round trip needs the outbound and return legs on genuinely different
    # days, which needs more than one date variant available to acquire.
    f = dict(max_offer_requests=80, max_destinations=4, max_date_variants=6, max_airport_variants=1)
    f.update(kw)
    return ProviderCallBudget(**f)


def _days(req):
    return days_for_request(req, req.candidate_start_dates(), max_days=6)


def _seed_uniform_attractiveness(db: Database, catalog: list, *, score: float = 70.0) -> None:
    """Every destination gets an identical profile - isolates diversity's
    own effect from attractiveness's, so a diversity-behavior test is not
    confounded by real per-city attractiveness variation."""
    profiles = [
        DestinationAttractivenessProfile(
            destination_id=d.id, model_version=1,
            sightseeing_score=score, culture_score=score, food_score=score,
            nightlife_score=score, nature_score=score, uniqueness_score=score,
            short_trip_score=score, experience_density_score=score,
            aggregate_score=score, confidence=AttractivenessConfidence.HIGH,
            provenance=AttractivenessProvenance.CURATED, source="test-fixture",
        )
        for d in catalog
    ]
    attractiveness_store.upsert_profiles(db, profiles)


def _run(
    db: Database, *, catalog, stub, cfg=None, budget=None,
    recorder: bool = True, portfolio_db: bool = True, req=None,
    planner_config=None,
):
    request = req or _req()
    budget = budget or _budget()
    cfg = cfg or SearchIntelConfig()
    rec = None
    if recorder:
        rec = SearchIntelRecorder(
            db, request, mode="SMART", provider="duffel",
            provider_call_budget=budget.max_offer_requests, cfg=cfg,
        )
    result = live_search(
        request, duffel=_duffel(stub), selection_store=SelectionStore(),
        destinations=catalog, airports=["CGN"], days=_days(request),
        budget=budget, recorder=rec, portfolio_db=db if portfolio_db else None,
        portfolio_cfg=cfg, origin_resolver=CatalogOriginResolver(),
        config=planner_config,
    )
    return result


# ---------------------------------------------------------------------------
# Group A — the consumer endpoint genuinely reaches the integration seam
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(monkeypatch) -> Database:
    """A fresh, isolated in-memory database, swapped in for `api.v1.get_db`
    for the duration of one test - never the real local `detoura.db`."""
    db = Database(":memory:")
    monkeypatch.setattr(v1, "get_db", lambda: db)
    return db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


_SEARCH_BASE = {
    "budget": 4000, "travelers": 2, "duration_days": 4,
    "date_from": "2026-10-12", "date_to": "2026-10-26", "date_flexible": True,
    "interests": ["culture", "history"],
}


def test_consumer_search_persists_a_trace_through_the_real_endpoint(client, isolated_db, monkeypatch):
    """The strongest available proof that `/api/v1/search` genuinely reaches
    the recorder: after a live search, the isolated database it was given
    has a `search_traces` row - not a mock assertion that a function was
    called, a real read of what got persisted."""
    monkeypatch.setenv("SEARCH_LIVE_ENABLED", "1")
    stub = _PriceByDestinationStub({})
    monkeypatch.setattr(v1, "_search_live_duffel_or_none", lambda: _duffel(stub))

    response = client.post("/api/v1/search", json={**_SEARCH_BASE, "origin": "Düsseldorf"})
    assert response.status_code == 200
    assert response.json()["diagnostics"]["supply_source"] == "LIVE"

    coverage = pm.coverage_summary(isolated_db)
    assert coverage["search_traces"] == 1


def test_consumer_search_persists_price_observations(client, isolated_db, monkeypatch):
    monkeypatch.setenv("SEARCH_LIVE_ENABLED", "1")
    stub = _PriceByDestinationStub({})
    monkeypatch.setattr(v1, "_search_live_duffel_or_none", lambda: _duffel(stub))

    response = client.post("/api/v1/search", json={**_SEARCH_BASE, "origin": "Düsseldorf"})
    assert response.status_code == 200

    coverage = pm.coverage_summary(isolated_db)
    assert coverage["observations"] > 0


def test_no_recorder_persistence_when_live_search_is_disabled(client, isolated_db, monkeypatch):
    """The two-switch fail-closed gate (Slice 1) still holds: with the flag
    off, nothing is persisted at all - the recorder is never even
    constructed."""
    monkeypatch.delenv("SEARCH_LIVE_ENABLED", raising=False)

    response = client.post("/api/v1/search", json={**_SEARCH_BASE, "origin": "Düsseldorf"})
    assert response.status_code == 200
    assert response.json()["diagnostics"]["supply_source"] == "SYNTHETIC"

    coverage = pm.coverage_summary(isolated_db)
    assert coverage["search_traces"] == 0
    assert coverage["observations"] == 0


# ---------------------------------------------------------------------------
# Group B — recorder genuinely active, direct service level
# ---------------------------------------------------------------------------

def test_recorder_writes_a_trace_when_supplied():
    db = Database(":memory:")
    stub = _PriceByDestinationStub({})
    result = _run(db, catalog=DESTINATIONS, stub=stub, recorder=True, portfolio_db=False)
    assert result.search_trace is not None
    cov = pm.coverage_summary(db)
    assert cov["search_traces"] == 1
    assert cov["observations"] > 0


def test_no_trace_when_recorder_omitted():
    db = Database(":memory:")
    stub = _PriceByDestinationStub({})
    result = _run(db, catalog=DESTINATIONS, stub=stub, recorder=False, portfolio_db=False)
    assert result.search_trace is None
    cov = pm.coverage_summary(db)
    assert cov["search_traces"] == 0


# ---------------------------------------------------------------------------
# Group C — attractiveness and diversity are genuinely behavioral
# ---------------------------------------------------------------------------

def test_diversity_reranking_changes_the_final_set_vs_naive_price_sort():
    """The §28 "strong test" pattern: without portfolio, three otherwise-
    similar destinations from the same tight geographic cluster (cheapest)
    should dominate a naive price sort over one pricier, distant outlier.
    With the real Phase 3 portfolio applied (recorder + portfolio_db,
    uniform attractiveness so only geography differs), the distant outlier
    must actually enter the top selection - a real change in *which*
    destinations are recommended, not merely their order."""
    prices = {d.id: 90.0 for d in _CLUSTER}
    prices[_OUTLIER.id] = 95.0  # pricier, but far away
    cfg = SearchIntelConfig(portfolio_size=3)

    db_plain = Database(":memory:")
    _seed_uniform_attractiveness(db_plain, _DIVERSITY_CATALOG)
    stub_plain = _PriceByDestinationStub(prices)
    without_portfolio = _run(
        db_plain, catalog=_DIVERSITY_CATALOG, stub=stub_plain, cfg=cfg,
        recorder=False, portfolio_db=False,
        budget=_budget(max_destinations=len(_DIVERSITY_CATALOG)),
    )
    picked_without = {it.cities[0] for it in without_portfolio.recommendations if len(it.cities) == 1}

    db_portfolio = Database(":memory:")
    _seed_uniform_attractiveness(db_portfolio, _DIVERSITY_CATALOG)
    stub_portfolio = _PriceByDestinationStub(prices)
    with_portfolio = _run(
        db_portfolio, catalog=_DIVERSITY_CATALOG, stub=stub_portfolio, cfg=cfg,
        recorder=True, portfolio_db=True,
        budget=_budget(max_destinations=len(_DIVERSITY_CATALOG)),
    )
    picked_with = {it.cities[0] for it in with_portfolio.recommendations if len(it.cities) == 1}

    # Sanity: naive price sort actually does favour the cluster (otherwise
    # this scenario proves nothing about diversity specifically).
    assert _OUTLIER.id not in picked_without or len(picked_without & {c.id for c in _CLUSTER}) < len(_CLUSTER)
    # The real behavioral claim: diversity reranking pulls the geographically
    # distant, pricier outlier into the final set.
    assert _OUTLIER.id in picked_with


def test_attractiveness_is_persisted_and_flows_into_a_real_portfolio_decision():
    """Proves the actual persistence-to-decision path end to end: a real
    `Database`, real `attractiveness_store.upsert_profiles`/`batch_get_profiles`,
    real `Itinerary` objects from a real `live_search()` run, real
    `candidates_from_itineraries`/`select_portfolio` - with only the
    attractiveness component varied and every other component pinned equal,
    so the outcome is attributable to attractiveness specifically and not to
    the real catalog's own incidental price/fit/quality variation between
    two different real cities (verified the hard way: an earlier version of
    this test picked two real cities and let `live_search()` derive
    `user_fit`/`trip_quality`/price organically, which turned out to vary
    enough between any two real cities tried to decide the outcome on its
    own, unrelated to attractiveness - see git history/PR discussion)."""
    from detoura.services.portfolio import PortfolioCandidate, select_portfolio

    catalog = [_BY_ID["Madrid"], _BY_ID["Rome"]]
    db = Database(":memory:")
    # Madrid scores far higher than Rome - deliberately lopsided, not
    # subtle, so the assertion is not a coin flip.
    attractiveness_store.upsert_profiles(db, [
        DestinationAttractivenessProfile(
            destination_id="Madrid", model_version=1,
            sightseeing_score=95, culture_score=95, food_score=95,
            nightlife_score=95, nature_score=95, uniqueness_score=95,
            short_trip_score=95, experience_density_score=95,
            aggregate_score=95, confidence=AttractivenessConfidence.HIGH,
            provenance=AttractivenessProvenance.CURATED, source="test-fixture",
        ),
        DestinationAttractivenessProfile(
            destination_id="Rome", model_version=1,
            sightseeing_score=10, culture_score=10, food_score=10,
            nightlife_score=10, nature_score=10, uniqueness_score=10,
            short_trip_score=10, experience_density_score=10,
            aggregate_score=10, confidence=AttractivenessConfidence.HIGH,
            provenance=AttractivenessProvenance.CURATED, source="test-fixture",
        ),
    ])

    # Real persisted round trip: exactly what live_search.py itself calls.
    profiles = attractiveness_store.batch_get_profiles(db, ["Madrid", "Rome"], model_version=1)
    assert profiles["Madrid"].aggregate_score == 95.0
    assert profiles["Rome"].aggregate_score == 10.0

    destinations_by_id = {d.id: d for d in catalog}
    candidates = [
        PortfolioCandidate(
            destination_id=dest_id, price_per_person=90.0,
            attractiveness_score=profiles[dest_id].aggregate_score,
            # Every other component pinned identical - isolates
            # attractiveness as the only asymmetric input.
            user_fit=0.5, trip_quality=0.5, market_opportunity=None,
            novelty=None, pre_rank=i,
        )
        for i, dest_id in enumerate(["Madrid", "Rome"])
    ]
    result = select_portfolio(candidates, destinations_by_id, size=1, cfg=SearchIntelConfig())
    assert [d.destination_id for d in result.selected] == ["Madrid"]
    madrid_decision = next(d for d in result.all_decisions if d.destination_id == "Madrid")
    rome_decision = next(d for d in result.all_decisions if d.destination_id == "Rome")
    assert madrid_decision.components["attractiveness"] == pytest.approx(0.95)
    assert rome_decision.components["attractiveness"] == pytest.approx(0.10)
    assert madrid_decision.base_value > rome_decision.base_value


# ---------------------------------------------------------------------------
# Group D — market prior influence + UNKNOWN safety
# ---------------------------------------------------------------------------

def test_unknown_prior_and_attractiveness_do_not_crash_or_distort():
    """The default, honest case (matching this environment's real ~0%
    market-prior coverage - see the Market Prior Coverage Audit): an empty
    database, no seeded attractiveness, no seeded prior. Recorder +
    portfolio must both run without error and produce a sane result."""
    db = Database(":memory:")  # deliberately unseeded
    stub = _PriceByDestinationStub({})
    result = _run(db, catalog=_DIVERSITY_CATALOG, stub=stub, recorder=True, portfolio_db=True)
    assert result.plan_result is not None
    # Neutral, not fabricated: every recommendation still has a real,
    # positive price from the stub - UNKNOWN attractiveness/prior never
    # invents or zeroes a price.
    for it in result.recommendations:
        assert it.total_cost > 0


def test_market_prior_row_does_not_crash_and_is_isolated_from_live_price():
    """A seeded market_priors row for one destination must not leak into
    that destination's actual *price* in the response (prior is acquisition
    intelligence only - never a quote, per models/market_prior.py)."""
    from detoura.models.market_prior import BootstrapMarketPrior, PriorConfidence
    from detoura.persistence import market_priors as mp

    db = Database(":memory:")
    mp.upsert_priors(db, [BootstrapMarketPrior(
        prior_id=f"p{uuid.uuid4().hex[:12]}", source="test-fixture", source_version="1",
        imported_at=datetime.now(timezone.utc), source_date=date(2026, 1, 1),
        origin_airport="CGN", destination_airport="PRG", currency="EUR",
        sample_count=5, median_minor=500,  # a deliberately absurd 5.00 EUR "prior"
        confidence=PriorConfidence.HIGH,
    )])
    prices = {"Prague": 90.0}
    stub = _PriceByDestinationStub(prices)
    result = _run(db, catalog=[_BY_ID["Prague"]], stub=stub, recorder=True, portfolio_db=True,
                   budget=_budget(max_destinations=1))
    assert result.recommendations
    prague = next(it for it in result.recommendations if it.cities == ["Prague"])
    # The live-fetched price (90 EUR total per leg / 2 travelers = 45/pp)
    # must win - and specifically must NOT be anywhere near the absurd
    # seeded prior (5.00 EUR median_minor = 0.05 EUR), proving the prior
    # never leaks into an actual displayed price.
    assert prague.legs[0].price_per_person == pytest.approx(45.0)
    assert prague.legs[0].price_per_person > 1.0  # nowhere near the 0.05 EUR prior


# ---------------------------------------------------------------------------
# Group E — determinism, provider budget, no duplicate acquisition
# ---------------------------------------------------------------------------

def test_repeated_identical_request_is_deterministic():
    prices = {d.id: 90.0 + i for i, d in enumerate(_DIVERSITY_CATALOG)}
    cfg = SearchIntelConfig(portfolio_size=3)

    def run_once():
        db = Database(":memory:")
        _seed_uniform_attractiveness(db, _DIVERSITY_CATALOG)
        stub = _PriceByDestinationStub(prices)
        result = _run(db, catalog=_DIVERSITY_CATALOG, stub=stub, cfg=cfg,
                        recorder=True, portfolio_db=True,
                        budget=_budget(max_destinations=len(_DIVERSITY_CATALOG)))
        return [it.cities for it in result.recommendations]

    assert run_once() == run_once()


def test_provider_call_budget_is_still_bounded_with_recorder_and_portfolio():
    db = Database(":memory:")
    stub = _PriceByDestinationStub({})
    budget = _budget(max_offer_requests=6, max_destinations=len(DESTINATIONS))
    _run(db, catalog=DESTINATIONS, stub=stub, budget=budget, recorder=True, portfolio_db=True)
    assert stub.calls <= budget.max_offer_requests


def test_recorder_and_portfolio_cause_no_duplicate_provider_acquisition():
    """The integration must not cause a second acquisition pass - the
    recorder only *observes* the one real acquisition `real_supply.py`
    already performs (via `wrap_fetch`), it never triggers its own."""
    db_without = Database(":memory:")
    stub_without = _PriceByDestinationStub({})
    _run(db_without, catalog=DESTINATIONS, stub=stub_without, recorder=False, portfolio_db=False,
          budget=_budget(max_destinations=3))

    db_with = Database(":memory:")
    stub_with = _PriceByDestinationStub({})
    _run(db_with, catalog=DESTINATIONS, stub=stub_with, recorder=True, portfolio_db=True,
          budget=_budget(max_destinations=3))

    assert stub_with.calls == stub_without.calls


# ---------------------------------------------------------------------------
# Group F — search modes, LIVE/SYNTHETIC truth, Slice 1 regression
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["QUICK", "SMART", "DEEP"])
def test_search_modes_still_reach_live_search_with_recorder(client, isolated_db, monkeypatch, mode):
    monkeypatch.setenv("SEARCH_LIVE_ENABLED", "1")
    stub = _PriceByDestinationStub({})
    monkeypatch.setattr(v1, "_search_live_duffel_or_none", lambda: _duffel(stub))

    response = client.post(
        "/api/v1/search",
        json={**_SEARCH_BASE, "origin": "Düsseldorf", "search_mode": mode},
    )
    assert response.status_code == 200
    assert response.json()["diagnostics"]["supply_source"] == "LIVE"
    cov = pm.coverage_summary(isolated_db)
    assert cov["search_traces"] == 1


def test_live_response_never_carries_a_synthetic_closest_price(client, isolated_db, monkeypatch):
    """Slice 1 regression: a LIVE-labelled response must never attach a
    synthetic-planner-derived closest_price, even with the recorder now
    active."""
    monkeypatch.setenv("SEARCH_LIVE_ENABLED", "1")

    class _Boom:
        def request(self, *a, **kw):
            raise ConnectionError("simulated network failure")

    monkeypatch.setattr(v1, "_search_live_duffel_or_none", lambda: _duffel(_Boom()))

    response = client.post("/api/v1/search", json={**_SEARCH_BASE, "origin": "Düsseldorf"})
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostics"]["supply_source"] == "LIVE"
    assert body["issues"]
    assert body["no_results"] is None or body["no_results"]["closest_price"] is None


def test_synthetic_fallback_remains_correctly_labeled_when_disabled(client, isolated_db, monkeypatch):
    monkeypatch.delenv("SEARCH_LIVE_ENABLED", raising=False)
    response = client.post("/api/v1/search", json={**_SEARCH_BASE, "origin": "Düsseldorf"})
    assert response.status_code == 200
    assert response.json()["diagnostics"]["supply_source"] == "SYNTHETIC"


def test_non_cologne_origin_still_reaches_live_search_with_recorder(client, isolated_db, monkeypatch):
    """Slice 1 regression, combined with Slice 1.5's new wiring: a
    catalog-resolved non-legacy origin must still work end to end."""
    monkeypatch.setenv("SEARCH_LIVE_ENABLED", "1")
    stub = _PriceByDestinationStub({})
    monkeypatch.setattr(v1, "_search_live_duffel_or_none", lambda: _duffel(stub))

    response = client.post("/api/v1/search", json={**_SEARCH_BASE, "origin": "Dublin"})
    assert response.status_code == 200
    assert "DUB" in response.json()["origin_airports"]


def test_typo_origin_still_rejected_not_silently_resolved(client, isolated_db, monkeypatch):
    monkeypatch.setenv("SEARCH_LIVE_ENABLED", "1")
    response = client.post("/api/v1/search", json={**_SEARCH_BASE, "origin": "Dusseldrof"})
    assert response.status_code == 422
