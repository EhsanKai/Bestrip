"""FastAPI application factory.

Run with::

    uvicorn travel_planner.api.app:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from .routes import router

DESCRIPTION = """
Deterministic multi-objective travel-route optimizer.

**All transport data is synthetic.** Prices, schedules and availability are
fabricated for this MVP and must not be treated as real-world offers.
"""


def create_app() -> FastAPI:
    app = FastAPI(
        title="Intelligent Budget Travel Planner",
        version="0.1.0",
        description=DESCRIPTION,
    )
    app.include_router(router)
    return app


app = create_app()
