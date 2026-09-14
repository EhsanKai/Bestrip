"""V9 Phase 6 slice 2 — request body size cap.

Unit-level tests drive ``MaxBodySizeMiddleware`` directly at the ASGI layer
(precise control over Content-Length presence/correctness and chunked
delivery); a couple of end-to-end tests confirm the real app still serves
ordinary requests once the middleware is installed - the highest-blast-
radius kind of change, since a bug here affects every route.
"""

from __future__ import annotations

import asyncio

from detoura.api.body_limit import MaxBodySizeMiddleware


async def _echo_app(scope, receive, send):
    """A minimal ASGI app that fully drains the request body (as any real
    handler implicitly does) before responding 200."""
    while True:
        message = await receive()
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _run(coro):
    return asyncio.run(coro)


def _scope(headers: dict[str, str]):
    return {
        "type": "http", "method": "POST", "path": "/x",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }


def test_rejects_via_content_length_before_reading_any_body():
    mw = MaxBodySizeMiddleware(_echo_app, max_bytes=10)
    scope = _scope({"content-length": "1000"})
    reads = {"n": 0}

    async def receive():
        reads["n"] += 1
        return {"type": "http.request", "body": b"x" * 1000, "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    _run(mw(scope, receive, send))
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
    assert reads["n"] == 0, "body must never be read once Content-Length alone proves it's oversized"


def test_rejects_via_streaming_guard_when_content_length_missing():
    """No (or a lying) Content-Length must not bypass the cap - chunks are
    counted as they arrive."""
    mw = MaxBodySizeMiddleware(_echo_app, max_bytes=10)
    scope = _scope({})  # no content-length at all
    chunks = [b"x" * 6, b"x" * 6]  # 12 bytes total, over the 10-byte cap
    state = {"i": 0}

    async def receive():
        i = state["i"]
        state["i"] += 1
        if i < len(chunks):
            return {"type": "http.request", "body": chunks[i], "more_body": i < len(chunks) - 1}
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    _run(mw(scope, receive, send))
    assert sent[0]["status"] == 413


def test_malformed_content_length_falls_back_to_streaming_guard_not_a_crash():
    mw = MaxBodySizeMiddleware(_echo_app, max_bytes=5)
    scope = _scope({"content-length": "not-a-number"})

    async def receive():
        return {"type": "http.request", "body": b"x" * 100, "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    _run(mw(scope, receive, send))
    assert sent[0]["status"] == 413  # still caught, just by the streaming guard


def test_small_body_passes_through_untouched():
    mw = MaxBodySizeMiddleware(_echo_app, max_bytes=1000)
    scope = _scope({"content-length": "2"})

    async def receive():
        return {"type": "http.request", "body": b"ok", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    _run(mw(scope, receive, send))
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"ok"


def test_non_http_scopes_pass_through_unaffected():
    calls = {"n": 0}

    async def app(scope, receive, send):
        calls["n"] += 1

    mw = MaxBodySizeMiddleware(app, max_bytes=10)
    _run(mw({"type": "lifespan"}, None, None))
    assert calls["n"] == 1


# ======================================================================
# End-to-end: the real app still serves ordinary requests
# ======================================================================
def test_real_app_still_serves_get_requests(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import detoura.persistence.db as _db
    from detoura.api.app import create_app

    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "bodylimit.db"))
    monkeypatch.setattr(_db, "_DB", None)
    client = TestClient(create_app())
    r = client.get("/api/v1/health")
    assert r.status_code == 200


def test_real_app_still_serves_ordinary_post_bodies(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import detoura.persistence.db as _db
    from detoura.api.app import create_app

    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "bodylimit2.db"))
    monkeypatch.setattr(_db, "_DB", None)
    client = TestClient(create_app())
    r = client.post("/api/v1/auth/register",
                     json={"email": "a@example.com", "password": "correct horse battery"})
    assert r.status_code == 200


def test_real_app_rejects_an_oversized_post_body(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import detoura.persistence.db as _db
    from detoura.api.app import create_app

    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "bodylimit3.db"))
    monkeypatch.setenv("DETOURA_MAX_REQUEST_BODY_BYTES", "1024")
    monkeypatch.setattr(_db, "_DB", None)
    client = TestClient(create_app())
    huge_password = "p" * (2 * 1024 * 1024)
    r = client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": huge_password})
    assert r.status_code == 413
