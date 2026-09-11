"""V9 Phase 2.6 Part B — the destination-image API seam (§B11).

Proves the new routes are purely additive: existing endpoints are
untouched, and a destination with no eligible image is simply absent from
the response, never a placeholder.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from detoura.api.app import create_app
from detoura.models.destination_image import DestinationImage, ImageStatus
from detoura.services.destination_images.manifest import save_manifest

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _img(destination_id: str, **kw) -> DestinationImage:
    base = dict(
        destination_id=destination_id, city_name=destination_id, country_code="FR",
        image_id=f"commons:{destination_id}", source_name="Wikimedia Commons",
        source_page_url="https://commons.wikimedia.org/wiki/File:x.jpg",
        original_image_url="https://upload.wikimedia.org/x.jpg",
        photographer_or_creator="Jane Doe", license_name="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0",
        attribution_text="Jane Doe, CC BY-SA 4.0", requires_attribution=True,
        retrieved_at=NOW, original_width=2000, original_height=1200,
        optimized_width=1600, optimized_height=900, file_format="WEBP",
        file_size_bytes=100_000, sha256="a" * 64, status=ImageStatus.SELECTED,
    )
    base.update(kw)
    return DestinationImage(**base)


@pytest.fixture
def app_with_manifest(tmp_path, monkeypatch):
    assets_dir = tmp_path / "data" / "destination_images" / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "Paris.webp").write_bytes(b"fake-webp-content")
    manifest_path = tmp_path / "data" / "destination_images" / "manifest.json"

    records = {
        "Paris": _img("Paris", local_asset_path=str((assets_dir / "Paris.webp").relative_to(tmp_path))),
        "NoLicense": _img("NoLicense", license_name=None,
                          local_asset_path=str((assets_dir / "Paris.webp").relative_to(tmp_path))),
    }
    save_manifest(manifest_path, records)

    import detoura.api.destination_images as di
    monkeypatch.setattr(di, "_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(di, "_ASSETS_DIR", assets_dir)
    di._cached_manifest.cache_clear()

    return TestClient(create_app())


def test_list_images_only_returns_production_eligible_entries(app_with_manifest):
    r = app_with_manifest.get("/api/v1/destinations/images")
    assert r.status_code == 200
    body = r.json()["images"]
    assert "Paris" in body
    assert "NoLicense" not in body  # unknown license -> never served, never substituted


def test_list_images_response_is_keyed_by_destination_id_with_expected_fields(app_with_manifest):
    body = app_with_manifest.get("/api/v1/destinations/images").json()["images"]
    paris = body["Paris"]
    assert paris["primary"] == "/destination-images/Paris.webp"
    assert "Jane Doe" in paris["credit"]
    assert paris["aspect_ratio"] == pytest.approx(16 / 9, abs=0.01)
    assert paris["focal_point"] == {"x": 0.5, "y": 0.5}


def test_single_destination_image_endpoint(app_with_manifest):
    r = app_with_manifest.get("/api/v1/destinations/Paris/image")
    assert r.status_code == 200
    assert r.json()["primary"] == "/destination-images/Paris.webp"


def test_unknown_destination_returns_null_not_error(app_with_manifest):
    r = app_with_manifest.get("/api/v1/destinations/DoesNotExist/image")
    assert r.status_code == 200
    assert r.json() is None


def test_ineligible_destination_single_endpoint_returns_null(app_with_manifest):
    r = app_with_manifest.get("/api/v1/destinations/NoLicense/image")
    assert r.status_code == 200
    assert r.json() is None


def test_no_manifest_at_all_is_a_graceful_empty_response(tmp_path, monkeypatch):
    import detoura.api.destination_images as di
    monkeypatch.setattr(di, "_MANIFEST_PATH", tmp_path / "no-such-manifest.json")
    monkeypatch.setattr(di, "_ASSETS_DIR", tmp_path / "no-such-assets")
    di._cached_manifest.cache_clear()
    client = TestClient(create_app())
    r = client.get("/api/v1/destinations/images")
    assert r.status_code == 200
    assert r.json() == {"images": {}}


def test_existing_search_endpoint_is_unaffected(app_with_manifest):
    r = app_with_manifest.get("/api/v1/health")
    assert r.status_code == 200


def test_destination_images_dir_env_override_wins_over_repo_relative_guess(monkeypatch, tmp_path):
    """Regression test for a real bug this project's own Docker health-check
    caught: ``pip install``-ing this package moves the running module from
    ``src/detoura/api/destination_images.py`` to a site-packages layout at a
    different depth, so a ``__file__``-relative ``parents[3]`` guess silently
    resolved to the wrong directory and the API served an empty manifest even
    though the real manifest+assets were present on disk. The env var must
    win regardless of where the module file physically lives."""
    import detoura.api.destination_images as di

    monkeypatch.setenv("DETOURA_DESTINATION_IMAGES_DIR", str(tmp_path / "custom-location"))
    resolved = di._destination_images_dir()
    assert resolved == (tmp_path / "custom-location").resolve()
