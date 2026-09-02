"""Explainability: structured facts, never prose, from the optimizer."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from detoura.api.app import create_app  # noqa: E402
from detoura.api.routes import get_planner  # noqa: E402
from detoura.models.itinerary import ExplanationFactor  # noqa: E402
from detoura.models.trip import TravelPreferences  # noqa: E402
from detoura.profiles import ProfileName  # noqa: E402
from detoura.services.planner import TravelPlanner  # noqa: E402

from .conftest import trip_request  # noqa: E402

PAYLOAD = {
    "origin": "Köln",
    "budget": 450,
    "travelers": 2,
    "duration_days": 5,
    "date_from": "2026-09-10",
    "date_to": "2026-09-15",
    "date_flexible": True,
    "transport_preferences": ["flight", "train"],
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


@pytest.fixture(scope="module")
def result():
    return TravelPlanner().plan(trip_request(), debug=True)


# ---------------------------------------------------------------------------
# Every number the spec asks to be exposed (section 42)
# ---------------------------------------------------------------------------
def test_every_documented_figure_is_exposed(result):
    for itinerary in result.recommendations:
        breakdown = itinerary.cost_breakdown
        assert breakdown.transport > 0
        assert breakdown.accommodation > 0
        assert breakdown.ground_transfer > 0
        assert breakdown.total == pytest.approx(itinerary.total_cost, abs=0.02)

        assert itinerary.total_travel_minutes > 0
        assert itinerary.ground_transfer_minutes > 0
        assert itinerary.total_transport_minutes > itinerary.total_travel_minutes
        assert itinerary.usable_destination_minutes > 0

        value = itinerary.value_breakdown
        assert value is not None
        for component in ("cost", "experience", "preferences", "time", "diversity"):
            assert 0.0 <= getattr(value, component) <= 1.0
        assert value.total == pytest.approx(itinerary.score, abs=1e-6)
        assert itinerary.preference_score == value.preferences
        assert itinerary.experience_score == value.experience


def test_stays_are_itemised(result):
    for itinerary in result.recommendations:
        assert len(itinerary.stays) == len(itinerary.cities)
        for stay, city in zip(itinerary.stays, itinerary.cities):
            assert stay.city == city
            assert stay.nights >= 1
            assert stay.departure > stay.arrival
            assert stay.accommodation_name
            assert stay.accommodation_cost > 0


# ---------------------------------------------------------------------------
# Explanation factors (section 43)
# ---------------------------------------------------------------------------
def test_factors_are_typed_and_unique(result):
    for itinerary in result.recommendations:
        factors = itinerary.explanation_factors
        assert factors
        assert all(isinstance(f, ExplanationFactor) for f in factors)
        assert len(factors) == len(set(factors))
        assert ExplanationFactor.FITS_BUDGET in factors


def test_city_count_factor_matches_the_itinerary(result):
    expected = {
        1: ExplanationFactor.SINGLE_CITY,
        2: ExplanationFactor.TWO_CITIES,
    }
    for itinerary in result.recommendations:
        wanted = expected.get(len(itinerary.cities), ExplanationFactor.MULTI_CITY)
        assert wanted in itinerary.explanation_factors


def test_cheaper_than_baseline_is_flagged_only_when_true(result):
    for itinerary in result.recommendations:
        cheaper = ExplanationFactor.CHEAPER_THAN_BASELINE in itinerary.explanation_factors
        assert cheaper == (itinerary.baseline_comparison.money_saved > 0)


def test_preferred_destination_is_flagged_when_visited():
    planner = TravelPlanner()
    result = planner.plan(trip_request(budget=700, preferred_destinations=["Prague"]))
    visiting = [i for i in result.recommendations if "Prague" in i.cities]
    assert visiting
    for itinerary in visiting:
        assert (
            ExplanationFactor.VISITS_PREFERRED_DESTINATION
            in itinerary.explanation_factors
        )


def test_mandatory_destination_is_flagged():
    planner = TravelPlanner()
    result = planner.plan(trip_request(budget=700, must_visit=["London"]))
    assert result.recommendations
    for itinerary in result.recommendations:
        assert (
            ExplanationFactor.VISITS_MANDATORY_DESTINATION
            in itinerary.explanation_factors
        )


def test_underspending_is_flagged():
    """A trip using a third of a very large budget says so."""
    planner = TravelPlanner()
    result = planner.plan(
        trip_request(budget=2000, preferences=TravelPreferences(multiple_cities=0.0)),
        profile=ProfileName.CHEAPEST,
    )
    assert result.recommendations
    assert (
        ExplanationFactor.LEAVES_BUDGET_UNUSED
        in result.recommendations[0].explanation_factors
    )


def test_factors_are_deterministic(result):
    again = TravelPlanner().plan(trip_request())
    assert [i.explanation_factors for i in again.recommendations] == [
        i.explanation_factors for i in result.recommendations
    ]


# ---------------------------------------------------------------------------
# The API surface (sections 51, 52)
# ---------------------------------------------------------------------------
@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_planner] = lambda: TravelPlanner()
    return TestClient(app)


def test_response_carries_the_v2_fields(client):
    body = client.post("/plan-trip", json=PAYLOAD).json()
    assert body["profile"] == "BEST_VALUE"
    itinerary = body["recommendations"][0]
    assert set(itinerary["cost_breakdown"]) == {
        "transport",
        "accommodation",
        "ground_transfer",
    }
    assert itinerary["usable_destination_minutes"] > 0
    assert itinerary["total_travel_minutes"] > 0
    assert itinerary["explanation_factors"]
    assert itinerary["value_breakdown"]["profile"] == "BEST_VALUE"
    assert itinerary["stays"]
    assert itinerary["baseline_comparison"]["baseline_cost"] > 0


def test_baseline_carries_its_own_cost_breakdown(client):
    body = client.post("/plan-trip", json=PAYLOAD).json()
    baseline = body["baseline"]
    assert baseline["cost_breakdown"]["accommodation"] > 0
    assert baseline["nights"] >= 1


def test_profile_query_parameter(client):
    for name in ("CHEAPEST", "BEST_VALUE", "ADVENTURE"):
        body = client.post(f"/plan-trip?profile={name}", json=PAYLOAD).json()
        assert body["profile"] == name
        assert body["metadata"]["profile"] == name


def test_profile_defaults_to_best_value(client):
    assert client.post("/plan-trip", json=PAYLOAD).json()["profile"] == "BEST_VALUE"


def test_an_unknown_profile_is_rejected(client):
    assert client.post("/plan-trip?profile=LUXURY", json=PAYLOAD).status_code == 422


def test_profiles_endpoint(client):
    body = client.get("/profiles").json()
    assert {p["name"] for p in body} == {"CHEAPEST", "BEST_VALUE", "ADVENTURE"}
    for profile in body:
        assert profile["description"]
        assert set(profile["weights"]) >= {
            "cost",
            "experience",
            "preferences",
            "time",
            "diversity",
        }


def test_score_and_cost_are_both_exposed_and_can_disagree(client):
    """A dearer trip may score higher - the API must show both numbers."""
    body = client.post("/plan-trip?profile=ADVENTURE", json=PAYLOAD).json()
    pairs = [(i["score"], i["total_cost"]) for i in body["recommendations"]]
    assert all(score > 0 and cost > 0 for score, cost in pairs)
    scores = [score for score, _ in pairs]
    assert scores == sorted(scores, reverse=True)
    # Ranking by score is not the same as ranking by price.
    costs = [cost for _, cost in pairs]
    assert costs != sorted(costs)
