"""Provider failure on the search path (V6.5).

Before this release the machinery here was a hollow shell. `api/v1.py` built a
`FailureLog`, never passed it to `plan()` (which had no parameter to receive
it), and the `Resilient*` decorators that are the only thing that writes to
such a log were never instantiated anywhere in `src/`. The consequences were
both of the failure modes the product docs say must never happen:

    a provider that raises  -> HTTP 500, discarding every leg that succeeded
    a provider that is down -> indistinguishable from "no trips matched"

The reason it shipped is the reason this file exists: every existing test
either exercised the decorators in isolation, or asserted the happy path
end-to-end. Nothing injected a failure through the actual HTTP endpoint.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.api.app import create_app  # noqa: E402
from detoura.api.v1 import get_planner  # noqa: E402
from detoura.providers.accommodation import SyntheticAccommodationDataProvider  # noqa: E402
from detoura.providers.transport import SyntheticTransportDataProvider  # noqa: E402
from detoura.services.planner import TravelPlanner  # noqa: E402

BODY = {
    "origin": "Köln",
    "budget": 450,
    "travelers": 2,
    "duration_days": 5,
    "date_from": "2026-09-10",
    "date_to": "2026-09-15",
    "search_mode": "QUICK",
}


class BrokenTransport:
    """A transport provider whose upstream is down."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or TimeoutError("upstream timed out")

    def search(self, origin: str, destination: str, departure_date: date):
        raise self.error


class FlakyAccommodation(SyntheticAccommodationDataProvider):
    """Real data, except one city that always fails."""

    def search(self, city: str, check_in, check_out, travelers: int):
        if city == "Prague":
            raise ConnectionError("accommodation upstream unreachable")
        return super().search(city, check_in, check_out, travelers)


def client_with(transport=None, accommodation=None) -> TestClient:
    app = create_app()
    planner = TravelPlanner(
        transport_provider=transport or SyntheticTransportDataProvider(),
        accommodation_provider=accommodation or SyntheticAccommodationDataProvider(),
    )
    app.dependency_overrides[get_planner] = lambda: planner
    return TestClient(app)


# ---------------------------------------------------------------------------
# The cardinal rule
# ---------------------------------------------------------------------------
def test_a_dead_transport_provider_does_not_return_500():
    """The failure this release exists to remove."""
    response = client_with(transport=BrokenTransport()).post("/api/v1/search", json=BODY)

    assert response.status_code == 200, response.text


def test_a_dead_provider_is_reported_as_an_issue_not_as_no_trips():
    """An outage and an empty market are different claims about the world."""
    body = client_with(transport=BrokenTransport()).post("/api/v1/search", json=BODY).json()

    assert body["issues"], "an infrastructure failure must reach the client"
    assert body["recommendations"] == []
    # The "nothing matched your budget" guidance must NOT be offered: we do
    # not know that nothing matched. We know we could not look.
    assert body["no_results"] is None


def test_the_reported_issue_is_typed_and_actionable():
    body = client_with(transport=BrokenTransport()).post("/api/v1/search", json=BODY).json()
    issue = body["issues"][0]

    assert issue["kind"] == "TIMEOUT"
    assert issue["provider"] == "transport"
    assert issue["retryable"] is True
    assert issue["message"]


@pytest.mark.parametrize(
    "error,kind",
    [
        (TimeoutError("timed out"), "TIMEOUT"),
        (ConnectionError("refused"), "UNAVAILABLE"),
    ],
)
def test_failure_kinds_are_distinguished(error, kind):
    """Different faults need different advice, so they stay different kinds."""
    body = client_with(transport=BrokenTransport(error)).post(
        "/api/v1/search", json=BODY
    ).json()

    assert body["issues"][0]["kind"] == kind


# ---------------------------------------------------------------------------
# Partial degradation
# ---------------------------------------------------------------------------
def test_one_broken_city_does_not_lose_the_whole_search():
    """Graceful degradation: the legs that worked are still worth showing."""
    body = client_with(accommodation=FlakyAccommodation()).post(
        "/api/v1/search", json=BODY
    ).json()

    assert body["recommendations"], "a partial outage must not empty the results"
    assert body["issues"], "but it must still be disclosed"
    assert body["issues"][0]["provider"] == "accommodation"


def test_a_degraded_search_never_recommends_the_broken_city():
    body = client_with(accommodation=FlakyAccommodation()).post(
        "/api/v1/search", json=BODY
    ).json()

    for trip in body["recommendations"]:
        assert "Prague" not in trip["cities"]


# ---------------------------------------------------------------------------
# The healthy path is unchanged
# ---------------------------------------------------------------------------
def test_a_healthy_search_still_reports_no_issues():
    body = client_with().post("/api/v1/search", json=BODY).json()

    assert body["issues"] == []
    assert body["recommendations"]


def test_failures_do_not_leak_between_requests():
    """The log is per-request; the planner is shared.

    A log stored on the planner would report the first caller's outage inside
    the second caller's answer, which is a worse lie than no reporting at all.
    """
    healthy = client_with()
    broken = client_with(transport=BrokenTransport())

    assert broken.post("/api/v1/search", json=BODY).json()["issues"]
    assert healthy.post("/api/v1/search", json=BODY).json()["issues"] == []
    # And again, in the other order.
    assert broken.post("/api/v1/search", json=BODY).json()["issues"]


def test_a_failure_is_not_cached_as_an_empty_answer():
    """Resilience wraps outside the cache, so a timeout is not memoised.

    Caching(Resilient(inner)) would store the empty list a degraded provider
    returns and serve it for the rest of the run. This asserts the ordering by
    its consequence: a provider that recovers is seen to recover.
    """
    transport = BrokenTransport()
    app_client = client_with(transport=transport)

    assert app_client.post("/api/v1/search", json=BODY).json()["issues"]

    # The upstream comes back.
    healthy = SyntheticTransportDataProvider()
    transport.search = healthy.search  # type: ignore[method-assign]

    recovered = app_client.post("/api/v1/search", json=BODY).json()
    assert recovered["recommendations"], "a recovered provider must produce trips"
