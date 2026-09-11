"""V9 Phase 1/2 — Ops read access to Search Intelligence + privacy of DTOs."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from detoura.api.app import create_app
from detoura.models.search_intel import PriceObservation, SearchModeTag, TripShape
from detoura.persistence import get_db
from detoura.persistence import market_priors as mpp
from detoura.persistence import price_memory as pm
from detoura.services.market_prior_import import run_import
from detoura.services.market_prior_source import FixtureMarketPriorSource


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "si.db"))
    monkeypatch.setenv("DETOURA_OPS_TOKEN", "adm")
    import detoura.persistence.db as _db
    monkeypatch.setattr(_db, "_DB", None)
    from detoura.search_intel_config import reset_search_intel_config
    reset_search_intel_config()
    return TestClient(create_app())


@pytest.fixture
def H(client):
    tok = client.post("/api/v1/ops/session", json={"token": "adm"}).json()["session_token"]
    return {"Authorization": f"Bearer {tok}"}


def _seed(client, n=6):
    db = get_db()
    obs = [
        PriceObservation(
            observation_id=pm.new_observation_id(),
            observed_at=datetime.now(timezone.utc),
            provider="duffel", origin="CGN", destination="BCN",
            departure_date=date(2026, 9, 1), trip_shape=TripShape.ONE_WAY,
            travelers=2, travelers_bucket="2",
            total_amount_minor=24000, per_person_minor=12000, currency="EUR",
            direct=(i % 2 == 0), stops=0, search_id="s1",
            acquisition_call_id=f"s1:{i}", search_mode=SearchModeTag.SMART,
            entered_candidate_set=(i < 4), contributed_to_top_k=(i < 2),
        )
        for i in range(n)
    ]
    pm.record_observations(db, obs)


def test_all_routes_require_ops_auth(client):
    for p in ("/overview", "/markets", "/traces",
              "/market?origin=CGN&destination=BCN&departure_date=2026-09-01"):
        assert client.get(f"/api/v1/ops/search-intel{p}").status_code in (401, 403)


def test_overview_reports_coverage_and_economics(client, H):
    _seed(client)
    r = client.get("/api/v1/ops/search-intel/overview", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["coverage"]["observations"] == 6
    assert body["retention_days"] == 180
    assert body["top_k"] == 5
    # economics unconfigured -> UNKNOWN, not 0
    assert body["economics"]["configured"] is False
    assert body["economics"]["estimated_excess_search_cost"] is None


def test_market_signal_is_flagged_not_a_quote(client, H):
    _seed(client)
    r = client.get(
        "/api/v1/ops/search-intel/market?origin=CGN&destination=BCN"
        "&departure_date=2026-09-01&travelers=2",
        headers=H,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["not_a_quote"] is True
    assert "never a current fare" in body["note"]
    sig = body["signals_by_currency"]["EUR"]
    assert sig["not_a_quote"] is True
    assert sig["sample_count"] == 6
    assert "median_observed" in sig and "current_price" not in sig
    assert "price" not in {k for k in sig if k.endswith("price")}  # only *_observed


def test_markets_weak_ordering(client, H):
    _seed(client, n=6)
    r = client.get("/api/v1/ops/search-intel/markets?order=weak", headers=H)
    assert r.status_code == 200
    assert r.json()["order"] == "weak"


def test_prune_returns_counts(client, H):
    _seed(client)
    r = client.post("/api/v1/ops/search-intel/prune", headers=H)
    assert r.status_code == 200
    assert "observations_pruned" in r.json()


def test_traces_endpoint_never_returns_pii(client, H):
    db = get_db()
    from detoura.models.search_trace import SearchIntelligenceTrace
    tr = SearchIntelligenceTrace(
        search_id="srch_x", started_at=datetime.now(timezone.utc),
        origin="CGN", date_from=date(2026, 9, 1), date_to=date(2026, 9, 14),
        duration_days=5, travelers=2,
    )
    pm.record_trace(db, tr)
    r = client.get("/api/v1/ops/search-intel/traces", headers=H)
    assert r.status_code == 200
    blob = json.dumps(r.json()).lower()
    for bad in ("email", "phone", "passport", "given_name", "born_on"):
        assert bad not in blob
    detail = client.get("/api/v1/ops/search-intel/traces/srch_x", headers=H)
    assert detail.status_code == 200


# ======================================================================
# V9 Phase 2 — catalog + Bootstrap Market Prior Ops observability
# ======================================================================
def test_phase2_routes_require_ops_auth(client):
    for p in ("/catalog", "/market-prior", "/market-prior/imports", "/knowledge"):
        assert client.get(f"/api/v1/ops/search-intel{p}").status_code in (401, 403)
    # noted as a coverage gap in the Phase 2 QA report - closed here.
    assert client.post("/api/v1/ops/search-intel/market-prior/prune").status_code in (401, 403)


def test_catalog_endpoint_reports_expanded_scale(client, H):
    r = client.get("/api/v1/ops/search-intel/catalog", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["catalog_total"] > 150       # far beyond the legacy 16
    assert body["core_network_total"] == 16
    assert body["acquisition_eligible_total"] > 150
    assert body["countries_covered"] >= 20
    assert body["subregions_covered"] >= 8
    assert sum(body["by_subregion"].values()) == body["catalog_total"]


def test_market_prior_endpoint_reports_coverage_and_is_not_a_quote(client, H):
    db = get_db()
    run_import(db, FixtureMarketPriorSource(markets=(("CGN", "BCN"), ("CGN", "FCO"))))
    r = client.get("/api/v1/ops/search-intel/market-prior", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["not_a_quote"] is True
    assert body["coverage"]["rows"] == 12   # 2 markets x 6 horizon buckets
    assert body["coverage"]["markets"] == 2


def test_market_prior_imports_endpoint_lists_recorded_runs(client, H):
    db = get_db()
    run_import(db, FixtureMarketPriorSource(markets=(("CGN", "BCN"),), version="v1"))
    r = client.get("/api/v1/ops/search-intel/market-prior/imports", headers=H)
    assert r.status_code == 200
    imports = r.json()["imports"]
    assert len(imports) == 1
    assert imports[0]["ok"] == 1


def test_market_prior_prune_only_touches_the_prior_table(client, H):
    db = get_db()
    run_import(db, FixtureMarketPriorSource(markets=(("CGN", "BCN"),),
                                            source_date=date(2020, 1, 1)))
    r = client.post("/api/v1/ops/search-intel/market-prior/prune", headers=H)
    assert r.status_code == 200
    assert r.json()["priors_pruned"] == 6
    assert mpp.coverage_summary(db)["rows"] == 0


def test_knowledge_endpoint_aggregates_funnel_trace(client, H):
    db = get_db()
    from detoura.models.search_trace import SearchIntelligenceTrace
    tr = SearchIntelligenceTrace(
        search_id="srch_funnel", started_at=datetime.now(timezone.utc),
        origin="CGN", date_from=date(2026, 9, 1), date_to=date(2026, 9, 14),
        duration_days=5, travelers=2,
        funnel={"catalog_total": 203, "live_history_known_count": 2,
                "prior_known_count": 3, "fully_unknown_count": 3,
                "shortlisted_total": 8, "exploit_candidates": 5,
                "explore_candidates": 3},
    )
    pm.record_trace(db, tr)
    r = client.get("/api/v1/ops/search-intel/knowledge", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["searches_with_funnel_trace"] == 1
    assert body["totals"]["live"] == 2
    assert body["totals"]["prior"] == 3
    assert body["totals"]["unknown"] == 3
    assert body["totals"]["catalog_total"] == 203


def test_knowledge_endpoint_skips_pre_phase2_traces_without_error(client, H):
    db = get_db()
    from detoura.models.search_trace import SearchIntelligenceTrace
    tr = SearchIntelligenceTrace(
        search_id="srch_old", started_at=datetime.now(timezone.utc),
        origin="CGN", date_from=date(2026, 9, 1), date_to=date(2026, 9, 14),
        duration_days=5, travelers=2,
    )  # no `funnel` set -> defaults to {}
    pm.record_trace(db, tr)
    r = client.get("/api/v1/ops/search-intel/knowledge", headers=H)
    assert r.status_code == 200
    assert r.json()["searches_with_funnel_trace"] == 0
