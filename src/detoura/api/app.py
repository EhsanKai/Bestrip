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
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import router as engine_router
from .v1 import router as product_router

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


def create_app() -> FastAPI:
    app = FastAPI(
        title="Detoura",
        version="5.0.0",
        description=DESCRIPTION,
    )
    # The frontend dev server runs on a different port; in production it is
    # served from the same origin and this is a no-op.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(product_router)
    app.include_router(engine_router)
    return app


app = create_app()
