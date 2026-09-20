"""Request correlation + structured request logging.

Pure ASGI, not Starlette's ``BaseHTTPMiddleware`` - the same reasoning as
``api/body_limit.py``: this project already avoids ``BaseHTTPMiddleware``
for its extra buffering, and there is no reason to reintroduce it here.

Installed outermost in ``api/app.py`` (added first, so Starlette - which
runs the most-recently-added middleware first - reaches it last on the way
in and first on the way out), so a correlation id and the request-completed
log line cover every route, including one that raises before FastAPI's own
routing ever sees it.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import metrics
from .logging import bind_request_id, log_event, reset_request_id, sanitize_request_id

logger = logging.getLogger("detoura.request")

REQUEST_ID_HEADER = b"x-request-id"


class CorrelationMiddleware:
    """Assigns/validates a request id, times the request, and logs one
    ``request_completed`` (or ``request_failed``) structured event per call -
    the one place every route gets this for free, so individual endpoints
    do not each need their own timing/logging boilerplate."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming: str | None = None
        for name, value in scope.get("headers") or []:
            if name == REQUEST_ID_HEADER:
                incoming = value.decode("latin-1", errors="replace")
                break
        request_id = sanitize_request_id(incoming)
        token = bind_request_id(request_id)

        status_holder = {"status": 500}

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers") or [])
                headers.append((REQUEST_ID_HEADER, request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        method = scope.get("method", "")
        route = scope.get("path", "")
        start = time.monotonic()
        try:
            await self.app(scope, receive, _send)
        except Exception:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            log_event(
                logger, "request_failed", level=logging.ERROR,
                method=method, route=route, duration_ms=duration_ms,
            )
            raise
        else:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            status = status_holder["status"]
            log_event(
                logger, "request_completed",
                method=method, route=route, status_code=status, duration_ms=duration_ms,
            )
            metrics.observe_http_request(method=method, status=status, duration_ms=duration_ms)
        finally:
            reset_request_id(token)
