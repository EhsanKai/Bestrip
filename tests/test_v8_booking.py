"""V8 Phase 4: the booking flow — one confirmation, several tickets, a test pass.

The journey never appears booked while a required leg is not. The pass is
generated from real state, so it changes with the journey and the traveller.
No real payment anywhere. Nothing here makes a network call.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pytest

from detoura.models.booking import BookingState, PriceTolerance
from detoura.models.travel_pass import PassMode, PassStatus
from detoura.models.traveler import (
    Traveler,
    TravelerGender,
    TravelerParty,
    TravelerTitle,
)
from detoura.providers.duffel import DuffelOrderError, DuffelTransportProvider
from detoura.providers.http import HttpResponse
from detoura.services.booking_flow import (
    attach_travelers,
    build_travel_pass,
    create_run_demo,
    create_run_from_selection,
)
from detoura.services.booking_orchestrator import BookingPhase, run_booking
from detoura.services.journey_reference import is_journey_reference, new_journey_reference
from detoura.services.selection_store import SelectedOffer, Selection
from detoura.models.baggage import BaggageStatus

from . import duffel_fixtures as fx

DEP = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)


def _leg(o, d, price, **kw):
    return {
        "origin": o, "destination": d, "departure": DEP, "arrival": DEP + timedelta(hours=2),
        "carrier": kw.get("carrier", "XX"), "flight_number": kw.get("fn", "100"),
        "price_per_person": price, "cabin": kw.get("cabin", "included"),
        "checked": kw.get("checked", "included"),
    }


def _party(name=("Arman", "Example"), n=1):
    t = Traveler(given_name=name[0], family_name=name[1], born_on=date(1990, 5, 1),
                 email="a@example.com", phone="+441234567890",
                 gender=TravelerGender.MALE, title=TravelerTitle.MR)
    return TravelerParty(travelers=tuple(t for _ in range(n)))


def _run_demo(legs, *, total=None):
    return create_run_demo(
        trip_label="test", currency="EUR",
        discovered_total=total if total is not None else sum(l["price_per_person"] for l in legs),
        legs=legs,
    )


# ---------------------------------------------------------------------------
# Traveler model
# ---------------------------------------------------------------------------
def test_traveler_validates_and_does_not_expose_dob_in_the_public_summary():
    t = Traveler(given_name="Arman", family_name="Example", born_on=date(1990, 5, 1),
                 email="arman@example.com", phone="+49 170 1234567")
    assert t.full_name == "Arman Example"
    assert set(t.public_summary()) == {"name"}
    assert "1990" not in str(t.public_summary())


@pytest.mark.parametrize("bad", [
    {"email": "not-an-email"}, {"phone": "abc"}, {"given_name": "  "},
    {"nationality": "GERMANY"}, {"born_on": date(1500, 1, 1)},
])
def test_traveler_rejects_malformed_input(bad):
    base = dict(given_name="A", family_name="B", born_on=date(1990, 1, 1),
                email="a@b.com", phone="+441234567")
    base.update(bad)
    with pytest.raises(Exception):
        Traveler(**base)


def test_a_party_size_must_match_the_trip():
    run = _run_demo([_leg("CGN", "PRG", 90)])
    run.items[0].travelers = 2
    with pytest.raises(ValueError, match="2 traveller"):
        attach_travelers(run, _party(n=1))


# ---------------------------------------------------------------------------
# Journey reference
# ---------------------------------------------------------------------------
def test_journey_reference_is_server_shaped_and_carries_no_meaning():
    refs = {new_journey_reference() for _ in range(200)}
    assert len(refs) == 200
    for r in refs:
        assert r.startswith("DTR-V8-") and is_journey_reference(r)
        assert not any(c in r[7:] for c in "IO01")


def test_the_reference_is_set_at_run_creation_not_by_a_client():
    run = _run_demo([_leg("CGN", "PRG", 90)])
    assert is_journey_reference(run.journey_reference)


# ---------------------------------------------------------------------------
# Demo flow — happy path
# ---------------------------------------------------------------------------
def test_a_demo_journey_confirms_every_leg_and_the_pass_is_ready():
    run = _run_demo([_leg("CGN", "PRG", 95.8), _leg("PRG", "VIE", 79.2), _leg("VIE", "CGN", 111.18)])
    attach_travelers(run, _party())
    run_booking(run, duffel=None, sleep=lambda _s: None)
    assert run.phase is BookingPhase.COMPLETE
    assert all(i.state is BookingState.CONFIRMED for i in run.items)

    tp = build_travel_pass(run)
    assert tp.status is PassStatus.READY
    assert tp.mode is PassMode.DEMO_ONLY
    assert tp.route_cities == ("Cologne", "Prague", "Vienna", "Cologne")
    assert tp.traveler_name == "Arman Example"
    assert tp.tickets_prepared == 3
    assert tp.provider_order_ids == ()
    assert tp.disclaimer.payment_collected is False
    assert tp.disclaimer.valid_boarding_pass is False


def test_the_pass_changes_with_the_traveler_and_the_route():
    a = _run_demo([_leg("CGN", "BER", 60)])
    attach_travelers(a, _party(("Arman", "Example")))
    run_booking(a, duffel=None, sleep=lambda _s: None)

    b = _run_demo([_leg("CGN", "MAD", 120), _leg("MAD", "CGN", 130)])
    attach_travelers(b, _party(("Mina", "Traveller")))
    run_booking(b, duffel=None, sleep=lambda _s: None)

    pa, pb = build_travel_pass(a), build_travel_pass(b)
    assert pa.journey_reference != pb.journey_reference
    assert pa.traveler_name != pb.traveler_name
    assert pa.route_cities != pb.route_cities
    assert len(pa.tickets) == 1 and len(pb.tickets) == 2


# ---------------------------------------------------------------------------
# Partial failure — the invariant
# ---------------------------------------------------------------------------
class _DuffelLegScript:
    """Fake Duffel: leg offer ids map to ok / gone / order-refused."""

    def __init__(self, plan: dict):
        self.plan = plan

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        if "/air/orders" in url:
            import json
            oid = json.loads(body)["data"]["selected_offers"][0]
            if self.plan.get(oid) == "order_refused":
                return HttpResponse(status=422, body='{"errors":[{"code":"offer_no_longer_available","title":"gone"}]}')
            return HttpResponse(status=201, body=json.dumps({"data": {"id": f"ord_{oid}", "live_mode": False}}))
        # get_offer
        oid = url.rsplit("/air/offers/", 1)[1].split("?")[0]
        if self.plan.get(oid) == "gone":
            return HttpResponse(status=404, body='{"errors":[{"code":"not_found"}]}')
        o = fx.clone(fx.DIRECT)["data"]["offers"][0]
        o["id"] = oid
        o["expires_at"] = (DEP.replace(year=2030)).isoformat().replace("+00:00", "Z")
        return HttpResponse(status=200, body=__import__("json").dumps({"data": o}))


def _sandbox_run(plan: dict):
    offers = tuple(
        SelectedOffer(
            offer_id=oid, provider="duffel", origin="CGN", destination="BCN",
            leg_label=f"leg {i}", travelers=1, discovered_amount=142.51,
            discovered_currency="EUR", discovered_raw_amount="142.51",
            discovered_raw_currency="EUR",
            discovered_baggage_cabin=BaggageStatus.INCLUDED,
            discovered_baggage_checked=BaggageStatus.INCLUDED,
            discovered_departure=DEP, discovered_arrival=DEP + timedelta(hours=2),
        )
        for i, oid in enumerate(plan, start=1)
    )
    sel = Selection(selection_id="sel_x", recommendation_id="r", trip_label="t",
                    currency="EUR", discovered_total=142.51 * len(offers), offers=offers)
    run = create_run_from_selection(sel, tolerance=PriceTolerance(absolute=50.0))
    return run


def test_two_confirmed_and_one_refused_is_never_a_success():
    plan = {"off_A": "ok", "off_B": "ok", "off_C": "order_refused"}
    run = _sandbox_run(plan)
    attach_travelers(run, _party())
    duffel = DuffelTransportProvider(access_token="duffel_test_x",
                                     http_client=_DuffelLegScript(plan), max_calls=40)
    run_booking(run, duffel=duffel, sleep=lambda _s: None)

    assert run.phase is BookingPhase.PARTIAL_FAILURE
    states = [i.state for i in run.items]
    assert states[0] is BookingState.CONFIRMED
    assert states[1] is BookingState.CONFIRMED
    assert states[2] is BookingState.FAILED

    tp = build_travel_pass(run)
    assert tp.status is PassStatus.RECOVERY_REQUIRED
    assert tp.status is not PassStatus.READY
    assert tp.tickets_prepared == 2


def test_the_run_stops_after_the_first_failed_required_leg():
    plan = {"off_A": "ok", "off_B": "order_refused", "off_C": "ok"}
    run = _sandbox_run(plan)
    attach_travelers(run, _party())
    duffel = DuffelTransportProvider(access_token="duffel_test_x",
                                     http_client=_DuffelLegScript(plan), max_calls=40)
    run_booking(run, duffel=duffel, sleep=lambda _s: None)
    assert [i.state for i in run.items] == [
        BookingState.CONFIRMED, BookingState.FAILED, BookingState.NOT_ATTEMPTED,
    ]


def test_a_gone_offer_at_revalidation_fails_before_any_order():
    plan = {"off_A": "ok", "off_B": "gone", "off_C": "ok"}
    run = _sandbox_run(plan)
    attach_travelers(run, _party())
    duffel = DuffelTransportProvider(access_token="duffel_test_x",
                                     http_client=_DuffelLegScript(plan), max_calls=40)
    run_booking(run, duffel=duffel, sleep=lambda _s: None)
    assert run.phase is BookingPhase.FAILED
    assert run.items[1].state is BookingState.FAILED
    # nothing was ordered
    assert all(i.provider_order_id is None for i in run.items)


# ---------------------------------------------------------------------------
# create_test_order guards
# ---------------------------------------------------------------------------
def test_create_test_order_refuses_a_non_test_token():
    d = DuffelTransportProvider(access_token="duffel_live_x", allow_non_test_token=True,
                                http_client=_DuffelLegScript({}))
    with pytest.raises(Exception, match="not a test-mode token"):
        d.create_test_order("off_A", passengers=[], expected_amount="10", expected_currency="EUR")


def test_create_test_order_refuses_when_the_price_moved():
    class _Stub:
        def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
            import json
            o = fx.clone(fx.DIRECT)["data"]["offers"][0]
            o["id"] = "off_A"; o["total_amount"] = "999.00"
            return HttpResponse(status=200, body=json.dumps({"data": o}))
    d = DuffelTransportProvider(access_token="duffel_test_x", http_client=_Stub(), max_calls=8)
    with pytest.raises(DuffelOrderError, match="changed"):
        d.create_test_order("off_A", passengers=[], expected_amount="142.51", expected_currency="EUR")


# ---------------------------------------------------------------------------
# Endpoint + security
# ---------------------------------------------------------------------------
def _client():
    from fastapi.testclient import TestClient
    from detoura.api.app import create_app
    return TestClient(create_app())


def test_the_endpoints_generate_a_data_driven_demo_pass():
    c = _client()
    dep = (datetime.now() + timedelta(days=20)).replace(microsecond=0)
    legs = [{
        "origin": "CGN", "destination": "PRG", "departure": dep.isoformat(),
        "arrival": (dep + timedelta(hours=1)).isoformat(), "carrier": "OK",
        "flight_number": "1", "price_per_person": 95.8, "cabin": "included", "checked": "unknown",
    }]
    r = c.post("/api/v1/booking-intents", json={
        "demo_trip_label": "T", "demo_currency": "EUR", "demo_travelers": 1,
        "demo_legs": legs, "service_tier": "ALL_IN_ONE",
    })
    assert r.status_code == 201
    bid = r.json()["booking_id"]
    assert r.json()["journey_reference"].startswith("DTR-V8-")

    r = c.post(f"/api/v1/booking-intents/{bid}/travelers", json={"travelers": [{
        "given_name": "Sam", "family_name": "Rivera", "born_on": "1992-03-03",
        "email": "sam@example.com", "phone": "+34 600 000 000",
    }]})
    assert r.status_code == 200 and r.json()["phase"] == "awaiting_confirmation"

    c.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    for _ in range(60):
        time.sleep(0.3)
        s = c.get(f"/api/v1/booking-intents/{bid}").json()
        if s["pass_available"]:
            break
    p = c.get(f"/api/v1/booking-intents/{bid}/travel-pass").json()
    assert p["status"] == "ready" and p["mode"] == "demo_only"
    assert p["traveler_name"] == "Sam Rivera"
    assert p["route_cities"] == ["Cologne", "Prague"]
    assert "DEMO ONLY" in p["mode_note"]
    assert p["disclaimer"]["payment_collected"] is False


def test_the_pass_is_not_available_until_a_terminal_phase():
    c = _client()
    r = c.post("/api/v1/booking-intents", json={
        "demo_trip_label": "T", "demo_currency": "EUR", "demo_travelers": 1,
        "demo_legs": [{"origin": "CGN", "destination": "PRG",
                       "departure": "2026-11-01T08:00:00", "arrival": "2026-11-01T09:00:00",
                       "price_per_person": 90.0}],
    })
    bid = r.json()["booking_id"]
    assert c.get(f"/api/v1/booking-intents/{bid}/travel-pass").status_code == 409


def test_traveler_pii_never_appears_in_an_error_or_the_url():
    c = _client()
    r = c.post("/api/v1/booking-intents/bk_nope/travelers", json={"travelers": [{
        "given_name": "Secret", "family_name": "Person", "born_on": "1990-01-01",
        "email": "secret.person@example.com", "phone": "+10000000000",
    }]})
    assert r.status_code == 404
    assert "secret.person" not in r.text.lower()
    assert "Secret" not in r.text


def test_the_confirm_request_carries_no_price_or_status_the_client_could_forge():
    from detoura.api.contracts import ConfirmBookingRequest, CreateBookingIntentRequest
    assert set(ConfirmBookingRequest.model_fields) == {"tolerance_absolute", "tolerance_percentage"}
    # a create request cannot assert a Duffel order id or a booking status
    fields = set(CreateBookingIntentRequest.model_fields)
    assert "provider_order_id" not in fields and "status" not in fields
