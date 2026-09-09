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

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..persistence import bootstrap as bootstrap_db
from ..services.feedback import configure_sessions
from ..services.session_store import store_from_env
from .ops import router as ops_router
from .routes import router as engine_router
from .static import mount_frontend
from .v1 import router as product_router

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


def create_app() -> FastAPI:
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
    app.include_router(product_router)
    app.include_router(engine_router)
    # V8.5: the admin/ops console API. Disabled (503) unless DETOURA_OPS_TOKEN
    # is set at runtime.
    app.include_router(ops_router)
    # Last: the SPA fallback is a catch-all and would shadow the routers.
    mount_frontend(app)
    return app


app = create_app()
