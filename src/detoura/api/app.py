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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from ..observability import configure_logging, log_event, render_prometheus_text
from ..observability.logging import current_request_id
from ..observability.middleware import CorrelationMiddleware
from ..persistence import bootstrap as bootstrap_db
from ..persistence import get_db
from ..services.feedback import configure_sessions
from ..services.session_store import store_from_env
from .auth import router as auth_router
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

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """Readiness (V9 Limited Beta observability contract §15): checks
        only what this process needs to accept traffic at all - a live
        connection to its own database. Deliberately does NOT call Duffel or
        Stripe: a temporary provider outage should not pull the whole app
        out of a load balancer's rotation when it can still serve search,
        auth, and read-only routes just fine."""
        try:
            get_db().query_one("SELECT 1")
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
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
