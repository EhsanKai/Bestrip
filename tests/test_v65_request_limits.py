"""Request bounds (V6.5).

Every limit asserted here closes a hole that was demonstrated against a
running server, not hypothesised:

    200-year flexible window     -> HTTP 200 in 75.5s
    200k interests + 50k visited -> HTTP 200 in 26.7s
    37,500 recheck legs          -> HTTP 200, 37,500 uncached provider calls

The tests are written from both ends deliberately. A cap that only proves it
rejects is half a test: the value of these bounds is that they are far above
any real request, so each one also asserts that a generous, realistic request
still passes. A limit that quietly broke ordinary searches would be a worse
bug than the one it fixed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.api.app import create_app  # noqa: E402
from detoura.api.contracts import (  # noqa: E402
    MAX_DESTINATION_NAMES,
    MAX_INTERESTS,
    MAX_RECHECK_LEGS,
    MAX_SEARCH_WINDOW_DAYS,
)

BASE = {
    "origin": "Köln",
    "budget": 450,
    "travelers": 2,
    "duration_days": 5,
    "date_from": "2026-09-10",
    "date_to": "2026-09-15",
    "search_mode": "QUICK",
}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


def search(client, **overrides):
    return client.post("/api/v1/search", json={**BASE, **overrides})


# ---------------------------------------------------------------------------
# The search window
# ---------------------------------------------------------------------------
def test_an_absurd_date_window_is_refused(client):
    """The exact payload that held a server for 75 seconds."""
    response = search(client, date_from="1900-01-01", date_to="2100-01-01",
                      date_flexible=True)

    assert response.status_code == 422


def test_the_refusal_says_what_to_change(client):
    """A 422 that does not name the offending field is a shrug."""
    body = search(client, date_from="2026-01-01", date_to="2030-01-01").json()

    assert "window" in str(body).lower()


def test_a_year_of_flexibility_is_still_allowed(client):
    """The cap must not get in the way of planning well ahead."""
    response = search(client, date_from="2026-09-10", date_to="2027-09-01",
                      date_flexible=True)

    assert response.status_code == 200


def test_the_boundary_itself_is_inclusive(client):
    """Exactly at the limit is allowed; one day past it is not."""
    from datetime import date, timedelta

    start = date(2026, 9, 10)
    at = start + timedelta(days=MAX_SEARCH_WINDOW_DAYS - 1)
    over = start + timedelta(days=MAX_SEARCH_WINDOW_DAYS)

    assert search(client, date_from=str(start), date_to=str(at)).status_code == 200
    assert search(client, date_from=str(start), date_to=str(over)).status_code == 422


def test_an_unparseable_date_is_left_to_the_domain_model(client):
    """The window check must not shadow the existing date errors."""
    response = search(client, date_from="not-a-date", date_to="2026-09-15")

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# List fields
# ---------------------------------------------------------------------------
def test_an_enormous_interest_array_is_refused(client):
    """`previously_visited` was scanned linearly per destination insight."""
    assert search(client, interests=["culture"] * 200_000).status_code == 422


def test_an_enormous_visited_list_is_refused(client):
    assert search(client, previously_visited=["Rome"] * 50_000).status_code == 422


@pytest.mark.parametrize(
    "field,size",
    [
        ("interests", MAX_INTERESTS),
        ("disliked", MAX_INTERESTS),
        ("preferred_destinations", MAX_DESTINATION_NAMES),
        ("avoided_destinations", MAX_DESTINATION_NAMES),
        ("previously_visited", MAX_DESTINATION_NAMES),
    ],
)
def test_each_list_accepts_its_full_allowance(client, field, size):
    """Generous by design: the caps are structural, not product limits."""
    assert search(client, **{field: ["culture"] * size}).status_code == 200


@pytest.mark.parametrize(
    "field,size",
    [
        ("interests", MAX_INTERESTS + 1),
        ("preferred_destinations", MAX_DESTINATION_NAMES + 1),
        ("previously_visited", MAX_DESTINATION_NAMES + 1),
    ],
)
def test_one_past_the_allowance_is_refused(client, field, size):
    assert search(client, **{field: ["culture"] * size}).status_code == 422


def test_an_enormous_origin_string_is_refused(client):
    assert search(client, origin="x" * 10_000).status_code == 422


# ---------------------------------------------------------------------------
# Recheck fan-out
# ---------------------------------------------------------------------------
def _leg(i: int = 0) -> dict:
    return {
        "from": "CGN",
        "to": "Prague",
        "departure": "2026-09-10T09:00:00",
        "operator": "TestAir",
        "price_per_person": 60.0,
    }


def test_a_flood_of_recheck_legs_is_refused(client):
    """Each leg is one deliberately uncached upstream call.

    This is the endpoint that bypasses the provider cache on purpose, so an
    unbounded list here is the most expensive request in the API - and against
    a real billed provider, the most expensive in the literal sense.
    """
    body = {
        "trip_id": "t",
        "travelers": 2,
        "saved_price": 100.0,
        "legs": [_leg(i) for i in range(37_500)],
    }

    assert client.post("/api/v1/trips/recheck", json=body).status_code == 422


def test_a_realistic_itinerary_still_re_checks(client):
    """A trip is capped at six cities, so real leg counts are far below this."""
    body = {
        "trip_id": "t",
        "travelers": 2,
        "saved_price": 100.0,
        "legs": [_leg(i) for i in range(MAX_RECHECK_LEGS)],
    }

    assert client.post("/api/v1/trips/recheck", json=body).status_code == 200


def test_one_leg_past_the_cap_is_refused(client):
    body = {
        "trip_id": "t",
        "travelers": 2,
        "saved_price": 100.0,
        "legs": [_leg(i) for i in range(MAX_RECHECK_LEGS + 1)],
    }

    assert client.post("/api/v1/trips/recheck", json=body).status_code == 422
