"""FastAPI application factory.

Run with::

    uvicorn detoura.api.app:app --reload

Two routers are mounted, deliberately:

* ``/api/v1`` is the **product** API - the contract the Detoura frontend
  consumes, which knows nothing about beams or frontiers.
* the root router is the **engine** API, which exposes the full ``PlanResult``
  including the search trace. It is kept for development, tuning and anyone
  who wants to see the optimizer's own view.

Keeping them separate is what lets the search strategy change without a
frontend release.

In production the app also serves the built web client from the same origin,
when there is one on disk - see :mod:`detoura.api.static`.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from ..auth_config import auth_config
from ..communication_config import resolve_communication_provider
from ..observability import configure_logging, log_event, render_prometheus_text
from ..observability.logging import current_request_id
from ..observability.middleware import CorrelationMiddleware
from ..persistence import bootstrap as bootstrap_db
from ..persistence import get_db
from ..services.feedback import configure_sessions
from ..services.session_store import store_from_env
from .auth import router as auth_router
from .auth_account import router as auth_account_router
from .auth_google import router as auth_google_router
from .body_limit import DEFAULT_MAX_BODY_BYTES, MaxBodySizeMiddleware
from .destination_images import mount_destination_images
from .destination_images import router as destination_images_router
from .me_trips import router as me_trips_router
from .ops import router as ops_router
from .ops_attractiveness import router as ops_attractiveness_router
from .ops_confirmations import router as ops_confirmations_router
from .ops_market_prior_acquisition import router as ops_acquisition_router
from .ops_payments import router as ops_payments_router
from .ops_search_intel import router as ops_search_intel_router
from .payments import router as payments_router
from .routes import router as engine_router
from .static import mount_frontend
from .v1 import router as product_router

_logger = logging.getLogger("detoura.api")

# Where the Vite dev server runs. Kept as the default because the common case
# is a developer with `npm run dev` on one port and `uvicorn` on another.
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

#: Field-name fragments whose submitted value a 422 validation error must
#: never echo back (see ``_validation_exception_handler``) - mirrors the
#: intent of ``observability.logging.FORBIDDEN_FIELDS`` but for API
#: responses rather than logs. Matched as a substring, not an exact name
#: (V9 Google auth + account lifecycle security review): the original,
#: exact-match version of this set caught ``password`` but not
#: ``current_password``/``new_password`` - both real field names on the
#: password-change/reset endpoints added in this slice - which reproduced
#: the exact same verbatim-secret-in-a-422-body leak the exact-match
#: version was written to fix, just under a different field name. A
#: substring match is the only way this list does not need a new literal
#: entry every time a future endpoint's field happens to end in
#: ``_password`` instead of being named ``password``.
_SENSITIVE_VALIDATION_FIELDS = frozenset({
    "password", "token", "secret", "csrf", "authorization",
    "cookie", "card_number", "cvc", "cvv",
})


def _is_sensitive_field(name: object) -> bool:
    text = str(name).lower()
    return any(sensitive in text for sensitive in _SENSITIVE_VALIDATION_FIELDS)


def _redact_sensitive(value: object) -> object:
    """Blank out dict values whose own key looks sensitive, recursively.

    A FastAPI/Pydantic "missing" validation error's ``input`` is not that one
    field's value - there is nothing to point to when the field itself is
    absent - it is the *whole* submitted object the field was missing from.
    Checking only the erroring field's own ``loc`` (as this handler used to)
    misses that: a password submitted alongside a separately-missing email
    rode along untouched inside that object. This walks the value looking for
    sensitive keys independently of which field actually failed validation."""
    if isinstance(value, dict):
        return {
            key: "[redacted]" if _is_sensitive_field(key) else _redact_sensitive(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


#: Security response headers (V9 staging hardening audit,
#: docs/V9_REAL_STAGING_DEPLOYMENT_REPORT.md): a real deployment was found
#: sending none of these at all. The policy below is derived from this app's
#: actual dependencies (audited directly, not templated): Wikimedia-hosted
#: destination photography is the only cross-origin resource the client
#: loads; Google sign-in is a server-side 302 redirect
#: (api/auth_google.py), never a client-side script/iframe; there is no
#: Stripe.js or other payment SDK wired up yet (Checkout.tsx is a
#: placeholder); no inline <script>/<style> or `dangerouslySetInnerHTML`
#: exists anywhere in the frontend; no camera/microphone/geolocation API is
#: used. Swagger UI (/docs, /redoc, /openapi.json) is the one real exception
#: - it loads third-party CDN assets and inline scripts by default - so it is
#: excluded from the CSP below rather than loosening the policy for every
#: route to accommodate it.
_CSP_EXEMPT_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})
_CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "img-src 'self' https://thumb.wikimedia.org",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "connect-src 'self'",
    "media-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))

DESCRIPTION = """
**Detoura** - AI travel discovery and optimization.

Detoura does not just find flights. It discovers the trips you did not think to
search for: given a budget, some dates and a sense of how you like to travel,
it explores destinations, routes and stays and reports the ones worth taking.

`/api/v1` is the product API. The unprefixed routes expose the optimizer's own
result, including its search trace, for development.

**All data is synthetic.** Prices, schedules, inventory and availability are
fabricated for this build and must not be treated as real-world offers.
"""


def cors_origins() -> list[str]:
    """The origins allowed to call this API.

    ``DETOURA_CORS_ORIGINS`` is a comma-separated list, and setting it
    *replaces* the dev defaults rather than adding to them: a deployment that
    names its origins should not silently keep trusting localhost.

    There is deliberately no wildcard shortcut. When the client is served from
    this same origin - the default deployment - no origin needs listing at all,
    because the requests are not cross-origin in the first place.
    """
    configured = os.getenv("DETOURA_CORS_ORIGINS", "").strip()
    if not configured:
        return list(DEV_ORIGINS)
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


def _max_body_bytes() -> int:
    raw = os.getenv("DETOURA_MAX_REQUEST_BODY_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_BODY_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_BODY_BYTES
    return value if value > 0 else DEFAULT_MAX_BODY_BYTES


def create_app() -> FastAPI:
    # Structured (JSON) logging on the root logger, before any request or
    # startup log line is emitted. Idempotent - safe even if create_app()
    # runs more than once in a process (tests build the app repeatedly).
    configure_logging()

    # Install the session store this deployment is configured for, before any
    # request can touch it.
    #
    # Every worker process runs this, which is the point: with more than one
    # worker the default process-local store is not merely slower, it is
    # wrong - four workers held four disagreeing copies of one traveller's
    # profile. Configuring it here rather than at import time keeps the choice
    # observable in tests, which build the app explicitly.
    configure_sessions(store_from_env())

    # The commercial + ops database (SQLite). Seeds the example markup policy
    # and promo on first run. Path from DETOURA_DB_PATH; see the Dockerfile.
    bootstrap_db()

    app = FastAPI(
        title="Detoura",
        version="5.0.0",
        description=DESCRIPTION,
    )
    # Only needed when the client is served from somewhere else. Same-origin
    # deployments never exercise this.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # V9 Phase 6 slice 2: reject an oversized request body before it is
    # ever fully buffered in memory - added last/outermost (Starlette runs
    # the most-recently-added middleware first) so it can refuse before
    # CORS or any route even sees the request. See body_limit.py.
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=_max_body_bytes())
    # Limited Beta observability baseline: request correlation + timing +
    # structured request_completed/request_failed logging. Added last/
    # outermost (see MaxBodySizeMiddleware's own comment above on Starlette's
    # ordering) so it wraps and times every request, including one CORS or
    # the body-size guard itself rejects.
    app.add_middleware(CorrelationMiddleware)

    # V9 staging/production ops readiness: only a deployment that explicitly
    # sets DETOURA_ENV=production (see render.yaml and auth_config.py, which
    # this same flag already gates Secure cookies on) is ever indexable -
    # *unless* DETOURA_FORCE_NOINDEX is also set, which keeps a Secure-cookie,
    # DETOURA_ENV=production deployment noindexed anyway (V9 staging
    # hardening: a real Render deployment needed Secure cookies but was not
    # meant to be indexable, which the old single-flag gate could not express
    # without sacrificing one for the other - see auth_config.py's
    # force_noindex docstring). Anything else - a staging host, a review app,
    # a bare `docker run` with no env set - fails closed to noindex, so a
    # forgotten config flag costs search visibility, never the reverse
    # (docs/V9_TECHNICAL_SEO_FOUNDATION_REPORT.md flagged staging noindex as
    # unsolved; a client-side meta tag alone would miss crawlers that don't
    # execute JS, so this is a real HTTP response header instead).
    _cfg = auth_config()
    _indexable_deployment = _cfg.is_production and not _cfg.force_noindex

    @app.middleware("http")
    async def _robots_header_middleware(request: Request, call_next):
        response = await call_next(request)
        if not _indexable_deployment:
            response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        return response

    @app.middleware("http")
    async def _security_headers_middleware(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        # Modern equivalent is CSP's frame-ancestors (below); this is kept too
        # as defense-in-depth for browsers that predate it - nothing in this
        # app frames itself or needs to be framed by anyone else.
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path not in _CSP_EXEMPT_PATHS:
            response.headers.setdefault("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
        # Gated on the same flag as Secure cookies, not force_noindex: HSTS is
        # a transport-security guarantee about this host always being HTTPS,
        # unrelated to whether it should be search-indexed. Never emitted in
        # local/dev (DETOURA_ENV unset), where the server is plain HTTP and a
        # browser that believed this header would simply fail to connect.
        if _cfg.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000"
            )
        return response

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """The generic-500 safety net (V9 Limited Beta observability
        contract §16): logs the exception server-side with the request's
        correlation id, and returns a safe, stack-trace-free body carrying
        that same id so a report to Ops can be matched back to these logs.
        Never intercepts an ``HTTPException`` - FastAPI handles those on its
        own path with the message the route author chose, unchanged."""
        request_id = current_request_id()
        log_event(
            _logger, "unhandled_exception", level=logging.ERROR,
            method=request.method, route=request.url.path,
            exception_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": {"message": "An unexpected error occurred.", "request_id": request_id}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Same shape as FastAPI's own default handler, except a field whose
        name marks it sensitive never has its submitted value echoed back
        (V9 account/auth security audit, hardened again in the V9 staging
        audit): FastAPI's default handler puts the raw invalid value in each
        error's ``input``, so a password that only failed a length check -
        never anything about its content - came back verbatim in the 422
        body. ``type``/``loc``/``msg`` are kept, so the client still learns
        *what* was wrong, just not the secret itself. A "missing field"
        error's ``input`` is the whole submitted object, not just the absent
        field's value, so a sensitive sibling field (e.g. a real password,
        submitted alongside a merely-missing email) is redacted key-by-key
        rather than only checked against the one field that actually
        failed - see ``_redact_sensitive``. A body that is not even an
        object at all (a bare JSON string/number as the whole request body)
        fails the same way with ``loc == ["body"]`` and ``input`` set to that
        raw value verbatim - there is no field name to check for
        sensitivity and no dict to walk, so the only safe default is to drop
        it, exactly like a recognized-sensitive field, rather than risk
        echoing whatever a malformed client sent bare."""
        errors = jsonable_encoder(exc.errors())
        for error in errors:
            loc = tuple(error.get("loc") or ())
            if any(_is_sensitive_field(part) for part in loc):
                error.pop("input", None)
                continue
            if "input" not in error:
                continue
            value = error["input"]
            if loc in ((), ("body",)) and not isinstance(value, dict):
                # The whole request body failed before it was even treated
                # as an object - a bare scalar, or a bare list of scalars
                # with no keys for _redact_sensitive to judge sensitivity
                # by (second-round review finding: a bare JSON array body
                # sailed through the dict/list branch below untouched,
                # since a list of plain strings has no dict keys to redact).
                # No field name and no safe key-based structure either way,
                # so the only safe default is to drop it entirely.
                error.pop("input", None)
            elif isinstance(value, (dict, list)):
                error["input"] = _redact_sensitive(value)
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """Readiness (V9 Limited Beta observability contract §15): checks
        only what this process needs to accept traffic at all - a live
        connection to its own database. Deliberately does NOT call Duffel,
        Stripe, or Resend: a temporary provider outage should not pull the
        whole app out of a load balancer's rotation when it can still serve
        search, auth, and read-only routes just fine.

        It DOES validate that the *configured* communication provider can be
        resolved at all (V9 Production Transactional Email §22) - this is a
        pure configuration check (env vars present, key shape looks right),
        never a network call to Resend, so a real provider outage never
        affects readiness, but a deployment that turns on
        ``COMMUNICATION_LIVE_SENDING_ENABLED=true`` with
        ``COMMUNICATION_PROVIDER=resend`` and a missing/malformed
        ``RESEND_API_KEY``/``RESEND_FROM_EMAIL`` fails loudly here instead of
        silently sending nothing on the first real booking."""
        try:
            get_db().query_one("SELECT 1")
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        try:
            resolve_communication_provider()
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "communication_provider_misconfigured"},
            )
        return JSONResponse(status_code=200, content={"status": "ready"})

    if os.getenv("DETOURA_METRICS_ENABLED", "").strip().lower() in ("1", "true", "yes"):
        # Off by default (V9 Limited Beta observability contract §13): this
        # process has no auth story for its own endpoints, so /metrics is
        # opt-in and the operator is expected to keep it off any
        # publicly-reachable network path - see the observability report's
        # deployment notes.
        @app.get("/metrics")
        def metrics_endpoint() -> PlainTextResponse:
            return PlainTextResponse(render_prometheus_text(), media_type="text/plain; version=0.0.4")

    app.include_router(product_router)
    app.include_router(engine_router)
    # V9 Phase 2.6: account auth + My Trips. Anonymous callers of every other
    # route are unaffected - nothing on the search/booking path depends on
    # these.
    app.include_router(auth_router)
    app.include_router(auth_google_router)
    app.include_router(auth_account_router)
    app.include_router(me_trips_router)
    # V9 Phase 4: payment intents/confirm/refund + provider webhook.
    # Server-owned amount/currency/quote throughout - never client-supplied.
    app.include_router(payments_router)
    # V9 Phase 2.6 Part B: destination-image manifest seam. A no-op (404s,
    # not an error) when no image library has been acquired on disk.
    app.include_router(destination_images_router)
    # V8.5: the admin/ops console API. Disabled (503) unless DETOURA_OPS_TOKEN
    # is set at runtime.
    app.include_router(ops_router)
    # V9 Phase 1: read access to Search Intelligence (Price Memory coverage,
    # market signals, search traces, provider economics). Ops-authenticated.
    app.include_router(ops_search_intel_router)
    # V9 Phase 2.5: Authorized Market-Prior Acquisition job control (sources,
    # jobs, tasks). Ops-authenticated; unreachable from consumer search.
    app.include_router(ops_acquisition_router)
    # V9 Phase 3: read access to Destination Attractiveness profiles/coverage
    # + a reseed trigger. Ops-authenticated; no consumer-facing route.
    app.include_router(ops_attractiveness_router)
    # V9 Phase 4: payment visibility + explicit, domain-validated recovery
    # actions (reconcile/capture/cancel/refund) - no generic "set status".
    app.include_router(ops_payments_router)
    # V9 Phase 5: post-booking confirmation/document/communication visibility
    # + the one safe recovery action (retry communication) - no generic
    # "mark sent"/"mark issued"/"mark confirmed".
    app.include_router(ops_confirmations_router)
    # Static WebP assets for the destination-image seam above.
    mount_destination_images(app)
    # Last: the SPA fallback is a catch-all and would shadow the routers.
    mount_frontend(app)
    return app


app = create_app()
