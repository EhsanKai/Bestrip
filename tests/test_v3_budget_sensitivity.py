"""Budget sensitivity: what would another fifty euros buy?"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from travel_planner.api.app import create_app  # noqa: E402
from travel_planner.api.routes import get_planner  # noqa: E402
from travel_planner.profiles import ProfileName  # noqa: E402
from travel_planner.services.budget_sensitivity import (  # noqa: E402
    analyze_budget_sensitivity,
)
from travel_planner.services.planner import TravelPlanner  # noqa: E402

from .conftest import trip_request  # noqa: E402


@pytest.fixture(scope="module")
def sweep():
    planner = TravelPlanner()
    request = trip_request(budget=400)
    return analyze_budget_sensitivity(
        planner, request, budgets=[240.0, 300.0, 360.0, 420.0, 500.0]
    )


def test_the_sweep_covers_every_budget(sweep):
    assert [step.budget for step in sweep.steps] == [240.0, 300.0, 360.0, 420.0, 500.0]
    assert sweep.currency == "EUR"
    assert sweep.profile is ProfileName.BEST_VALUE


def test_a_bigger_budget_never_buys_less(sweep):
    """More money must never make the best available trip worse."""
    feasible = [step for step in sweep.steps if step.feasible]
    scores = [step.best_score for step in feasible]
    assert scores == sorted(scores), scores


def test_new_trips_appear_as_the_budget_rises(sweep):
    """The whole point: report the levels where something changes."""
    unlocked_totals = [len(step.unlocked) for step in sweep.steps if step.feasible]
    assert sum(unlocked_totals) > 1
    assert sweep.thresholds
    assert all(step.is_threshold for step in sweep.thresholds)


def test_no_step_ever_exceeds_its_own_budget(sweep):
    for step in sweep.steps:
        if step.feasible:
            assert step.best_cost <= step.budget


def test_the_minimum_feasible_budget_is_reported(sweep):
    minimum = sweep.minimum_feasible_budget
    assert minimum is not None
    for step in sweep.steps:
        if step.budget < minimum:
            assert not step.feasible


def test_a_budget_that_buys_nothing_is_reported_not_hidden():
    planner = TravelPlanner()
    sweep = analyze_budget_sensitivity(
        planner, trip_request(budget=100), budgets=[40.0, 80.0]
    )
    assert all(not step.feasible for step in sweep.steps)
    assert sweep.minimum_feasible_budget is None


def test_the_default_sweep_brackets_the_requested_budget():
    planner = TravelPlanner()
    sweep = analyze_budget_sensitivity(
        planner, trip_request(budget=400), steps=5, span=0.5
    )
    budgets = [step.budget for step in sweep.steps]
    assert len(budgets) == 5
    assert budgets[0] < 400 < budgets[-1]
    assert budgets == sorted(budgets)


def test_a_single_step_is_the_requested_budget():
    planner = TravelPlanner()
    sweep = analyze_budget_sensitivity(planner, trip_request(budget=400), steps=1)
    assert [step.budget for step in sweep.steps] == [400.0]


def test_profiles_can_be_swept_independently():
    planner = TravelPlanner()
    request = trip_request(budget=420)
    cheapest = analyze_budget_sensitivity(
        planner, request, budgets=[420.0], profile=ProfileName.CHEAPEST
    )
    adventure = analyze_budget_sensitivity(
        planner, request, budgets=[420.0], profile=ProfileName.ADVENTURE
    )
    assert cheapest.profile is ProfileName.CHEAPEST
    assert cheapest.steps[0].best_cost <= adventure.steps[0].best_cost


def test_the_sweep_is_deterministic():
    planner = TravelPlanner()
    request = trip_request(budget=400)
    first = analyze_budget_sensitivity(planner, request, budgets=[320.0, 400.0])
    second = analyze_budget_sensitivity(planner, request, budgets=[320.0, 400.0])
    assert first == second


def test_the_sweep_reuses_the_planner_caches():
    """Repeated planning must not multiply upstream provider calls."""
    planner = TravelPlanner()
    request = trip_request(budget=400)
    planner.plan(request)
    warm = planner.plan(request).metadata.provider_metrics["misses"]
    assert warm == 0

    analyze_budget_sensitivity(planner, request, budgets=[400.0])
    assert planner.plan(request).metadata.provider_metrics["misses"] == 0


def test_invalid_step_counts_are_rejected():
    with pytest.raises(ValueError, match="steps"):
        analyze_budget_sensitivity(TravelPlanner(), trip_request(), steps=0)


def test_it_renders(sweep):
    text = sweep.render()
    assert "Budget sensitivity" in text
    assert "BEST_VALUE" in text


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------
@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_planner] = lambda: TravelPlanner()
    return TestClient(app)


PAYLOAD = {
    "origin": "Köln",
    "budget": 400,
    "travelers": 2,
    "duration_days": 5,
    "date_from": "2026-09-10",
    "date_to": "2026-09-15",
    "date_flexible": True,
    "transport_preferences": ["flight", "train"],
    "preferred_destinations": ["Madrid"],
    "avoid_destinations": ["Paris"],
}


def test_the_endpoint_returns_the_sweep(client):
    body = client.post("/budget-sensitivity?steps=4", json=PAYLOAD).json()
    assert body["profile"] == "BEST_VALUE"
    assert len(body["steps"]) == 4
    for step in body["steps"]:
        assert {"budget", "feasible", "best_cost", "city_sets", "unlocks"} <= set(step)
        if step["feasible"]:
            assert step["best_cost"] <= step["budget"]


def test_the_endpoint_honours_the_profile(client):
    body = client.post(
        "/budget-sensitivity?steps=2&profile=CHEAPEST", json=PAYLOAD
    ).json()
    assert body["profile"] == "CHEAPEST"


def test_the_endpoint_validates_its_arguments(client):
    assert client.post("/budget-sensitivity?steps=0", json=PAYLOAD).status_code == 422
    assert (
        client.post("/budget-sensitivity", json={**PAYLOAD, "budget": -1}).status_code
        == 422
    )
