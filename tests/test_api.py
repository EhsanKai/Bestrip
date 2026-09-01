"""HTTP adapter."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from travel_planner.api.app import create_app  # noqa: E402
from travel_planner.api.routes import get_planner  # noqa: E402
from travel_planner.services.planner import TravelPlanner  # noqa: E402

#: The exact request body from the spec.
PAYLOAD = {
    "origin": "Köln",
    # V2 prices accommodation and ground transfers, so the V1 figure of 250
    # buys nothing for two people over five days.
    "budget": 450,
    "travelers": 2,
    "duration_days": 5,
    "date_from": "2026-09-10",
    "date_to": "2026-09-15",
    "date_flexible": True,
    "transport_preferences": ["flight", "train"],
    "must_visit": [],
    "preferred_destinations": ["Madrid"],
    "avoid_destinations": ["Paris"],
    "preferences": {
        "history": 0.8,
        "nature": 0.7,
        "nightlife": 0.2,
        "culture": 0.8,
        "food": 0.6,
        "multiple_cities": 0.9,
    },
}


@pytest.fixture
def client(transport, destinations, config) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_planner] = lambda: TravelPlanner(
        transport, destinations, config=config
    )
    return TestClient(app)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "data_source": "synthetic"}


def test_plan_trip_returns_the_documented_shape(client):
    response = client.post("/plan-trip", json=PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"profile", "baseline", "recommendations", "metadata"}
    assert body["profile"] == "BEST_VALUE"

    assert body["baseline"]["destination"] == "Madrid"
    assert body["baseline"]["total_cost"] <= 450

    assert body["recommendations"]
    for index, itinerary in enumerate(body["recommendations"], start=1):
        assert itinerary["rank"] == index
        assert itinerary["total_cost"] <= 450
        assert itinerary["currency"] == "EUR"
        assert itinerary["legs"]
        assert itinerary["cities"]
        assert itinerary["baseline_comparison"]["baseline_destination"] == "Madrid"

    metadata = body["metadata"]
    assert metadata["origin"] == "Köln"
    assert {"CGN", "DUS"} <= set(metadata["origin_airports"])
    assert metadata["beam_width"] == 20
    assert metadata["states_generated"] > 0


def test_debug_flag_returns_the_search_trace(client):
    plain = client.post("/plan-trip", json=PAYLOAD).json()
    assert "debug" not in plain

    traced = client.post("/plan-trip?debug=true", json=PAYLOAD).json()
    assert "debug" in traced
    debug = traced["debug"]
    assert debug["iterations"]
    first = debug["iterations"][0]
    assert {"generated", "rejected", "kept", "beam_width"} <= set(first)
    assert debug["pareto_input"] >= debug["pareto_kept"]
    assert debug["filtered"]


def test_avoided_destination_never_appears(client):
    body = client.post("/plan-trip", json={**PAYLOAD, "budget": 600}).json()
    for itinerary in body["recommendations"]:
        assert "Paris" not in itinerary["cities"]


def test_invalid_body_is_rejected(client):
    response = client.post("/plan-trip", json={**PAYLOAD, "budget": -5})
    assert response.status_code == 422


def test_unknown_origin_is_a_client_error(client):
    response = client.post("/plan-trip", json={**PAYLOAD, "origin": "Atlantis"})
    assert response.status_code == 422
    assert "unknown origin" in response.json()["detail"]


def test_contradictory_destinations_are_rejected(client):
    response = client.post(
        "/plan-trip",
        json={**PAYLOAD, "must_visit": ["Paris"], "avoid_destinations": ["Paris"]},
    )
    assert response.status_code == 422


def test_destinations_endpoint(client):
    body = client.get("/destinations").json()
    assert len(body) >= 16
    assert {"id", "country", "history", "recommended_min_days"} <= set(body[0])


def test_config_endpoint(client):
    body = client.get("/config").json()
    assert body["beam_width"] == 20
    assert body["max_cities"] == 4
    assert body["score_weights"]["budget"] == 0.40


def test_api_is_only_an_adapter(transport, destinations, config):
    """The same request, planned without touching FastAPI, gives the same answer."""
    from travel_planner.models.trip import TripRequest

    planner = TravelPlanner(transport, destinations, config=config)
    direct = planner.plan(TripRequest(**PAYLOAD))

    app = create_app()
    app.dependency_overrides[get_planner] = lambda: planner
    over_http = TestClient(app).post("/plan-trip", json=PAYLOAD).json()

    assert [i.route_label() for i in direct.recommendations] == [
        " -> ".join(
            [i["legs"][0]["origin"]] + [leg["destination"] for leg in i["legs"]]
        )
        for i in over_http["recommendations"]
    ]
