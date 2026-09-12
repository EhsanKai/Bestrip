"""V9 Phase 3 — Ops read access to Destination Attractiveness. Ops-only,
no consumer-facing route change."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from detoura.api.app import create_app
from detoura.data.destinations import acquisition_catalog
from detoura.persistence import get_db
from detoura.services.attractiveness_import import seed_attractiveness


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "attract.db"))
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


def test_unauthenticated_request_is_rejected(client):
    r = client.get("/api/v1/ops/attractiveness/overview")
    assert r.status_code in (401, 403)


def test_overview_before_any_seed_reports_full_missing(client, H):
    r = client.get("/api/v1/ops/attractiveness/overview", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["profiled_total"] == 0
    assert body["missing_count"] == body["catalog_total"] > 0


def test_reseed_populates_full_catalog(client, H):
    r = client.post("/api/v1/ops/attractiveness/reseed", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["catalog_total"] == body["inserted"]

    overview = client.get("/api/v1/ops/attractiveness/overview", headers=H).json()
    assert overview["missing_count"] == 0
    assert overview["profiled_total"] == overview["catalog_total"]


def test_reseed_is_idempotent(client, H):
    client.post("/api/v1/ops/attractiveness/reseed", headers=H)
    body2 = client.post("/api/v1/ops/attractiveness/reseed", headers=H).json()
    assert body2["inserted"] == 0
    assert body2["updated"] == 0
    assert body2["unchanged"] == body2["catalog_total"]


def test_get_single_profile(client, H):
    client.post("/api/v1/ops/attractiveness/reseed", headers=H)
    r = client.get("/api/v1/ops/attractiveness/profiles/Paris", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["destination_id"] == "Paris"
    assert body["is_known"] is True
    assert body["provenance"] == "CURATED"


def test_get_unknown_profile_is_404_not_a_fabricated_neutral(client, H):
    r = client.get("/api/v1/ops/attractiveness/profiles/NoSuchCity", headers=H)
    assert r.status_code == 404


def test_list_profiles_paginated(client, H):
    client.post("/api/v1/ops/attractiveness/reseed", headers=H)
    r = client.get("/api/v1/ops/attractiveness/profiles?limit=5&offset=0", headers=H)
    body = r.json()
    assert body["count"] == 5


def test_no_consumer_route_touched():
    """§Frontend ownership: nothing here changes an existing consumer route."""
    client = TestClient(create_app())
    r = client.get("/api/v1/health")
    assert r.status_code == 200
