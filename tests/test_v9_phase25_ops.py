"""V9 Phase 2.5 — Ops-authenticated Market-Prior Acquisition API (§35, §59)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from detoura.api.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "acq.db"))
    monkeypatch.setenv("DETOURA_OPS_TOKEN", "adm")
    import detoura.persistence.db as _db
    monkeypatch.setattr(_db, "_DB", None)
    from detoura.services import bootstrap_registry
    bootstrap_registry.reset_registry()
    bootstrap_registry.register_fetcher_factory(
        bootstrap_registry.DEFAULT_FIXTURE_SOURCE_ID,
        lambda reg, db, job_id: __import__(
            "detoura.services.bootstrap_fetchers", fromlist=["FixtureSourceFetcher"]
        ).FixtureSourceFetcher(),
    )
    return TestClient(create_app())


@pytest.fixture
def H(client):
    tok = client.post("/api/v1/ops/session", json={"token": "adm"}).json()["session_token"]
    return {"Authorization": f"Bearer {tok}"}


PREFIX = "/api/v1/ops/acquisition"


def test_every_route_requires_ops_auth(client):
    assert client.get(f"{PREFIX}/sources").status_code in (401, 403)
    assert client.get(f"{PREFIX}/sources/x").status_code in (401, 403)
    assert client.post(f"{PREFIX}/sources", json={}).status_code in (401, 403)
    assert client.post(f"{PREFIX}/sources/x/authorization", json={}).status_code in (401, 403)
    assert client.get(f"{PREFIX}/sources/x/health").status_code in (401, 403)
    assert client.post(f"{PREFIX}/jobs/dry-run", json={}).status_code in (401, 403)
    assert client.post(f"{PREFIX}/jobs", json={}).status_code in (401, 403)
    assert client.get(f"{PREFIX}/jobs").status_code in (401, 403)
    assert client.get(f"{PREFIX}/jobs/x").status_code in (401, 403)
    assert client.get(f"{PREFIX}/jobs/x/tasks").status_code in (401, 403)
    assert client.post(f"{PREFIX}/jobs/x/start").status_code in (401, 403)
    assert client.post(f"{PREFIX}/jobs/x/resume").status_code in (401, 403)
    assert client.post(f"{PREFIX}/jobs/x/pause").status_code in (401, 403)
    assert client.post(f"{PREFIX}/jobs/x/cancel").status_code in (401, 403)


def test_default_fixture_source_is_seeded_and_approved(client, H):
    r = client.get(f"{PREFIX}/sources", headers=H)
    assert r.status_code == 200
    ids = {s["source_id"] for s in r.json()["sources"]}
    assert "fixture-europe-demo" in ids
    demo = next(s for s in r.json()["sources"] if s["source_id"] == "fixture-europe-demo")
    assert demo["authorization_status"] == "APPROVED"
    assert demo["has_fetcher_configured"] is True


def test_get_missing_source_is_404(client, H):
    assert client.get(f"{PREFIX}/sources/does-not-exist", headers=H).status_code == 404


def _body(**kw):
    base = dict(source_id="fixture-europe-demo", origins=["LHR", "CDG"],
                destinations=["BCN", "MAD", "FCO"], horizon_days=[30, 60], request_budget=10)
    base.update(kw)
    return base


def test_dry_run_makes_no_job_and_no_priors(client, H):
    r = client.post(f"{PREFIX}/jobs/dry-run", json=_body(), headers=H)
    assert r.status_code == 200
    assert r.json()["network_requests_made"] == 0
    assert client.get(f"{PREFIX}/jobs", headers=H).json()["jobs"] == []


def test_dry_run_unknown_source_is_404(client, H):
    r = client.post(f"{PREFIX}/jobs/dry-run", json=_body(source_id="ghost"), headers=H)
    assert r.status_code == 404


def test_full_job_lifecycle(client, H):
    # 2 origins x 3 destinations x 2 horizons = 12 cells; budget 20 covers all.
    r = client.post(f"{PREFIX}/jobs", json=_body(request_budget=20), headers=H)
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["status"] == "PLANNED"
    assert r.json()["plan"]["planned"] == 12
    job_id = job["job_id"]

    r = client.post(f"{PREFIX}/jobs/{job_id}/start", json={"max_tasks": 100}, headers=H)
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["succeeded"] == 12
    assert r.json()["job"]["status"] == "COMPLETED"

    r = client.get(f"{PREFIX}/jobs/{job_id}", headers=H)
    assert r.json()["counters"]["remaining"] == 0

    r = client.get(f"{PREFIX}/jobs/{job_id}/tasks", headers=H)
    assert len(r.json()["tasks"]) == 12


def test_start_unknown_job_is_404(client, H):
    assert client.post(f"{PREFIX}/jobs/ghost/start", headers=H).status_code == 404


def test_pause_and_cancel_job(client, H):
    job_id = client.post(f"{PREFIX}/jobs", json=_body(), headers=H).json()["job"]["job_id"]
    r = client.post(f"{PREFIX}/jobs/{job_id}/pause", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "PAUSED"
    r = client.post(f"{PREFIX}/jobs/{job_id}/cancel", headers=H)
    assert r.status_code == 200
    assert r.json()["job"]["status"] == "CANCELLED"
    assert r.json()["pending_tasks_cancelled"] > 0


def test_starting_a_job_on_an_unwired_source_fails_closed(client, H):
    reg_body = {"source_id": "unwired", "source_name": "Unwired", "source_type": "AUTHORIZED_WEB_SOURCE",
                "base_domain": "example.com"}
    client.post(f"{PREFIX}/sources", json=reg_body, headers=H)
    client.post(f"{PREFIX}/sources/unwired/authorization", json={"status": "APPROVED"}, headers=H)
    job = client.post(f"{PREFIX}/jobs", json=_body(source_id="unwired"), headers=H).json()["job"]
    r = client.post(f"{PREFIX}/jobs/{job['job_id']}/start", headers=H)
    assert r.status_code == 409


def test_registering_a_source_never_auto_approves_it(client, H):
    reg_body = {"source_id": "newsrc", "source_name": "New", "source_type": "AUTHORIZED_WEB_SOURCE",
                "base_domain": "example.com"}
    r = client.post(f"{PREFIX}/sources", json=reg_body, headers=H)
    assert r.status_code == 200
    assert r.json()["authorization_status"] == "REVIEW_REQUIRED"
    assert r.json()["network_allowed"] is False


def test_authorization_change_is_reflected_in_health(client, H):
    reg_body = {"source_id": "newsrc2", "source_name": "New2", "source_type": "AUTHORIZED_WEB_SOURCE",
                "base_domain": "example.com"}
    client.post(f"{PREFIX}/sources", json=reg_body, headers=H)
    r = client.get(f"{PREFIX}/sources/newsrc2/health", headers=H)
    assert r.json()["health"] == "REVIEW_REQUIRED"
    client.post(f"{PREFIX}/sources/newsrc2/authorization", json={"status": "PROHIBITED"}, headers=H)
    r = client.get(f"{PREFIX}/sources/newsrc2/health", headers=H)
    assert r.json()["health"] == "BLOCKED"
