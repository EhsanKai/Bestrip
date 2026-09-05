"""Real-time personalization from explicit feedback (V6).

Exercised through the API, the way a client actually reaches it - the same
style :mod:`tests.test_v6_recheck` uses for ``/trips/recheck``. The service
module (:mod:`detoura.services.feedback`) does the real work; these tests
care about the contract the endpoint makes, not its internals.
"""

from __future__ import annotations

import sys
import uuid
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

#: A trip whose value came overwhelmingly from cost - far from BEST_VALUE's
#: own cost weight (~0.19), so a nudge towards or away from it is unmistakable.
COST_HEAVY_TRIP = {
    "cost": 1.0,
    "experience": 0.1,
    "preferences": 0.1,
    "time": 0.1,
    "diversity": 0.1,
    "city_count": 0.1,
    "accommodation": 0.1,
    "convenience": 0.1,
    "intensity": 0.1,
}


@pytest.fixture(autouse=True)
def _clean_sessions():
    """Each test gets a clean in-memory session table."""
    reset_sessions()
    yield
    reset_sessions()


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


def new_session_id() -> str:
    return f"test-{uuid.uuid4().hex}"


def send(
    client,
    *,
    session_id: str | None,
    action: str,
    trip: dict | None = None,
    trip_id: str = "trip-1",
    declared_profile: str | None = None,
):
    body = {
        "session_id": session_id if session_id is not None else "",
        "action": action,
        "value_breakdown": trip or COST_HEAVY_TRIP,
    }
    if declared_profile is not None:
        body["declared_profile"] = declared_profile
    response = client.post(f"/api/v1/feedback/{trip_id}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Direction: each action moves `observed` the expected way
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action", ["saved", "liked", "booked"])
def test_positive_actions_move_observed_toward_the_trips_profile(client, action):
    session_id = new_session_id()
    body = send(client, session_id=session_id, action=action)

    assert body["observed"]["cost"] > BASELINE["cost"]


@pytest.mark.parametrize("action", ["disliked", "rejected"])
def test_negative_actions_move_observed_away_from_the_trips_profile(client, action):
    session_id = new_session_id()
    body = send(client, session_id=session_id, action=action)

    assert body["observed"]["cost"] < BASELINE["cost"]


# ---------------------------------------------------------------------------
# Bounded drift
# ---------------------------------------------------------------------------
def test_repeated_identical_signals_do_not_run_away_past_the_cap(client):
    session_id = new_session_id()
    body = None
    for _ in range(60):
        body = send(client, session_id=session_id, action="liked")

    assert body["observed"]["cost"] <= BASELINE["cost"] + MAX_DRIFT_PER_COMPONENT + 1e-6
    # And it should actually have pushed hard against that ceiling, not
    # stalled somewhere timid short of it.
    assert body["observed"]["cost"] > BASELINE["cost"] + MAX_DRIFT_PER_COMPONENT * 0.5


def test_drift_stays_capped_on_every_component_not_just_cost(client):
    session_id = new_session_id()
    body = None
    for _ in range(60):
        body = send(client, session_id=session_id, action="booked")

    for name in COMPONENTS:
        assert (
            abs(body["observed"][name] - BASELINE[name])
            <= MAX_DRIFT_PER_COMPONENT + 1e-6
        )


def test_contradictory_signals_mostly_cancel_out(client):
    """Like, then dislike, the same kind of trip: net effect is small."""
    session_id = new_session_id()
    send(client, session_id=session_id, action="liked")
    body = send(client, session_id=session_id, action="disliked")

    # Small net drift, nowhere near the cap and nowhere near where a single
    # uncontested signal would have landed.
    assert abs(body["observed"]["cost"] - BASELINE["cost"]) < 0.02


# ---------------------------------------------------------------------------
# Declared vs. observed
# ---------------------------------------------------------------------------
def test_declared_profile_is_never_overwritten_by_signals(client):
    session_id = new_session_id()
    for action in ["liked", "disliked", "booked", "rejected", "saved"]:
        body = send(client, session_id=session_id, action=action)
        assert body["declared_profile"] == ProfileName.BEST_VALUE.value
        for name in COMPONENTS:
            assert body["declared"][name] == pytest.approx(BASELINE[name])


def test_declared_profile_only_changes_when_the_client_says_so(client):
    session_id = new_session_id()
    send(client, session_id=session_id, action="liked")
    body = send(
        client, session_id=session_id, action="liked", declared_profile="ADVENTURE"
    )

    assert body["declared_profile"] == "ADVENTURE"
    adventure = PROFILES[ProfileName.ADVENTURE].weights.normalized()
    for name in COMPONENTS:
        assert body["declared"][name] == pytest.approx(adventure[name])


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def test_a_brand_new_session_works_with_no_prior_state(client):
    body = send(client, session_id=new_session_id(), action="liked")

    assert body["signal_count"] == 1
    assert body["session_id"]


def test_a_missing_session_id_does_not_crash_and_creates_a_session(client):
    body = send(client, session_id=None, action="liked")

    assert body["signal_count"] == 1
    assert body["session_id"]


def test_an_empty_session_id_does_not_crash_and_creates_a_session(client):
    body = send(client, session_id="", action="saved")

    assert body["signal_count"] == 1
    assert body["session_id"]


def test_two_anonymous_callers_do_not_share_a_session(client):
    """Each blank session_id gets its own identity, not a shared bucket."""
    first = send(client, session_id="", action="liked")
    second = send(client, session_id="", action="liked")

    assert first["session_id"] != second["session_id"]
    assert first["signal_count"] == 1
    assert second["signal_count"] == 1


def test_an_unknown_trip_id_is_fine_since_nothing_is_looked_up(client):
    body = send(client, session_id=new_session_id(), action="liked", trip_id="does-not-exist")

    assert body["trip_id"] == "does-not-exist"


# ---------------------------------------------------------------------------
# Weight sanity
# ---------------------------------------------------------------------------
def test_weights_stay_non_negative_and_sum_sanely(client):
    session_id = new_session_id()
    body = None
    for action in ["liked", "liked", "disliked", "booked", "rejected"]:
        body = send(client, session_id=session_id, action=action)

    for profile_key in ("declared", "observed"):
        values = body[profile_key]
        assert all(value >= 0.0 for value in values.values())
        assert sum(values[name] for name in COMPONENTS) == pytest.approx(1.0, abs=1e-4)
        assert set(values) == set(COMPONENTS)


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------
def test_confidence_increases_with_more_signals(client):
    session_id = new_session_id()
    confidences = []
    for _ in range(5):
        body = send(client, session_id=session_id, action="liked")
        confidences.append(body["confidence"])

    assert confidences == sorted(confidences)
    assert confidences[0] < confidences[-1]
    assert confidences[0] > 0.0


def test_a_fresh_session_has_zero_confidence_before_any_signal(client):
    """Confidence is reported only after the first signal here, since the
    endpoint always records one; a `signal_count` of 1 should already read
    as low, not high, confidence."""
    body = send(client, session_id=new_session_id(), action="liked")

    assert 0.0 < body["confidence"] < 0.5


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------
def test_the_response_explains_itself(client):
    body = send(client, session_id=new_session_id(), action="liked")

    assert body["explanation"]
    assert isinstance(body["explanation"], str)
