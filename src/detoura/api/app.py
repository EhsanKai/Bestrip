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
    # Last: the SPA fallback is a catch-all and would shadow both routers.
    mount_frontend(app)
    return app


app = create_app()
