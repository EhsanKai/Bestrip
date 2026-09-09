"""V7.6: the checks that must hold before V8 touches a real sandbox.

Deliberately small. V7.5 already carries 87 tests over this ground; these
close the specific gaps a preflight audit found rather than restating what is
already proven elsewhere:

* a fare quoted with an amount and no currency,
* the probe's cabin argument and its test-mode banner,
* the application starting with no Duffel token at all,
* and, end to end, that two party sizes cannot share an acquired snapshot.

The last is the one worth having. `AcquisitionEdge` inequality was already
tested, but inequality of a key is not the same claim as two real acquisitions
staying apart, and the cost of being wrong is quoting a family of four the
solo fare.
"""

from __future__ import annotations

from datetime import date

import pytest

from detoura.data.destinations import DESTINATIONS
from detoura.models.trip import TripRequest
from detoura.providers.duffel import DuffelTransportProvider
from detoura.providers.failures import ProviderFailureKind
from detoura.providers.transport import SyntheticTransportDataProvider
from detoura.services.acquisition import (
    ProviderCallBudget,
    SnapshotTransportProvider,
    acquire,
    build_plan,
    days_for_request,
)

from . import duffel_fixtures as fx

TOKEN = "duffel_test_notarealtoken"


def a_request(**kw) -> TripRequest:
    fields = dict(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=date(2026, 9, 10), date_to=date(2026, 9, 24),
    )
    fields.update(kw)
    return TripRequest(**fields)


def _snapshot_for(travelers: int):
    request = a_request(travelers=travelers)
    budget = ProviderCallBudget(max_offer_requests=40, max_destinations=3,
                                max_date_variants=2, max_airport_variants=1)
    days = days_for_request(request, request.candidate_start_dates(), max_days=2)
    plan = build_plan(request, destinations=DESTINATIONS, airports=["CGN"],
                      days=days, budget=budget)
    upstream = SyntheticTransportDataProvider()
    snapshot = acquire(plan, lambda e: upstream.search(e.origin, e.destination, e.day))
    return plan, snapshot


# ---------------------------------------------------------------------------
# Parser contract
# ---------------------------------------------------------------------------
def test_an_amount_without_a_currency_is_refused_not_assumed_to_be_euros():
    """The gap a preflight audit found: no fixture had one without the other.

    Defaulting to EUR here would be the same class of lie as relabelling USD -
    a number given the authority of a currency nobody stated.
    """
    duffel = DuffelTransportProvider(access_token=TOKEN)
    options = duffel.parse_offers(fx.MALFORMED_OFFERS, "CGN", "BCN")
    assert all(o.id != "duffel-off_nocurrency" for o in options)
    assert ("off_nocurrency", ProviderFailureKind.MALFORMED_RESPONSE) in duffel.offers_dropped


# ---------------------------------------------------------------------------
# Probe readiness for V8
# ---------------------------------------------------------------------------
def test_the_probe_states_the_mode_outright(monkeypatch, capsys):
    """Nobody reading this output later should have to infer from a prefix
    whether real money was in play."""
    from detoura.tools import duffel_probe

    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_live_x")
    duffel_probe.main(["--origin", "CGN", "--destination", "BCN",
                       "--date", "2026-10-15"])
    assert "not a test-mode token" in capsys.readouterr().out


def test_the_probe_accepts_a_cabin_and_passes_it_through(monkeypatch, capsys):
    import json as _json

    from detoura.providers import http as http_module
    from detoura.tools import duffel_probe

    sent: list[dict] = []

    class _Stub:
        def request(self, method, url, *, headers=None, params=None, body=None,
                    timeout=None):
            sent.append(_json.loads(body))
            return http_module.HttpResponse(
                status=200, body=_json.dumps(fx.DIRECT), headers={}
            )

    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_probe")
    monkeypatch.setattr(
        duffel_probe, "DuffelTransportProvider",
        lambda **kw: DuffelTransportProvider(
            access_token=kw["access_token"], http_client=_Stub()),
    )
    code = duffel_probe.main([
        "--origin", "CGN", "--destination", "BCN", "--date", "2026-10-15",
        "--travelers", "3", "--cabin", "business",
    ])
    output = capsys.readouterr().out
    assert code == duffel_probe.EXIT_OK
    assert "=== DUFFEL TEST MODE ===" in output
    assert sent and sent[0]["data"]["cabin_class"] == "business"
    assert len(sent[0]["data"]["passengers"]) == 3
    assert "No order was created." in output


def test_the_probe_rejects_an_unknown_cabin_rather_than_sending_it():
    from detoura.tools import duffel_probe

    with pytest.raises(SystemExit):
        duffel_probe.build_parser().parse_args([
            "--origin", "CGN", "--destination", "BCN",
            "--date", "2026-10-15", "--cabin", "spaceship",
        ])


# ---------------------------------------------------------------------------
# The application does not depend on a Duffel token
# ---------------------------------------------------------------------------
def test_the_app_starts_with_no_duffel_token(monkeypatch):
    """V7.6 must not make an unset token break ordinary startup."""
    from detoura.api.app import create_app

    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    app = create_app()
    assert app is not None and len(app.routes) > 0


def test_a_synthetic_search_needs_no_token(monkeypatch):
    from detoura.services.planner import TravelPlanner

    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    result = TravelPlanner().plan(a_request())
    assert result.recommendations


# ---------------------------------------------------------------------------
# Party size cannot leak across acquisitions
# ---------------------------------------------------------------------------
def test_two_party_sizes_cannot_share_an_acquired_snapshot():
    """End to end, not just key inequality.

    The failure this prevents is quoting a family of four the solo fare, which
    would be invisible in the result: a plausible number, silently for the
    wrong party.
    """
    plan_one, snap_one = _snapshot_for(1)
    plan_four, snap_four = _snapshot_for(4)

    assert plan_one.travelers == 1 and plan_four.travelers == 4
    assert set(snap_one.offers_by_edge).isdisjoint(snap_four.offers_by_edge), (
        "a one-traveller edge matched a four-traveller edge"
    )
    for edge in snap_one.offers_by_edge:
        assert edge.travelers == 1
    for edge in snap_four.offers_by_edge:
        assert edge.travelers == 4


def test_a_snapshot_serves_only_its_own_party():
    """A provider built for four must not answer from a one-traveller snapshot."""
    _, snap_one = _snapshot_for(1)
    served_for_four = SnapshotTransportProvider(snap_one, travelers=4)
    edge = next(iter(snap_one.offers_by_edge))
    assert served_for_four.search(edge.origin, edge.destination, edge.day) == []

    served_for_one = SnapshotTransportProvider(snap_one, travelers=1)
    assert served_for_one.search(edge.origin, edge.destination, edge.day) != []
