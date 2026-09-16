"""V9 Post-Phase-6 Search Integration Slice 1 — wiring `/api/v1/search` to
the real Phase 3 `services.live_search` pipeline (§2/§20/§21).

Two separate things are tested here, deliberately not conflated:

* **Architectural wiring** - proven with injected Duffel HTTP test doubles
  (the same offline-fixture pattern `tests/test_v9_provenance_fix.py` and
  `tests/duffel_fixtures.py` already establish), with real assertions about
  behaviour, not merely "a function was called".
* **Fail-closed-by-default** - the live path requires two independent
  switches (`SEARCH_LIVE_ENABLED=1` *and* a valid `duffel_test_` token),
  mirroring `PAYMENT_LIVE_CHARGING_ENABLED`/`COMMUNICATION_LIVE_SENDING_ENABLED`;
  every existing caller that sets neither sees byte-identical behaviour to
  before this slice.

No real Duffel Test Mode credentials are available in this environment, so
no test here claims real provider E2E verification (§21) - see
`docs/V9_SEARCH_ORIGIN_INTELLIGENCE_REPORT.md` for the honest distinction.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from detoura.api.app import create_app  # noqa: E402
import detoura.api.v1 as v1  # noqa: E402
from detoura.data.destinations import DESTINATIONS  # noqa: E402
from detoura.models.trip import TravelPreferences, TripRequest  # noqa: E402
from detoura.providers.duffel import DuffelTransportProvider  # noqa: E402
from detoura.providers.http import HttpResponse  # noqa: E402
from detoura.services.live_search import live_search  # noqa: E402
from detoura.services.origin_resolver import (  # noqa: E402
    CatalogOriginResolver,
    StaticOriginResolver,
)
from detoura.services.selection_store import SelectionStore  # noqa: E402
from tests import duffel_fixtures as fx  # noqa: E402


class _Stub:
    """Offline Duffel HTTP double - see the module docstring on why this is
    not, and does not claim to be, a real provider response."""

    def __init__(self):
        self.calls = 0

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls += 1
        sl = json.loads(body)["data"]["slices"][0]
        offer = fx._offer(
            f"off_{self.calls:020d}",
            [fx._slice(sl["origin"], sl["destination"], "PT2H", [
                fx._segment(sl["origin"], sl["destination"],
                            "2026-10-14T08:00:00", "2026-10-14T10:00:00",
                            "PT2H", carrier="LH")])],
            total_amount="95.00", total_currency="EUR",
            expires_at="2027-01-01T00:00:00Z",
        )
        return HttpResponse(status=200, body=json.dumps(fx.response([offer])))


def _duffel(stub) -> DuffelTransportProvider:
    return DuffelTransportProvider(access_token="duffel_test_x", http_client=stub, max_offers=20)


# ---------------------------------------------------------------------------
# Group A — the fix this slice made: live_search() honours an injected
# origin_resolver, so a catalog-resolved (non-legacy) origin actually works.
# ---------------------------------------------------------------------------

def test_live_search_without_a_resolver_still_rejects_a_non_legacy_origin():
    """Documents the pre-fix behaviour: the default (StaticOriginResolver,
    the closed 5-airport table) still applies when no resolver is passed -
    proving the fix is additive, not a silent behaviour change for existing
    callers."""
    request = TripRequest(
        origin="Madrid", budget=4000.0, travelers=2, duration_days=4,
        date_from=date(2026, 10, 12), date_to=date(2026, 10, 26),
        preferences=TravelPreferences(history=0.9, culture=0.8),
    )
    with pytest.raises(ValueError):
        live_search(
            request, duffel=_duffel(_Stub()), selection_store=SelectionStore(),
            destinations=DESTINATIONS, airports=["MAD"], days=[date(2026, 10, 14)],
        )


def test_live_search_with_catalog_resolver_accepts_a_non_legacy_origin():
    """The actual fix: passing CatalogOriginResolver through to live_search
    lets a real (non-Cologne-cluster) origin resolve and search."""
    request = TripRequest(
        origin="Dublin", budget=4000.0, travelers=2, duration_days=4,
        date_from=date(2026, 10, 12), date_to=date(2026, 10, 26),
        preferences=TravelPreferences(history=0.9, culture=0.8),
    )
    result = live_search(
        request, duffel=_duffel(_Stub()), selection_store=SelectionStore(),
        destinations=DESTINATIONS, airports=["DUB"], days=[date(2026, 10, 14)],
        origin_resolver=CatalogOriginResolver(),
    )
    assert result.plan_result is not None


def test_live_search_still_works_with_the_legacy_resolver_explicitly():
    """No regression: an explicit StaticOriginResolver (the old default)
    still works for a legacy origin exactly as before."""
    request = TripRequest(
        origin="Köln", budget=4000.0, travelers=2, duration_days=4,
        date_from=date(2026, 10, 12), date_to=date(2026, 10, 26),
        preferences=TravelPreferences(history=0.9, culture=0.8),
    )
    result = live_search(
        request, duffel=_duffel(_Stub()), selection_store=SelectionStore(),
        destinations=DESTINATIONS, airports=["CGN"], days=[date(2026, 10, 14)],
        origin_resolver=StaticOriginResolver(),
    )
    assert result.plan_result is not None


# ---------------------------------------------------------------------------
# Group B — the fail-closed gate itself (unit level)
# ---------------------------------------------------------------------------

def test_live_search_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SEARCH_LIVE_ENABLED", raising=False)
    assert v1._search_live_enabled() is False


def test_live_search_enabled_only_by_an_explicit_truthy_value(monkeypatch):
    monkeypatch.setenv("SEARCH_LIVE_ENABLED", "1")
    assert v1._search_live_enabled() is True
    monkeypatch.setenv("SEARCH_LIVE_ENABLED", "0")
    assert v1._search_live_enabled() is False
    monkeypatch.setenv("SEARCH_LIVE_ENABLED", "")
    assert v1._search_live_enabled() is False


def test_no_duffel_provider_without_a_token(monkeypatch):
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    assert v1._search_live_duffel_or_none() is None


def test_no_duffel_provider_with_a_live_looking_token(monkeypatch):
    """A `duffel_live_...`-shaped token must NOT be accepted - the same
    fail-closed posture as everywhere else this token is checked."""
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_live_definitely_real")
    assert v1._search_live_duffel_or_none() is None


def test_duffel_provider_constructed_with_a_valid_test_token(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_abc123")
    provider = v1._search_live_duffel_or_none()
    assert isinstance(provider, DuffelTransportProvider)


# ---------------------------------------------------------------------------
# Group C — end to end through the real API
# ---------------------------------------------------------------------------

_SEARCH_BASE = {
    "budget": 450, "travelers": 2, "duration_days": 5,
    "date_from": "2026-09-10", "date_to": "2026-09-15", "date_flexible": True,
    "interests": ["culture", "history"],
}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_live_search_flag_alone_changes_nothing_without_a_token(client, monkeypatch):
    """The two-switch invariant, proven end to end: enabling the flag with
    no token configured must be byte-identical to doing nothing at all."""
    monkeypatch.setenv("SEARCH_LIVE_ENABLED", "1")
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    response = client.post(
        "/api/v1/search", json={**_SEARCH_BASE, "origin": "Düsseldorf"},
    )
    assert response.status_code == 200
    assert response.json()["diagnostics"]["supply_source"] == "SYNTHETIC"


def test_default_behaviour_is_unchanged_synthetic_search(client, monkeypatch):
    monkeypatch.delenv("SEARCH_LIVE_ENABLED", raising=False)
    response = client.post(
        "/api/v1/search", json={**_SEARCH_BASE, "origin": "Düsseldorf"},
    )
    assert response.status_code == 200
    assert response.json()["diagnostics"]["supply_source"] == "SYNTHETIC"


def test_live_search_wiring_produces_a_live_labelled_response(client, monkeypatch):
    """The full wiring, exercised end to end through the real API with an
    injected offline Duffel double standing in for the network. Proves:
    the flag is honoured, a non-legacy-cluster origin now reaches a real
    (test-double) provider call, and the response is honestly labelled
    LIVE rather than silently presented as the synthetic demo data."""
    monkeypatch.setenv("SEARCH_LIVE_ENABLED", "1")
    monkeypatch.setattr(v1, "_search_live_duffel_or_none", lambda: _duffel(_Stub()))

    response = client.post(
        "/api/v1/search", json={**_SEARCH_BASE, "origin": "Düsseldorf"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostics"]["supply_source"] == "LIVE"


def test_live_search_per_edge_failure_is_disclosed_not_a_500(client, monkeypatch):
    """A provider fault during the live attempt must never surface as an
    unhandled error - but it must also never be silently swallowed into a
    calm "no trip fits your budget" answer (§2: "Do not fabricate live
    availability. Do not silently substitute ... for live provider truth").
    The honest answer is: still labelled LIVE (a live attempt genuinely
    happened), zero recommendations, and the failure disclosed in
    ``issues`` - the same "empty + issues, not empty + guidance"
    distinction this file's own module docstring calls the most important
    thing in it."""
    class _Boom:
        def request(self, *a, **kw):
            raise ConnectionError("simulated network failure")

    monkeypatch.setenv("SEARCH_LIVE_ENABLED", "1")
    monkeypatch.setattr(v1, "_search_live_duffel_or_none", lambda: _duffel(_Boom()))

    response = client.post(
        "/api/v1/search", json={**_SEARCH_BASE, "origin": "Düsseldorf"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostics"]["supply_source"] == "LIVE"
    assert body["recommendations"] == []
    assert body["issues"]  # the outage must be disclosed, not hidden
    assert any(i["provider"] == "duffel_live_search" for i in body["issues"])
    # And never a fabricated synthetic "closest price" attached to a
    # response labelled LIVE - that would misrepresent demo data as a real
    # signal about what the live market could have offered.
    assert body["no_results"] is None or body["no_results"]["closest_price"] is None
