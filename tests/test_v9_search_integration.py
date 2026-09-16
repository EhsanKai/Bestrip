"""V9 Post-Phase-6 Search Integration + Origin Intelligence Slice 1 — the
consumer-facing API surface: origin suggestion/nearby endpoints, and the
non-Cologne-only behaviour of `/api/v1/search` and `/api/v1/origins/{query}`.

Covers §20's requirement to prove the consumer search endpoint actually
accepts and processes an arbitrary origin, not merely that a helper function
does. Live-provider wiring is exercised through injected test doubles only -
see the module docstring on `services.live_search` and V9 Phase 6's own
established pattern (no real Duffel credentials are available in this
environment; §21 forbids fabricating that they are).
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from detoura.api.app import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# GET /api/v1/origins/suggest
# ---------------------------------------------------------------------------

def test_suggest_prefix_query(client):
    body = client.get("/api/v1/origins/suggest", params={"q": "Duss"}).json()
    assert body["query"] == "Duss"
    names = [s["canonical_name"] for s in body["suggestions"]]
    assert "Dusseldorf" in names


def test_suggest_typo_query(client):
    body = client.get("/api/v1/origins/suggest", params={"q": "Dusseldrof"}).json()
    names = [s["canonical_name"] for s in body["suggestions"]]
    assert "Dusseldorf" in names
    hit = next(s for s in body["suggestions"] if s["canonical_name"] == "Dusseldorf")
    assert hit["match_type"] == "FUZZY"


def test_suggest_airport_code_ranks_first(client):
    body = client.get("/api/v1/origins/suggest", params={"q": "DUS"}).json()
    assert body["suggestions"][0]["match_type"] == "AIRPORT_CODE"
    assert body["suggestions"][0]["primary_airport"] == "DUS"


def test_suggest_empty_query_is_rejected(client):
    response = client.get("/api/v1/origins/suggest", params={"q": ""})
    assert response.status_code == 422


def test_suggest_missing_query_is_rejected(client):
    response = client.get("/api/v1/origins/suggest")
    assert response.status_code == 422


def test_suggest_oversized_query_is_rejected(client):
    response = client.get("/api/v1/origins/suggest", params={"q": "x" * 500})
    assert response.status_code == 422


def test_suggest_unrelated_nonsense_returns_no_results_not_an_error(client):
    response = client.get("/api/v1/origins/suggest", params={"q": "zzzzqqqqxxxx"})
    assert response.status_code == 200
    assert response.json()["suggestions"] == []


def test_suggest_result_limit_is_honoured(client):
    body = client.get(
        "/api/v1/origins/suggest", params={"q": "a", "limit": 3}
    ).json()
    assert len(body["suggestions"]) <= 3


def test_suggest_limit_above_the_cap_is_rejected(client):
    response = client.get(
        "/api/v1/origins/suggest", params={"q": "Berlin", "limit": 10_000}
    )
    assert response.status_code == 422


def test_suggest_is_deterministic(client):
    first = client.get("/api/v1/origins/suggest", params={"q": "Ber"}).json()
    second = client.get("/api/v1/origins/suggest", params={"q": "Ber"}).json()
    assert first == second


# ---------------------------------------------------------------------------
# GET /api/v1/origins/nearby
# ---------------------------------------------------------------------------

def test_nearby_valid_coordinates(client):
    # Düsseldorf's own coordinates - should surface DUS and CGN.
    body = client.get(
        "/api/v1/origins/nearby", params={"lat": 51.23, "lon": 6.78}
    ).json()
    codes = [a["code"] for a in body["airports"]]
    assert "DUS" in codes
    assert "CGN" in codes


def test_nearby_rejects_invalid_latitude(client):
    response = client.get("/api/v1/origins/nearby", params={"lat": 999, "lon": 0})
    assert response.status_code == 422


def test_nearby_rejects_invalid_longitude(client):
    response = client.get("/api/v1/origins/nearby", params={"lat": 0, "lon": -999})
    assert response.status_code == 422


def test_nearby_rejects_nan(client):
    response = client.get("/api/v1/origins/nearby", params={"lat": "nan", "lon": 0})
    assert response.status_code == 422


def test_nearby_rejects_infinity(client):
    response = client.get("/api/v1/origins/nearby", params={"lat": "inf", "lon": 0})
    assert response.status_code == 422


def test_nearby_radius_bound_is_enforced(client):
    response = client.get(
        "/api/v1/origins/nearby",
        params={"lat": 51.23, "lon": 6.78, "max_radius_km": 1_000_000},
    )
    assert response.status_code == 422


def test_nearby_candidate_limit_is_enforced(client):
    response = client.get(
        "/api/v1/origins/nearby",
        params={"lat": 51.23, "lon": 6.78, "limit": 10_000},
    )
    assert response.status_code == 422


def test_nearby_no_result_case_is_a_clean_empty_list(client):
    # The middle of the Atlantic - no catalog airport is nearby at any
    # sane radius.
    body = client.get(
        "/api/v1/origins/nearby",
        params={"lat": 30.0, "lon": -40.0, "max_radius_km": 50},
    ).json()
    assert body["airports"] == []


def test_nearby_distance_ordering(client):
    body = client.get(
        "/api/v1/origins/nearby",
        params={"lat": 51.23, "lon": 6.78, "max_radius_km": 500, "limit": 10},
    ).json()
    distances = [a["distance_km"] for a in body["airports"]]
    assert distances == sorted(distances)


# ---------------------------------------------------------------------------
# Non-Cologne origins through the real consumer search endpoint (§20)
# ---------------------------------------------------------------------------

_SEARCH_BASE = {
    "budget": 450, "travelers": 2, "duration_days": 5,
    "date_from": "2026-09-10", "date_to": "2026-09-15", "date_flexible": True,
    "interests": ["culture", "history"],
}


def test_dusseldorf_origin_is_accepted_by_search(client):
    """Düsseldorf was already one of the 5 legacy origin airports, so this
    is also a no-regression check: the catalog-backed resolver must still
    accept it and the hand-curated synthetic network must still answer it
    with real recommendations."""
    response = client.post(
        "/api/v1/search", json={**_SEARCH_BASE, "origin": "Düsseldorf"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "DUS" in body["origin_airports"]
    assert body["diagnostics"]["supply_source"] == "SYNTHETIC"


def test_koln_alias_origin_is_accepted_by_search(client):
    response = client.post(
        "/api/v1/search", json={**_SEARCH_BASE, "origin": "Köln"},
    )
    assert response.status_code == 200
    assert "CGN" in response.json()["origin_airports"]


def test_a_second_non_cologne_origin_is_accepted_not_rejected(client):
    """The product-problem-statement check: an origin far outside the old
    7-city/5-airport table must be *accepted* by the API (no 422) - proving
    origin resolution itself is no longer Cologne-centric - even though the
    synthetic demo network (a hand-curated fixture, unchanged this slice)
    has no timetable data for it, so recommendations are honestly empty
    without live search enabled."""
    response = client.post(
        "/api/v1/search", json={**_SEARCH_BASE, "origin": "Madrid"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["origin_airports"] == ["MAD"]
    # No fabricated recommendations - an honest, well-formed "no results"
    # answer, distinct from a 422/500.
    assert isinstance(body["recommendations"], list)


def test_previously_422ing_origin_is_no_longer_rejected(client):
    """Before this slice, any origin outside the closed 5-airport table was
    a hard 422 ('unknown origin'). Dublin (a real catalog city with real
    coordinates and an airport) must now resolve successfully."""
    response = client.post(
        "/api/v1/search", json={**_SEARCH_BASE, "origin": "Dublin"},
    )
    assert response.status_code == 200
    assert "DUB" in response.json()["origin_airports"]


def test_genuinely_unknown_origin_is_still_a_422(client):
    """Origin validation is real, not removed - an origin naming nothing in
    the catalog is still rejected, just with a wider catalog behind it."""
    response = client.post(
        "/api/v1/search", json={**_SEARCH_BASE, "origin": "Not A Real Place Zzz"},
    )
    assert response.status_code == 422


def test_typo_origin_in_search_is_rejected_not_silently_corrected(client):
    """§5 end to end: a typo must not silently redirect a search - the
    traveler must see a 422 (and use /origins/suggest to find the right
    spelling), never a silent substitution to a different city."""
    response = client.post(
        "/api/v1/search", json={**_SEARCH_BASE, "origin": "Dusseldrof"},
    )
    assert response.status_code == 422


def test_repeated_identical_search_is_deterministic(client):
    first = client.post(
        "/api/v1/search", json={**_SEARCH_BASE, "origin": "Düsseldorf"},
    ).json()
    second = client.post(
        "/api/v1/search", json={**_SEARCH_BASE, "origin": "Düsseldorf"},
    ).json()
    assert [r["cities"] for r in first["recommendations"]] == [
        r["cities"] for r in second["recommendations"]
    ]


# ---------------------------------------------------------------------------
# /api/v1/origins/{query} still works for the legacy cluster (no regression)
# ---------------------------------------------------------------------------

def test_legacy_origins_endpoint_still_answers_koln(client):
    body = client.get("/api/v1/origins/Köln").json()
    assert body["airports"]
    for airport in body["airports"]:
        assert airport["code"]


def test_origins_endpoint_now_answers_a_wider_catalog_city_too(client):
    body = client.get("/api/v1/origins/Dublin").json()
    assert body["airports"]
    codes = [a["code"] for a in body["airports"]]
    assert "DUB" in codes
