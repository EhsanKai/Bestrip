"""The LLM seams - present, typed, and not required to run the MVP."""

from __future__ import annotations

from datetime import date

from travel_planner.llm import (
    ItineraryExplainer,
    KeywordPreferenceParser,
    PreferenceParser,
    TemplateItineraryExplainer,
)
from travel_planner.models.trip import TripRequest
from travel_planner.services.planner import TravelPlanner

from .conftest import trip_request


def test_local_implementations_satisfy_the_protocols():
    assert isinstance(KeywordPreferenceParser(), PreferenceParser)
    assert isinstance(TemplateItineraryExplainer(), ItineraryExplainer)


def test_parser_extracts_budget_travelers_duration_and_interests():
    parser = KeywordPreferenceParser(default_date_from=date(2026, 9, 10))
    request = parser.parse(
        "We are 2 people looking for a 5 day trip, budget 250 EUR, "
        "we love history and food, dates are flexible"
    )
    assert isinstance(request, TripRequest)
    assert request.budget == 250.0
    assert request.travelers == 2
    assert request.duration_days == 5
    assert request.date_flexible is True
    assert request.preferences.history == 0.9
    assert request.preferences.food == 0.9
    assert request.preferences.nightlife == 0.5


def test_parser_falls_back_to_defaults():
    parser = KeywordPreferenceParser(default_budget=800.0, default_duration_days=7)
    request = parser.parse("take me somewhere nice")
    assert request.budget == 800.0
    assert request.duration_days == 7
    assert request.travelers == 1
    assert request.date_flexible is False


def test_parsed_request_is_plannable():
    """The parser output feeds straight into the deterministic optimizer."""
    parser = KeywordPreferenceParser(default_date_from=date(2026, 9, 10))
    request = parser.parse(
        "2 travelers, 5 days, 400 EUR, we like culture and multiple cities"
    )
    result = TravelPlanner().plan(request)
    assert result.recommendations


def test_explainer_only_restates_computed_numbers():
    result = TravelPlanner().plan(trip_request())
    explainer = TemplateItineraryExplainer()
    itinerary = result.recommendations[0]
    text = explainer.explain(itinerary)

    assert itinerary.route_label() in text
    assert f"{itinerary.total_cost:.2f}" in text
    assert str(itinerary.total_travel_minutes) in text
    assert "Strongest factors" in text
    assert itinerary.baseline_comparison.baseline_destination in text


def test_explainer_handles_a_dearer_than_baseline_itinerary():
    result = TravelPlanner().plan(trip_request())
    itinerary = result.recommendations[0].model_copy(deep=True)
    itinerary.baseline_comparison.money_saved = -25.0
    text = TemplateItineraryExplainer().explain(itinerary)
    assert "more than" in text


def test_the_optimizer_does_not_depend_on_the_llm_package():
    """Rule 3: no LLM anywhere in search, scoring, constraints or pricing."""
    import ast
    import pathlib

    import travel_planner

    root = pathlib.Path(travel_planner.__file__).parent
    optimizer_packages = ("algorithms", "constraints", "services", "providers", "data")
    offenders = []
    for package in optimizer_packages:
        for path in (root / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and "llm" in (node.module or ""):
                    offenders.append(str(path))
                elif isinstance(node, ast.Import) and any(
                    "llm" in alias.name for alias in node.names
                ):
                    offenders.append(str(path))
    assert offenders == []
