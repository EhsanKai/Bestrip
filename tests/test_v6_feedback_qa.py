"""Independent QA adversarial tests for V6 personalization (:mod:`detoura.services.feedback`).

Written by an independent reviewer, not the implementer - these deliberately
try to break the declared/observed split, the cumulative drift cap, session
isolation, and input validation rather than re-confirm the happy paths
already covered by :mod:`tests.test_v6_feedback`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from detoura.api.app import create_app  # noqa: E402
from detoura.profiles import COMPONENTS, PROFILES, ProfileName  # noqa: E402
from detoura.services.feedback import (  # noqa: E402
    MAX_DRIFT_PER_COMPONENT,
    reset_sessions,
)

BASELINE = PROFILES[ProfileName.BEST_VALUE].weights.normalized()

#: Extreme, adversarial trip profile: all the "mass" on one component, the
#: rest at exactly zero (the DTO's `ge=0.0` floor permits this) - chosen to
#: stress the `MIN_WEIGHT` floor inside `_cap_cumulative_drift` harder than
#: the existing suite's COST_HEAVY_TRIP (which keeps every component at 0.1).
ZERO_FLOOR_TRIP = {
    "cost": 1.0,
    "experience": 0.0,
    "preferences": 0.0,
    "time": 0.0,
    "diversity": 0.0,
    "city_count": 0.0,
    "accommodation": 0.0,
    "convenience": 0.0,
    "intensity": 0.0,
}

#: The tolerance the original suite already uses for the same cap assertion
#: (see test_v6_feedback.py's "+ 1e-6") - kept identical here rather than
#: invented fresh, since floating renormalization noise is expected, not a
#: bug, and the bound on how much is already established by that file.
DRIFT_TOLERANCE = 1e-6


@pytest.fixture(autouse=True)
def _clean_sessions():
    reset_sessions()
    yield
    reset_sessions()


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# 1. Cumulative drift cap under the harshest input the DTO allows
# ---------------------------------------------------------------------------
def test_drift_cap_holds_under_200_repeated_signals_with_zero_floor_components(client):
    """Repeated `booked` (the 1.5x-strength action) against a trip where most
    components sit exactly at zero pushes `_cap_cumulative_drift`'s MIN_WEIGHT
    floor and its renormalization step the hardest. The cap must still hold,
    on every component, for far more repetitions than a session would
    realistically see."""
    session_id = "qa-drift-zero-floor"
    body = None
    for _ in range(200):
        response = client.post(
            "/api/v1/feedback/trip-1",
            json={
                "session_id": session_id,
                "action": "booked",
                "value_breakdown": ZERO_FLOOR_TRIP,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()

    for name in COMPONENTS:
        drift = abs(body["observed"][name] - BASELINE[name])
        assert drift <= MAX_DRIFT_PER_COMPONENT + DRIFT_TOLERANCE, (
            f"{name} drifted {drift} past the cap of {MAX_DRIFT_PER_COMPONENT}"
        )


# ---------------------------------------------------------------------------
# 2. Session isolation: two explicit, distinct session_ids interleaved
# ---------------------------------------------------------------------------
def test_two_distinct_explicit_sessions_never_cross_contaminate_when_interleaved(client):
    """Not the same thing as the existing "two blank ids" test: this checks
    that *interleaved* calls under two different, caller-supplied session ids
    never leak signal_count or observed drift across the `_SESSIONS` dict."""
    cost_heavy = {
        "cost": 1.0, "experience": 0.1, "preferences": 0.1, "time": 0.1,
        "diversity": 0.1, "city_count": 0.1, "accommodation": 0.1,
        "convenience": 0.1, "intensity": 0.1,
    }

    def send(session_id: str, action: str) -> dict:
        response = client.post(
            "/api/v1/feedback/t1",
            json={"session_id": session_id, "action": action, "value_breakdown": cost_heavy},
        )
        assert response.status_code == 200, response.text
        return response.json()

    send("userA", "booked")
    b1 = send("userB", "disliked")
    a2 = send("userA", "booked")
    b2 = send("userB", "disliked")

    assert a2["signal_count"] == 2
    assert b2["signal_count"] == 2
    # A booked positive signal should push observed cost up; a disliked
    # negative signal should push it down. If the two sessions' state ever
    # aliased, one of these directions would flip or the counts would merge.
    assert a2["observed"]["cost"] > BASELINE["cost"]
    assert b2["observed"]["cost"] < BASELINE["cost"]
    assert b1["session_id"] != a2["session_id"]


# ---------------------------------------------------------------------------
# 3. Input validation: malformed value_breakdown must not corrupt a session
# ---------------------------------------------------------------------------
def test_negative_component_weight_is_rejected_and_leaves_no_session_behind(client):
    session_id = "qa-negative-weight"
    body = {
        "session_id": session_id,
        "action": "liked",
        "value_breakdown": {
            "cost": -1.0, "experience": 0.1, "preferences": 0.1, "time": 0.1,
            "diversity": 0.1, "city_count": 0.1, "accommodation": 0.1,
            "convenience": 0.1, "intensity": 0.1,
        },
    }
    response = client.post("/api/v1/feedback/trip-1", json=body)
    # Pydantic's `ge=0.0` on TripValueBreakdownDTO must reject this before it
    # ever reaches record_feedback - a 500 here would mean the DTO's
    # constraint isn't actually doing its job.
    assert response.status_code == 422

    # And it must not have created a half-initialized session as a side
    # effect of the rejected request.
    follow_up = client.post(
        "/api/v1/feedback/trip-1",
        json={
            "session_id": session_id,
            "action": "liked",
            "value_breakdown": {name: 0.1 for name in COMPONENTS},
        },
    )
    assert follow_up.status_code == 200, follow_up.text
    assert follow_up.json()["signal_count"] == 1


def test_all_zero_value_breakdown_is_a_422_not_a_500(client):
    """Every component at exactly 0.0 passes the DTO's `ge=0.0` field
    constraint but fails `TravelValueWeights`'s own "at least one weight must
    be positive" validator deeper inside `record_feedback`. That must surface
    as a client error, not an unhandled exception."""
    response = client.post(
        "/api/v1/feedback/trip-1",
        json={
            "session_id": "qa-all-zero",
            "action": "liked",
            "value_breakdown": {name: 0.0 for name in COMPONENTS},
        },
    )
    assert response.status_code == 422
    assert "qa-all-zero" not in __import__(
        "detoura.services.feedback", fromlist=["_SESSIONS"]
    )._SESSIONS
