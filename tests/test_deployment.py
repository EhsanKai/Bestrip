"""What the deployment layer has to guarantee.

Serving the built client from the API process is a small amount of code with
two failure modes that are invisible until they are in production:

* an unknown ``/api`` path answering with ``index.html`` and status 200, which
  turns a clean 404 into a JSON parse error in the browser, and
* ``index.html`` being cached, which pins visitors to the previous deploy's
  fingerprinted assets until they hard-refresh.

Both are asserted here. The rest of the file covers the configuration surface
that a deploy actually turns: where the build is, and who may call the API.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.api.app import cors_origins, create_app  # noqa: E402
from detoura.api.static import frontend_dist, mount_frontend  # noqa: E402


@pytest.fixture
def built_client(tmp_path, monkeypatch):
    """An app serving a minimal but structurally real Vite build."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>Detoura</title><div id=root></div>"
    )
    (dist / "assets" / "index-abc123.js").write_text("console.log('detoura')")
    (dist / "favicon.svg").write_text("<svg/>")
    monkeypatch.setenv("DETOURA_FRONTEND_DIST", str(dist))
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Locating the build
# ---------------------------------------------------------------------------
def test_no_build_means_nothing_is_mounted(tmp_path, monkeypatch):
    """A source tree without a build must still serve the API.

    Tests, `uvicorn --reload` and the Vite dev server all run this way.
    """
    monkeypatch.setenv("DETOURA_FRONTEND_DIST", str(tmp_path / "nowhere"))
    assert frontend_dist() is None

    app = create_app()
    assert mount_frontend(app) is False
    assert TestClient(app).get("/api/v1/health").status_code == 200


def test_a_directory_without_an_index_is_not_a_build(tmp_path, monkeypatch):
    """An empty or half-copied directory must not be mistaken for a build."""
    empty = tmp_path / "dist"
    empty.mkdir()
    monkeypatch.setenv("DETOURA_FRONTEND_DIST", str(empty))
    assert frontend_dist() is None


def test_the_env_override_wins(tmp_path, monkeypatch):
    """A container copies the build somewhere of its own choosing."""
    dist = tmp_path / "web"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>")
    monkeypatch.setenv("DETOURA_FRONTEND_DIST", str(dist))
    assert frontend_dist() == dist.resolve()


# ---------------------------------------------------------------------------
# The API keeps winning
# ---------------------------------------------------------------------------
def test_the_api_still_answers_with_the_client_mounted(built_client):
    assert built_client.get("/api/v1/health").status_code == 200
    assert built_client.get("/api/v1/profiles").status_code == 200


def test_an_unknown_api_path_is_json_404_not_the_app_shell(built_client):
    """The regression this whole module is arranged to prevent.

    The web client parses every response body as JSON. If the SPA fallback
    answered here, a 404 would arrive as HTML with status 200 and surface to
    the user as a syntax error rather than as a missing route.
    """
    response = built_client.get("/api/v1/no-such-route")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not Found"}


def test_a_real_api_404_is_untouched(built_client):
    """The product API's own 404s keep their own bodies."""
    assert built_client.get("/api/v1/origins/Atlantis").status_code == 404


def test_the_docs_are_not_swallowed_by_the_fallback(built_client):
    assert built_client.get("/openapi.json").status_code == 200
    assert built_client.get("/docs").status_code == 200


# ---------------------------------------------------------------------------
# Serving the client
# ---------------------------------------------------------------------------
def test_the_root_serves_the_app_shell(built_client):
    response = built_client.get("/")

    assert response.status_code == 200
    assert "<div id=root>" in response.text


def test_an_unknown_app_path_serves_the_shell(built_client):
    """The client holds its own screen state, so any path is the app."""
    response = built_client.get("/trips/anything")

    assert response.status_code == 200
    assert "<div id=root>" in response.text


def test_head_on_the_shell_is_not_a_405(built_client):
    """Uptime monitors and load balancers probe with HEAD.

    FastAPI's ``@app.get`` registers GET alone - bare Starlette would have
    added HEAD - so without asking for it explicitly, every monitor watching
    the site's root would report an outage against a healthy deploy.
    """
    response = built_client.head("/")

    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]


def test_a_real_file_in_the_build_wins_over_the_shell(built_client):
    response = built_client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.text == "<svg/>"


def test_the_shell_is_never_cached(built_client):
    """A cached shell pins the browser to the last deploy's asset names."""
    for path in ("/", "/some/screen"):
        assert "no-store" in built_client.get(path).headers["cache-control"]


def test_fingerprinted_assets_are_cached_hard(built_client):
    response = built_client.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]


def test_the_fallback_cannot_be_walked_out_of(built_client, tmp_path):
    """`../` must not reach outside the build directory."""
    (tmp_path / "secret.txt").write_text("not for the internet")

    response = built_client.get("/../secret.txt")

    assert "not for the internet" not in response.text


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
def test_cors_defaults_to_the_dev_server(monkeypatch):
    monkeypatch.delenv("DETOURA_CORS_ORIGINS", raising=False)
    assert cors_origins() == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_configured_origins_replace_the_defaults(monkeypatch):
    """A deployment that names its origins stops trusting localhost."""
    monkeypatch.setenv(
        "DETOURA_CORS_ORIGINS", "https://detoura.app, https://www.detoura.app"
    )

    assert cors_origins() == ["https://detoura.app", "https://www.detoura.app"]


def test_blank_entries_are_dropped(monkeypatch):
    """Trailing commas are what hand-edited env vars look like."""
    monkeypatch.setenv("DETOURA_CORS_ORIGINS", "https://detoura.app,,")
    assert cors_origins() == ["https://detoura.app"]
