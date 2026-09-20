"""V9 Phase 6 — booking/provider execution security.

Two real, exploitable findings from a fresh adversarial pass over the
booking execution path (services/booking_flow.py,
services/booking_orchestrator.py), both fixed here with permanent
regression coverage:

1. **Concurrent double-execution** (`start_confirmation`): the phase guard
   ("can this run be confirmed right now?") was a plain read, and the only
   place that actually *claimed* the run - transitioning its phase out of
   AWAITING_CONFIRMATION/RECONFIRM_REQUIRED - was the background worker
   thread, once it got scheduled. Two callers racing `start_confirmation`
   (a double-click, a client retry overlapping the original request) could
   both pass the stale read before either worker thread ran, and both would
   then spawn a worker - two independent `run_booking` executions against
   the same legs, each capable of creating its own Duffel Order for the
   same offer. Reproduced directly: 300/300 barrier-synchronized trials
   double-invoked `run_booking` once the simulated work inside it was long
   enough to overlap the window (i.e. the shape of any real provider call).
   Fixed by claiming the phase atomically, under the run's own lock, in the
   *caller's* thread before any worker is spawned.

2. **Uncertain provider outcome collapsed into a definite failure**
   (`_issue_item`/`_revalidate_item`): the domain model already carries
   `BookingState.TIMEOUT` and `BookingState.PROVIDER_FAILURE`, kept
   deliberately apart from `FAILED` and routed toward `RECOVERY_REQUIRED`
   rather than plain failure (see `models/booking.py`'s
   `ALLOWED_TRANSITIONS`) - but the orchestrator never produced them,
   collapsing every non-success (including "the provider didn't respond
   and we don't know what happened") into a bare `FAILED`, which reads as
   "definitely nothing happened, safe to retry" - the one thing that isn't
   true for a timeout. Fixed by threading the actual resulting state
   through instead of a boolean. A related second-order bug was caught
   fixing this: `_mark_unattempted`'s old exemption list only protected
   `FAILED`/`CONFIRMED`, so a `TIMEOUT`/`PROVIDER_FAILURE` item would have
   been immediately overwritten right back to `NOT_ATTEMPTED` the moment it
   was recorded - also fixed.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from detoura.models.baggage import BaggageStatus
from detoura.models.booking import BookingState, PriceTolerance
from detoura.models.commercial import ServiceTier
from detoura.models.traveler import Traveler, TravelerGender, TravelerParty, TravelerTitle
from detoura.providers.duffel import DuffelTransportProvider
from detoura.providers.http import HttpResponse, ProviderHttpError
from detoura.services.booking_flow import (
    attach_travelers,
    create_run_demo,
    create_run_from_selection,
    start_confirmation,
)
from detoura.services.booking_orchestrator import (
    BookingPhase,
    _mark_unattempted,
    run_booking,
)
from detoura.services.selection_store import SelectedOffer, Selection

DEP = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)


def _party() -> TravelerParty:
    t = Traveler(given_name="Arman", family_name="Example", born_on=date(1990, 5, 1),
                 email="a@example.com", phone="+441234567890",
                 gender=TravelerGender.MALE, title=TravelerTitle.MR)
    return TravelerParty(travelers=(t,))


def _demo_run(n_legs: int = 1):
    legs = [{
        "origin": "CGN", "destination": "PRG", "departure": DEP, "arrival": DEP + timedelta(hours=2),
        "carrier": "XX", "flight_number": "100", "price_per_person": 95.0,
        "cabin": "included", "checked": "included",
    } for _ in range(n_legs)]
    run = create_run_demo(trip_label="test", currency="EUR", legs=legs)
    run.service_tier = ServiceTier.ALL_IN_ONE
    attach_travelers(run, _party())
    return run


class _DuffelBehaviorScript:
    """Fake Duffel HTTP transport: one behavior per offer id.

    ``"ok"`` succeeds normally; ``"gone"``/``"order_refused"`` are the
    existing pre-Phase-6 scripted outcomes (see tests/test_v8_booking.py);
    ``"timeout"``/``"conn_error"`` raise directly from the transport layer,
    simulating "the provider never answered" rather than "the provider
    answered with a rejection" - the distinction this slice's second fix is
    about.
    """

    def __init__(self, plan: dict, *, order_calls: list | None = None):
        self.plan = plan
        self.order_calls = order_calls if order_calls is not None else []

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        if "/air/orders" in url:
            oid = json.loads(body)["data"]["selected_offers"][0]
            self.order_calls.append(oid)
            behavior = self.plan.get(oid)
            if behavior == "order_refused":
                return HttpResponse(status=422, body='{"errors":[{"code":"offer_no_longer_available","title":"gone"}]}')
            if behavior == "timeout":
                raise TimeoutError("simulated provider timeout during order creation")
            if behavior == "conn_error":
                raise ProviderHttpError("simulated connection reset during order creation")
            return HttpResponse(status=201, body=json.dumps({"data": {"id": f"ord_{oid}", "live_mode": False}}))
        # GET offer (used by both revalidation and issuance)
        oid = url.rsplit("/air/offers/", 1)[1].split("?")[0]
        behavior = self.plan.get(oid)
        if behavior == "gone":
            return HttpResponse(status=404, body='{"errors":[{"code":"not_found"}]}')
        if behavior == "reval_timeout":
            raise TimeoutError("simulated provider timeout during revalidation")
        if behavior == "reval_conn_error":
            raise ProviderHttpError("simulated connection reset during revalidation")
        from . import duffel_fixtures as fx
        o = fx.clone(fx.DIRECT)["data"]["offers"][0]
        o["id"] = oid
        o["expires_at"] = DEP.replace(year=2030).isoformat().replace("+00:00", "Z")
        return HttpResponse(status=200, body=json.dumps({"data": o}))


_TERMINAL_PHASES = (
    BookingPhase.COMPLETE, BookingPhase.FAILED, BookingPhase.PARTIAL_FAILURE,
    BookingPhase.RECONFIRM_REQUIRED,
)


def _wait_for_terminal(run, timeout: float = 10.0) -> None:
    """Real pacing (_PACE) means run_booking takes real wall-clock seconds
    even in DEMO_ONLY mode - poll generously rather than guess a sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run.phase in _TERMINAL_PHASES:
            return
        time.sleep(0.05)


def _sandbox_run(plan: dict, *, tolerance: PriceTolerance | None = None):
    offers = tuple(
        SelectedOffer(
            # Same-country route (both DE) on purpose: start_confirmation's
            # travel-document check is orthogonal to what this file tests
            # and international routes (e.g. CGN->BCN, used elsewhere) would
            # require one this fake party doesn't carry.
            offer_id=oid, provider="duffel", origin="CGN", destination="FRA",
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
    run = create_run_from_selection(sel, tolerance=tolerance or PriceTolerance(absolute=50.0))
    run.service_tier = ServiceTier.ALL_IN_ONE
    attach_travelers(run, _party())
    return run


# ======================================================================
# Finding 1 — concurrent double-execution of start_confirmation
# ======================================================================
def test_concurrent_confirmation_executes_run_booking_exactly_once():
    """The core repro, as a permanent test: two threads racing
    start_confirmation() for the same run must result in exactly one
    execution, with the loser cleanly rejected - never both spawning a
    worker."""
    import detoura.services.booking_flow as bf

    run = _demo_run()
    call_count = {"n": 0}
    lock = threading.Lock()
    real_run_booking = run_booking

    def counting_run_booking(r, **kw):
        with lock:
            call_count["n"] += 1
        time.sleep(0.01)  # widen the window to the shape of a real provider call
        return real_run_booking(r, **kw)

    bf.run_booking = counting_run_booking
    try:
        barrier = threading.Barrier(2)
        errors: list[str] = []
        err_lock = threading.Lock()

        def caller():
            barrier.wait()
            try:
                start_confirmation(run)
            except ValueError as e:
                with err_lock:
                    errors.append(str(e))

        threads = [threading.Thread(target=caller) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        bf.run_booking = real_run_booking

    _wait_for_terminal(run)
    assert call_count["n"] == 1, "run_booking must execute exactly once per confirmation"
    assert len(errors) == 1
    assert "cannot confirm from phase" in errors[0]
    assert run.phase is BookingPhase.COMPLETE


def test_concurrent_confirmation_never_creates_two_provider_orders():
    """The financial version of the same race: with a real (fake) Duffel
    transport, concurrent confirmation must never result in two Order
    creation calls for the same offer."""
    plan = {"off_A": "ok"}
    run = _sandbox_run(plan)
    order_calls: list[str] = []
    duffel = DuffelTransportProvider(
        access_token="duffel_test_x",
        http_client=_DuffelBehaviorScript(plan, order_calls=order_calls),
        max_calls=40,
    )

    barrier = threading.Barrier(2)

    def caller():
        barrier.wait()
        try:
            start_confirmation(run, duffel_factory=lambda: duffel)
        except ValueError:
            pass

    threads = [threading.Thread(target=caller) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _wait_for_terminal(run)
    assert order_calls == ["off_A"], f"expected exactly one order call, got {order_calls}"


def test_second_confirm_after_success_is_rejected_not_reexecuted():
    run = _demo_run()
    start_confirmation(run)
    _wait_for_terminal(run)
    assert run.phase is BookingPhase.COMPLETE
    with pytest.raises(ValueError, match="cannot confirm from phase"):
        start_confirmation(run)


# ======================================================================
# Finding 2 — uncertain provider outcome must not read as a plain FAILED
# ======================================================================
def test_issuance_timeout_is_recorded_as_timeout_not_failed():
    plan = {"off_A": "timeout"}
    run = _sandbox_run(plan)
    duffel = DuffelTransportProvider(access_token="duffel_test_x",
                                     http_client=_DuffelBehaviorScript(plan), max_calls=40)
    run_booking(run, duffel=duffel, sleep=lambda _s: None)

    assert run.items[0].state is BookingState.TIMEOUT
    assert run.items[0].provider_order_id is None
    assert "cannot be confirmed" in run.items[0].detail
    # The journey-level truth is unaffected by which specific non-success
    # state occurred - still correctly not a success.
    assert run.phase is BookingPhase.FAILED


def test_issuance_connection_error_is_recorded_as_provider_failure_not_failed():
    plan = {"off_A": "conn_error"}
    run = _sandbox_run(plan)
    duffel = DuffelTransportProvider(access_token="duffel_test_x",
                                     http_client=_DuffelBehaviorScript(plan), max_calls=40)
    run_booking(run, duffel=duffel, sleep=lambda _s: None)

    assert run.items[0].state is BookingState.PROVIDER_FAILURE
    assert run.items[0].provider_order_id is None


def test_revalidation_timeout_on_a_required_leg_is_timeout_not_failed_and_stops_the_run():
    plan = {"off_A": "ok", "off_B": "reval_timeout", "off_C": "ok"}
    run = _sandbox_run(plan)
    duffel = DuffelTransportProvider(access_token="duffel_test_x",
                                     http_client=_DuffelBehaviorScript(plan), max_calls=40)
    run_booking(run, duffel=duffel, sleep=lambda _s: None)

    assert run.phase is BookingPhase.FAILED
    assert run.items[1].state is BookingState.TIMEOUT
    # Nothing was ordered - the run stopped before issuance ever started.
    assert all(i.provider_order_id is None for i in run.items)


def test_revalidation_connection_error_on_a_required_leg_is_provider_failure():
    plan = {"off_A": "ok", "off_B": "reval_conn_error", "off_C": "ok"}
    run = _sandbox_run(plan)
    duffel = DuffelTransportProvider(access_token="duffel_test_x",
                                     http_client=_DuffelBehaviorScript(plan), max_calls=40)
    run_booking(run, duffel=duffel, sleep=lambda _s: None)

    assert run.items[1].state is BookingState.PROVIDER_FAILURE


def test_mark_unattempted_preserves_timeout_and_provider_failure_but_downgrades_ready():
    """The second-order bug caught while fixing the first: _mark_unattempted
    must not overwrite an already-resolved TIMEOUT/PROVIDER_FAILURE/
    UNAVAILABLE/FAILED item - only a still-READY one (never issued because
    the run is stopping) becomes NOT_ATTEMPTED."""
    run = _demo_run(n_legs=4)
    run.items[0].state = BookingState.READY
    run.items[1].state = BookingState.TIMEOUT
    run.items[2].state = BookingState.PROVIDER_FAILURE
    run.items[3].state = BookingState.UNAVAILABLE

    _mark_unattempted(run)

    assert run.items[0].state is BookingState.NOT_ATTEMPTED
    assert run.items[1].state is BookingState.TIMEOUT
    assert run.items[2].state is BookingState.PROVIDER_FAILURE
    assert run.items[3].state is BookingState.UNAVAILABLE


def test_a_gone_offer_is_unavailable_not_failed():
    """The pre-existing, more common case (an outright-gone fare) still
    gets its own precise state, not folded into either FAILED or the new
    uncertain states - it's a *definite* fact, just not FAILED specifically."""
    plan = {"off_A": "gone"}
    run = _sandbox_run(plan)
    duffel = DuffelTransportProvider(access_token="duffel_test_x",
                                     http_client=_DuffelBehaviorScript(plan), max_calls=40)
    run_booking(run, duffel=duffel, sleep=lambda _s: None)
    assert run.items[0].state is BookingState.UNAVAILABLE


# ======================================================================
# Authorization boundary (Invariant 5): booking-intent creation/confirmation
# is capability-URL based by product design (booking_id is a 120-bit random
# token, never listable, never enumerable - the same model recheck's own
# docstring describes: "no account, no database, no session"), separate
# from My Trips' ownership-gated *visibility* (verified in the prior Phase 6
# ownership slice - see tests/test_v9_phase6_ownership_wiring.py). This
# proves that documented boundary holds exactly as designed, rather than
# assuming a stricter model the product never chose: another account that
# obtains the booking_id (the only way to act on any booking-intent, with
# or without an owner) can still execute it - by design - but ownership
# metadata is never inferred, spoofed, or reassigned by doing so, and
# My Trips visibility for the true owner is unaffected.
# ======================================================================
def test_booking_id_capability_authorizes_execution_by_design_ownership_is_unaffected(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import detoura.persistence.db as _db
    from detoura.api.app import create_app
    from detoura.persistence import accounts as store
    from detoura.persistence import get_db

    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "boundary.db"))
    monkeypatch.setattr(_db, "_DB", None)

    owner_client = TestClient(create_app())
    owner_client.post("/api/v1/auth/register", json={"email": "owner@example.com", "password": "correct horse battery"})
    r = owner_client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "correct horse battery"})
    owner_id = r.json()["user_id"]

    owner_csrf = owner_client.cookies.get("detoura_csrf")
    r = owner_client.post("/api/v1/booking-intents", headers={"X-CSRF-Token": owner_csrf}, json={
        "demo_trip_label": "T", "demo_currency": "EUR", "demo_travelers": 1,
        "demo_legs": [{
            "origin": "CGN", "destination": "PRG", "departure": "2026-11-01T08:00:00",
            "arrival": "2026-11-01T09:00:00", "carrier": "OK", "flight_number": "1",
            "price_per_person": 90.0, "cabin": "included", "checked": "unknown",
        }],
        "service_tier": "ALL_IN_ONE",
    })
    booking_id = r.json()["booking_id"]

    # A second, unrelated authenticated account - holding only the
    # booking_id, exactly what any anonymous holder of the same link would
    # have - can still drive this booking-intent forward (by product
    # design: no account/session is checked on this path at all).
    other_client = TestClient(create_app())
    other_client.post("/api/v1/auth/register", json={"email": "other@example.com", "password": "correct horse battery"})
    r = other_client.post("/api/v1/auth/login", json={"email": "other@example.com", "password": "correct horse battery"})
    other_id = r.json()["user_id"]

    r = other_client.post(f"/api/v1/booking-intents/{booking_id}/travelers", json={"travelers": [{
        "given_name": "Some", "family_name": "One", "born_on": "1990-01-01",
        "email": "someone@example.com", "phone": "+1 555 0100",
    }]})
    assert r.status_code == 200  # capability model: booking_id alone is sufficient, as designed

    # Crucially: none of that changed who *owns* the trip, and My Trips
    # visibility still belongs only to the original owner.
    db = get_db()
    assert store.get_trip_owner(db, booking_id) == owner_id
    assert booking_id not in store.list_trip_ids_for_user(db, other_id)
    r = other_client.get(f"/api/v1/me/trips/{booking_id}")
    assert r.status_code == 404  # the ownership-gated read model, unaffected by the shared capability


# ======================================================================
# Finding 3 (from independent review) — the ISSUING loop's `stop`-skip
# branch had the identical clobber bug _mark_unattempted was fixed for,
# at a sibling site the first pass of this slice didn't touch: an optional
# item already sitting at a resolved, uncertain-or-definite revalidation
# outcome could be overwritten to NOT_ATTEMPTED once a *later*, unrelated
# required leg failed at issuance.
# ======================================================================
@pytest.mark.parametrize("behavior,expected_state", [
    ("gone", BookingState.UNAVAILABLE),
    ("reval_timeout", BookingState.TIMEOUT),
    ("reval_conn_error", BookingState.PROVIDER_FAILURE),
])
def test_optional_items_resolved_state_survives_a_later_required_stop(behavior, expected_state):
    """off_A (required) issues fine; off_B (required) fails at issuance,
    setting `stop`; off_C (optional) already resolved to `expected_state`
    during *revalidation* (unaffected, since it's not required) and is
    reached via the issuing loop's `if stop:` skip branch on the next
    iteration - it must come out with its ORIGINAL state intact, not
    downgraded to NOT_ATTEMPTED."""
    plan = {"off_A": "ok", "off_B": "order_refused", "off_C": behavior}
    run = _sandbox_run(plan)
    run.items[2].required = False

    duffel = DuffelTransportProvider(access_token="duffel_test_x",
                                     http_client=_DuffelBehaviorScript(plan), max_calls=40)
    run_booking(run, duffel=duffel, sleep=lambda _s: None)

    assert run.items[0].state is BookingState.CONFIRMED  # off_A: unaffected
    assert run.items[1].state is BookingState.FAILED  # off_B: the required failure that set `stop`
    assert run.items[2].state is expected_state  # off_C: preserved, not NOT_ATTEMPTED
    assert run.items[2].provider_order_id is None


# ======================================================================
# Finding 4 (from independent review) — an optional item that did NOT pass
# revalidation must not be silently pushed through issuance anyway. Only
# a genuinely READY item may proceed to USER_CONFIRMED -> BOOKING ->
# provider issuance.
# ======================================================================
@pytest.mark.parametrize("behavior,expected_state", [
    ("gone", BookingState.UNAVAILABLE),
    ("reval_timeout", BookingState.TIMEOUT),
    ("reval_conn_error", BookingState.PROVIDER_FAILURE),
])
def test_optional_item_that_failed_revalidation_is_not_reattempted_at_issuance(behavior, expected_state):
    plan = {"off_A": "ok", "off_B": behavior}
    run = _sandbox_run(plan)
    run.items[1].required = False

    order_calls: list[str] = []
    duffel = DuffelTransportProvider(
        access_token="duffel_test_x",
        http_client=_DuffelBehaviorScript(plan, order_calls=order_calls),
        max_calls=40,
    )
    run_booking(run, duffel=duffel, sleep=lambda _s: None)

    # The optional leg's revalidation-time state/detail stands - untouched
    # by the issuing loop, and never given a second, independent attempt.
    assert run.items[1].state is expected_state
    assert run.items[1].provider_order_id is None
    assert "off_B" not in order_calls
    # The required leg was entirely unaffected and still confirmed normally.
    assert run.items[0].state is BookingState.CONFIRMED
    assert order_calls == ["off_A"]


def test_ready_optional_item_still_issues_normally():
    """The fix must not over-block: a genuinely READY optional item
    proceeds to issuance exactly as before."""
    plan = {"off_A": "ok", "off_B": "ok"}
    run = _sandbox_run(plan)
    run.items[1].required = False

    order_calls: list[str] = []
    duffel = DuffelTransportProvider(
        access_token="duffel_test_x",
        http_client=_DuffelBehaviorScript(plan, order_calls=order_calls),
        max_calls=40,
    )
    run_booking(run, duffel=duffel, sleep=lambda _s: None)

    assert run.items[1].state is BookingState.CONFIRMED
    assert run.items[1].provider_order_id == "ord_off_B"
    assert set(order_calls) == {"off_A", "off_B"}
    assert run.phase is BookingPhase.COMPLETE
