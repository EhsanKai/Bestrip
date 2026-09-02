"""V5: product semantics on top of the V4 engine.

Three things this file exists to protect, all of them stated as rules in the
V5 spec and none of them enforced by the engine's own tests:

1. **Infrastructure failure is never reported as "no trips found."** These are
   different sentences with different next steps, and a UI can only tell them
   apart if the backend does.
2. **Search modes are a product intent, not a beam width.** The frontend must
   never need to know what a beam is.
3. **The product contract does not leak the optimizer.** If a screen can read
   `beam_rounds`, changing the search becomes a frontend release.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from detoura.api.assembler import _freshness
from detoura.api.contracts import (
    AvailabilityStatus,
    IntensityBand,
    TripSearchRequest,
)
from detoura.config import PlannerConfig
from detoura.models.freshness import (
    FRESH_WINDOW,
    RECENT_WINDOW,
    PriceChange,
    PriceFreshness,
    PriceProvenance,
    combine,
)
from detoura.models.itinerary import StaySummary
from detoura.providers.failures import (
    FailureLog,
    ProviderFailure,
    ProviderFailureKind,
)
from detoura.providers.http import ProviderHttpError, RateLimitExceeded
from detoura.providers.resilient import (
    ResilientAccommodationProvider,
    ResilientTransportProvider,
    classify,
)
from detoura.search_modes import (
    MODE_SETTINGS,
    SearchMode,
    apply_mode,
    deeper_than,
)
from detoura.services.confidence import ConfidenceLevel, SearchQuality, assess

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from detoura.api.app import create_app  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0)

PAYLOAD = {
    "origin": "Köln",
    "budget": 450,
    "travelers": 2,
    "duration_days": 5,
    "date_from": "2026-09-10",
    "date_to": "2026-09-15",
    "date_flexible": True,
    "interests": ["culture", "history", "food"],
    "avoided_destinations": ["Paris"],
    "preferred_destinations": ["Madrid"],
}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(scope="module")
def searched(client) -> dict:
    response = client.post("/api/v1/search", json=PAYLOAD)
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# V5.1.1 - failure semantics
# ---------------------------------------------------------------------------
def test_no_results_is_not_an_infrastructure_failure():
    """The distinction the whole module exists for."""
    assert not ProviderFailureKind.NO_RESULTS.is_infrastructure
    assert not ProviderFailureKind.SOLD_OUT.is_infrastructure
    for kind in (
        ProviderFailureKind.TIMEOUT,
        ProviderFailureKind.UNAVAILABLE,
        ProviderFailureKind.MALFORMED_RESPONSE,
        ProviderFailureKind.AUTHENTICATION_FAILED,
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.CURRENCY_UNAVAILABLE,
    ):
        assert kind.is_infrastructure, kind


def test_only_transient_failures_are_retryable():
    """"Try again" must be honest advice, not a shrug."""
    assert ProviderFailureKind.TIMEOUT.is_retryable
    assert ProviderFailureKind.RATE_LIMITED.is_retryable
    assert not ProviderFailureKind.AUTHENTICATION_FAILED.is_retryable
    assert not ProviderFailureKind.MALFORMED_RESPONSE.is_retryable


def test_every_failure_has_something_to_show_a_person():
    for kind in ProviderFailureKind:
        failure = ProviderFailure(kind=kind, provider="test")
        assert failure.message and not failure.message.startswith("None")


def test_a_log_of_no_results_is_not_degraded():
    log = FailureLog()
    log.record(ProviderFailureKind.NO_RESULTS, "synthetic", context="CGN->VIE")
    assert log.failures
    assert not log.degraded


def test_a_log_with_a_timeout_is_degraded():
    log = FailureLog()
    log.record(ProviderFailureKind.TIMEOUT, "amadeus")
    assert log.degraded
    assert len(log.infrastructure_failures) == 1


def test_one_outage_is_reported_once_with_a_count():
    """Forty timeouts on forty lookups is one fact about the world."""
    log = FailureLog()
    for _ in range(40):
        log.record(ProviderFailureKind.TIMEOUT, "amadeus")
    summary = log.summary()
    assert len(summary) == 1
    assert summary[0]["occurrences"] == 40


@pytest.mark.parametrize(
    "error,expected",
    [
        (RateLimitExceeded("429", status=429), ProviderFailureKind.RATE_LIMITED),
        (ProviderHttpError("x timed out after 10s"), ProviderFailureKind.TIMEOUT),
        (
            ProviderHttpError("upstream returned 200 with a body that is not JSON"),
            ProviderFailureKind.MALFORMED_RESPONSE,
        ),
        (
            ProviderHttpError("cannot price a GBP quote"),
            ProviderFailureKind.CURRENCY_UNAVAILABLE,
        ),
        (ProviderHttpError("boom"), ProviderFailureKind.UNAVAILABLE),
        (RuntimeError("who knows"), ProviderFailureKind.UNAVAILABLE),
    ],
)
def test_exceptions_classify(error, expected):
    assert classify(error) is expected


class Broken:
    def search(self, *args, **kwargs):
        raise ProviderHttpError("provider is down")

    def min_price_per_night(self, city, travelers):
        raise ProviderHttpError("provider is down")


def test_a_broken_transport_provider_degrades_instead_of_aborting():
    """One failed route lookup must not lose the other eight hundred trips."""
    log = FailureLog()
    provider = ResilientTransportProvider(Broken(), log, name="test")
    assert provider.search("CGN", "VIE", date(2026, 9, 10)) == []
    assert log.degraded


def test_a_degraded_bound_is_unknown_not_zero_and_not_a_guess():
    """Admissibility survives a provider failure.

    ``None`` means "do not prune", which loses efficiency and never
    correctness. A guess here is the inadmissibility bug V3 already fixed once.
    """
    log = FailureLog()
    provider = ResilientAccommodationProvider(Broken(), log, name="test")
    assert provider.min_price_per_night("Prague", 2) is None


# ---------------------------------------------------------------------------
# V5.2 - search modes
# ---------------------------------------------------------------------------
def test_the_default_mode_is_smart():
    assert TripSearchRequest(
        origin="Köln", date_from="2026-09-10", date_to="2026-09-15",
        duration_days=5, budget=450,
    ).search_mode is SearchMode.SMART


def test_smart_reproduces_the_engine_default():
    """Every published V4 benchmark describes the default experience."""
    base = PlannerConfig()
    smart = apply_mode(base, SearchMode.SMART)
    assert smart.beam_width == base.beam_width
    assert smart.adaptive_beam is False
    assert smart.max_transport_options_per_leg == base.max_transport_options_per_leg
    assert smart.accommodation_options_per_stay == base.accommodation_options_per_stay


def test_quick_narrows_the_branching_not_only_the_beam():
    """Halving the beam alone does not reach a second: branching is the cost."""
    quick = apply_mode(PlannerConfig(), SearchMode.QUICK)
    smart = apply_mode(PlannerConfig(), SearchMode.SMART)
    assert quick.beam_width < smart.beam_width
    assert quick.max_transport_options_per_leg < smart.max_transport_options_per_leg
    assert quick.accommodation_options_per_stay < smart.accommodation_options_per_stay


def test_only_deep_is_adaptive_and_it_is_time_bounded():
    assert apply_mode(PlannerConfig(), SearchMode.DEEP).adaptive_beam is True
    assert apply_mode(PlannerConfig(), SearchMode.QUICK).adaptive_beam is False
    assert apply_mode(PlannerConfig(), SearchMode.SMART).adaptive_beam is False
    # A mode that promises "10-15 seconds" must be able to keep the promise.
    assert MODE_SETTINGS[SearchMode.DEEP].time_budget_seconds is not None


def test_applying_a_mode_never_mutates_the_callers_config():
    base = PlannerConfig()
    apply_mode(base, SearchMode.DEEP)
    assert base.adaptive_beam is False


def test_deeper_than_runs_out_at_the_top():
    assert deeper_than(SearchMode.QUICK) is SearchMode.SMART
    assert deeper_than(SearchMode.SMART) is SearchMode.DEEP
    assert deeper_than(SearchMode.DEEP) is None


def test_quick_is_actually_quicker(client):
    quick = client.post("/api/v1/search", json={**PAYLOAD, "search_mode": "QUICK"}).json()
    smart = client.post("/api/v1/search", json={**PAYLOAD, "search_mode": "SMART"}).json()
    assert quick["recommendations"]
    assert (
        quick["diagnostics"]["itineraries_considered"]
        < smart["diagnostics"]["itineraries_considered"]
    )


def test_every_mode_returns_usable_trips(client):
    for mode in ("QUICK", "SMART", "DEEP"):
        body = client.post("/api/v1/search", json={**PAYLOAD, "search_mode": mode}).json()
        assert body["recommendations"], mode
        assert body["diagnostics"]["mode"] == mode
        for trip in body["recommendations"]:
            assert trip["total_price"] <= PAYLOAD["budget"]


# ---------------------------------------------------------------------------
# V5.3 - price freshness
# ---------------------------------------------------------------------------
def test_freshness_reads_off_the_clock():
    stamp = PriceProvenance(provider="amadeus", fetched_at=NOW)
    assert stamp.freshness_at(NOW) is PriceFreshness.FRESH
    assert stamp.freshness_at(NOW + FRESH_WINDOW / 2) is PriceFreshness.FRESH
    assert stamp.freshness_at(NOW + RECENT_WINDOW / 2) is PriceFreshness.RECENT
    assert stamp.freshness_at(NOW + timedelta(hours=2)) is PriceFreshness.STALE


def test_the_providers_own_expiry_wins():
    """It knows its quote's lifetime better than any window we would guess."""
    stamp = PriceProvenance(
        provider="amadeus", fetched_at=NOW, expires_at=NOW + timedelta(seconds=30)
    )
    assert stamp.freshness_at(NOW + timedelta(minutes=1)) is PriceFreshness.STALE


def test_no_provenance_is_unknown_never_fresh():
    assert combine() is PriceFreshness.UNKNOWN
    assert combine(None, None) is PriceFreshness.UNKNOWN


def test_a_trip_is_only_as_current_as_its_oldest_quote():
    assert (
        combine(PriceFreshness.FRESH, PriceFreshness.STALE) is PriceFreshness.STALE
    )
    assert (
        combine(PriceFreshness.FRESH, PriceFreshness.RECENT) is PriceFreshness.RECENT
    )
    # Unknown is worse than recent and better than stale.
    assert combine(PriceFreshness.RECENT, PriceFreshness.UNKNOWN) is PriceFreshness.UNKNOWN
    assert combine(PriceFreshness.UNKNOWN, PriceFreshness.STALE) is PriceFreshness.STALE


def test_a_room_rate_can_drag_a_trips_freshness_down():
    """Freshness covers the whole trip, not just its flights.

    A three-hour-old room rate makes the itinerary stale even when every leg
    is current, because the price a traveler is being shown is the total.
    """
    stale_room = StaySummary(
        city="Vienna",
        arrival=NOW,
        departure=NOW + timedelta(days=2),
        nights=2,
        provenance=PriceProvenance(
            provider="stub", fetched_at=NOW - timedelta(hours=3)
        ),
    )
    fresh_leg = SimpleNamespace(
        provenance=PriceProvenance(provider="stub", fetched_at=NOW)
    )
    trip = SimpleNamespace(legs=[fresh_leg], stays=[stale_room])
    assert _freshness(trip, NOW) is PriceFreshness.STALE


def test_synthetic_prices_report_unknown_not_fresh(searched):
    """Fabricated prices have no provenance, and saying so is the honest render."""
    for trip in searched["recommendations"]:
        assert trip["price_freshness"] == "UNKNOWN"


def test_a_trivial_price_change_is_not_news():
    assert not PriceChange(previous=360.0, current=360.4).material
    assert PriceChange(previous=360.0, current=374.0).material


def test_a_price_change_says_the_right_thing():
    assert "cheaper" in PriceChange(previous=360.0, current=342.0).message()
    assert "increased" in PriceChange(previous=360.0, current=374.0).message()
    assert PriceChange(previous=360.0, current=360.0).message() is None


def test_an_uncertain_change_is_hedged():
    """"Do not overstate precision when provider data is uncertain."""
    hedged = PriceChange(previous=360.0, current=374.0, confident=False).message()
    assert "about" in hedged
    assert "increased by" not in hedged


# ---------------------------------------------------------------------------
# V5.8 - recommendation confidence
# ---------------------------------------------------------------------------
def test_a_degraded_search_can_never_be_high_confidence(searched):
    from detoura.models.itinerary import Itinerary

    planner_trip = Itinerary.model_construct(rank=1, stays=[], legs=[])
    quality = SearchQuality(completed=800, alternatives_returned=5, degraded=True, deep=True)
    verdict = assess(planner_trip, quality, freshness=PriceFreshness.FRESH)
    assert verdict.level is not ConfidenceLevel.HIGH
    assert any("did not respond" in reason.label for reason in verdict.reasons)


def test_a_dominated_trip_can_never_be_high_confidence():
    from detoura.models.itinerary import Itinerary

    trip = Itinerary.model_construct(rank=3, stays=[], legs=[])
    quality = SearchQuality(completed=900, alternatives_returned=5, deep=True)
    verdict = assess(trip, quality, freshness=PriceFreshness.FRESH, dominated=True)
    assert verdict.level is ConfidenceLevel.LIMITED


def test_confidence_always_explains_itself(searched):
    for trip in searched["recommendations"]:
        confidence = trip["confidence"]
        assert confidence["level"] in {"HIGH", "GOOD", "LIMITED"}
        assert confidence["label"]
        assert confidence["reasons"], "a level with no reasons is marketing"


def test_estimated_prices_are_held_against_the_confidence(searched):
    """Synthetic data has no live quotes, and the confidence panel says so."""
    for trip in searched["recommendations"]:
        labels = [r["label"] for r in trip["confidence"]["reasons"]]
        assert any("estimate" in label for label in labels)


# ---------------------------------------------------------------------------
# V5.9 - the product contract
# ---------------------------------------------------------------------------
def test_the_response_leaks_no_optimizer_internals(searched):
    """The coupling rule, asserted rather than trusted."""
    blob = repr(searched)
    for leak in (
        "beam_rounds",
        "beam_width",
        "states_generated",
        "pareto_kept",
        "value_breakdown",
        "score_breakdown",
        "adaptive",
    ):
        assert leak not in blob, leak


def test_a_recommendation_carries_what_the_ui_needs(searched):
    trip = searched["recommendations"][0]
    for field in (
        "id", "route", "cities", "total_price", "price_per_person", "currency",
        "costs", "usable_hours", "travel_hours", "travel_intensity",
        "intensity_band", "experience_score", "preference_match",
        "accommodation_score", "confidence", "price_freshness", "availability",
        "highlights", "stays", "legs", "destination_matches",
    ):
        assert field in trip, field


def test_prose_is_built_from_real_numbers(searched):
    trip = searched["recommendations"][0]
    assert trip["why_we_like_it"]
    assert f"{trip['usable_hours']:.0f}" in trip["why_we_like_it"]


def test_the_tradeoff_names_the_travelers_own_idea(searched):
    baseline = searched["baseline"]
    assert baseline is not None
    trip = searched["recommendations"][0]
    assert baseline["destination"] in trip["tradeoff"]


def test_the_baseline_reports_its_own_usable_time(searched):
    """Comparing against a baseline of zero hours would flatter every trip."""
    assert searched["baseline"]["usable_hours"] > 0


def test_availability_is_unknown_rather_than_invented(searched):
    """A provider that reports no inventory has not said "sold out"."""
    for trip in searched["recommendations"]:
        assert trip["availability"] == AvailabilityStatus.UNKNOWN.value


def test_intensity_is_banded_for_humans(searched):
    for trip in searched["recommendations"]:
        assert trip["intensity_band"] in {b.value for b in IntensityBand}


def test_a_healthy_search_reports_no_issues(searched):
    assert searched["issues"] == []
    assert searched["no_results"] is None


def test_the_canonical_demo_still_reproduces(searched):
    """Cologne, EUR 450, 2 travellers, 5 days, Best Value, culture/history/food.

    The exact scenario the V5 spec asks the finished product to demonstrate.
    """
    best = searched["recommendations"][0]
    assert best["cities"] == ["Munich", "Vienna"]
    assert best["total_price"] == pytest.approx(409.74)
    assert best["usable_hours"] == pytest.approx(57.8, abs=0.2)
    assert searched["baseline"]["destination"] == "Madrid"
    assert searched["baseline"]["total_price"] == pytest.approx(338.47)
    assert searched["baseline"]["extra_cities"] == 1


# ---------------------------------------------------------------------------
# Nothing matched vs. the search failing
# ---------------------------------------------------------------------------
def test_an_impossible_budget_gets_guidance_not_an_error(client):
    body = client.post("/api/v1/search", json={**PAYLOAD, "budget": 60}).json()
    assert body["recommendations"] == []
    assert body["issues"] == []
    guidance = body["no_results"]
    assert guidance is not None
    assert guidance["suggestions"], "nothing matched is actionable, not a dead end"


def test_the_guidance_says_how_far_off_the_budget_was(client):
    body = client.post("/api/v1/search", json={**PAYLOAD, "budget": 200}).json()
    guidance = body["no_results"]
    assert guidance is not None
    if guidance["closest_price"] is not None:
        assert guidance["closest_price"] > 200
        budget_bump = [s for s in guidance["suggestions"] if "budget" in s["label"]]
        assert budget_bump
        assert budget_bump[0]["patch"]["budget"] > 200


def test_every_suggestion_is_an_applicable_patch(client):
    """"Try a bigger budget" must be a button, not advice."""
    body = client.post("/api/v1/search", json={**PAYLOAD, "budget": 60}).json()
    for suggestion in body["no_results"]["suggestions"]:
        assert suggestion["patch"], suggestion["label"]
        assert suggestion["description"]


def test_an_unknown_origin_is_a_client_error_not_an_empty_result(client):
    response = client.post("/api/v1/search", json={**PAYLOAD, "origin": "Atlantis"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Supporting endpoints
# ---------------------------------------------------------------------------
def test_origins_report_what_it_costs_to_reach_them(client):
    body = client.get("/api/v1/origins/Köln").json()
    assert body["airports"]
    for airport in body["airports"]:
        assert airport["code"]
        assert airport["transfer_price"] is not None


def test_an_unknown_origin_lookup_is_a_404(client):
    assert client.get("/api/v1/origins/Atlantis").status_code == 404


def test_destinations_speak_in_interests_not_attributes(client):
    """V5.7: users read "culture", never "culture = 0.84"."""
    catalog = client.get("/api/v1/destinations").json()
    assert catalog
    for entry in catalog:
        assert isinstance(entry["strengths"], list)
        assert all(isinstance(name, str) for name in entry["strengths"])
        assert "attributes" not in entry


def test_search_modes_are_described_for_a_person(client):
    modes = client.get("/api/v1/search-modes").json()
    assert {m["name"] for m in modes} == {"QUICK", "SMART", "DEEP"}
    for mode in modes:
        assert mode["label"] and mode["description"]
        assert len(mode["estimated_seconds"]) == 2
    assert [m for m in modes if m["is_default"]][0]["name"] == "SMART"


def test_budget_sensitivity_reaches_the_product_api(client):
    body = client.post("/api/v1/budget-sensitivity?steps=4", json=PAYLOAD).json()
    assert len(body["steps"]) == 4
    for step in body["steps"]:
        assert {"budget", "feasible", "trips_found", "unlocks", "is_threshold"} <= set(step)


def test_the_engine_api_still_works(client):
    """V5 added a product API; it did not remove the engine's own."""
    body = client.post(
        "/plan-trip",
        json={
            "origin": "Köln", "budget": 450, "travelers": 2, "duration_days": 5,
            "date_from": "2026-09-10", "date_to": "2026-09-15",
            "transport_preferences": ["flight", "train"],
        },
    ).json()
    assert body["recommendations"]
    assert "value_breakdown" in body["recommendations"][0]


def test_health_names_the_product(client):
    assert client.get("/api/v1/health").json()["product"] == "Detoura"
