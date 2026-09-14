"""Request body size cap (V9 Phase 6 slice 2).

FastAPI/Starlette buffer an incoming request body into memory to parse it
(JSON, form, etc.) before any Pydantic validation ever runs - a
``Field(max_length=...)`` on a request model caps the *parsed* value, not
the bytes read to produce it. Without a cap here, one oversized request body
is an easy way to spend memory on any route, including ones whose eventual
validated fields are tiny (e.g. login's two short strings). This API has no
file-upload endpoints (`grep -r UploadFile src/detoura/api` is empty), so
every real body is a small JSON document; the default cap is generous for
that and refuses everything else.

Pure ASGI middleware, not Starlette's ``BaseHTTPMiddleware``:
``BaseHTTPMiddleware`` already buffers the *entire* body itself before
handing it to the wrapped app, which defeats a byte cap during the read.
Working at the ASGI ``receive`` layer lets this middleware refuse
mid-stream, before an oversized body is ever fully held in memory - and
lets it catch a request with no (or a dishonest) ``Content-Length`` too,
since it counts bytes as they actually arrive rather than trusting the
header alone.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Generous for any JSON body this API sends or receives (booking intents,
#: traveler lists, commercial quotes) - there is no file-upload endpoint to
#: accommodate. Override via DETOURA_MAX_REQUEST_BODY_BYTES if a genuine
#: need for a larger body ever appears.
DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB


class _BodyTooLarge(Exception):
    pass


class MaxBodySizeMiddleware:
    """Rejects any HTTP request whose body exceeds ``max_bytes`` with a 413,
    before the wrapped application ever sees the full body."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Fast path: a well-formed, honest Content-Length lets us refuse
        # before reading a single byte of body.
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        await _reject_413(send)
                        return
                except ValueError:
                    pass  # malformed header - fall through to the streaming guard
                break

        seen = 0

        async def _guarded_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > self.max_bytes:
                    # More body has now arrived than Content-Length claimed
                    # (missing, wrong, or a chunked transfer with no length
                    # at all) - stop reading rather than let the handler
                    # keep buffering an unbounded stream.
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, _guarded_receive, send)
        except _BodyTooLarge:
            await _reject_413(send)


async def _reject_413(send: Send) -> None:
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({
        "type": "http.response.body",
        "body": b'{"detail":{"message":"Request body too large."}}',
    })
