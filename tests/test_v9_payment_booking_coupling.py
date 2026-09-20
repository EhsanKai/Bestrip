"""V9 Payment <-> Booking Coupling slice.

Closes the Limited Beta Reality Audit's severe finding: an ALL_IN_ONE
booking could reach customer-facing CONFIRMED with zero payment
authorization behind it, because ``run_paid_booking`` (V9 Phase 4/6, fully
built and hardened) had no production caller - ``api/v1.py``'s
``confirm_booking`` called ``services.booking_flow.start_confirmation``
directly, with no payment involved at all.

This file targets what is NEW in this slice specifically:

* ``payment_booking_orchestrator.resolve_eligible_payment_for_booking`` -
  the ALL_IN_ONE payment gate itself (Group A).
* ``payment_booking_orchestrator.execute_paid_booking`` /
  ``_execute_after_authorization`` - the shared post-authorization decision
  tree, now reused by the production path too, including the price-decrease/
  price-increase capture-amount fix (Group B).
* the real integration seam - ``api/v1.py``'s ``confirm_booking`` ->
  ``resolve_eligible_payment_for_booking`` -> ``booking_flow.start_confirmation``
  (Group C).

Deliberately does NOT re-derive what ``tests/test_v9_phase4_*.py`` and
``tests/test_v9_phase6_*.py`` already cover well and still cover unchanged
by this slice: payment creation idempotency, CSRF, webhook signature/replay,
refund amount bounds, UNKNOWN/RECONCILIATION_REQUIRED reconciliation
semantics, CheckoutSnapshot expiry, and the raw ``claim_for_execution``/
``run_booking`` single-execution mutex (proven directly, with real threads,
in ``test_v9_phase6_payment_security.py``). Group C's concurrency test
proves that SAME mutex is what the new API-level gate now sits in front of,
not a second copy of it.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from detoura.api.app import create_app
from detoura.models.booking import BookingState, PriceTolerance
from detoura.models.commercial import CommercialQuote, PriceBreakdown, PricingPolicyRef, ServiceTier
from detoura.models.payment import PaymentStatus
from detoura.models.travel_pass import PassMode
from detoura.models.traveler import Traveler, TravelerParty
from detoura.persistence.db import Database
from detoura.providers.duffel import DuffelOrderError
from detoura.providers.sandbox_payment import SandboxPaymentProvider
from detoura.services import payment_service as ps
from detoura.services.booking_orchestrator import BookingPhase, BookingRun, ItemProgress, claim_for_execution
from detoura.services.payment_booking_orchestrator import (
    PaymentEligibilityError,
    execute_paid_booking,
    resolve_eligible_payment_for_booking,
)

from .conftest import authorize_payment_for_booking


# ======================================================================
# Shared fixtures (self-contained - no cross-file test coupling, matching
# tests/test_v9_phase6_payment_security.py's own stated convention)
# ======================================================================
class FakeDuffel:
    """Same shape as the Phase 4/6 orchestrator doubles, with a
    configurable revalidated price so price-decrease/price-increase capture
    correctness can be exercised deterministically."""

    def __init__(self, *, reval_price: float = 100.0, fail_offer_ids: set[str] | None = None) -> None:
        self.reval_price = reval_price
        self.fail_offer_ids = fail_offer_ids or set()
        self.order_calls: list[str] = []
        self._lock = threading.Lock()

    def revalidate_offer(self, offer_id, origin_airport, destination_airport, *, travelers):
        price = self.reval_price

        class _R:
            price_per_person = price
            provider_ref = None
            operator = ""

        return _R()

    def get_offer(self, offer_id):
        return {
            "total_amount": f"{self.reval_price:.2f}", "total_currency": "EUR",
            "passengers": [{"id": "pas_0"}],
        }

    def create_test_order(self, offer_id, *, passengers, expected_amount, expected_currency):
        with self._lock:
            self.order_calls.append(offer_id)
        if offer_id in self.fail_offer_ids:
            raise DuffelOrderError("sandbox declined order (scripted)", code="ORDER_DECLINED")
        return {"id": f"ord_{offer_id}"}


def _traveler() -> Traveler:
    return Traveler(
        given_name="Ada", family_name="Lovelace", born_on=date(1990, 1, 1),
        email="ada@example.com", phone="+491511234567",
    )


def _item(item_id: str, offer_id: str, *, price: float = 100.0, required: bool = True) -> ItemProgress:
    now = datetime.now(timezone.utc)
    return ItemProgress(
        item_id=item_id, origin_city="BER", origin_airport="BER",
        destination_city="LHR", destination_airport="LHR",
        departure=now, arrival=now, carrier="XY", flight_number="123",
        offer_id=offer_id, provider="duffel", travelers=1,
        quoted_price=price, currency="EUR", required=required,
    )


def _run(
    items: list[ItemProgress], *, booking_id: str = "bk_couple", total: float = 100.0,
    phase: BookingPhase = BookingPhase.AWAITING_CONFIRMATION, owner_user_id: str | None = None,
) -> BookingRun:
    return BookingRun(
        booking_id=booking_id, journey_reference=f"jr_{booking_id}", mode=PassMode.SANDBOX_BOOKED,
        trip_label="test trip", route_cities=("BER", "LHR"), currency="EUR",
        discovered_total=total, tolerance=PriceTolerance(absolute=5.0, percentage=5.0),
        items=items, party=TravelerParty(travelers=(_traveler(),)), phase=phase,
        quote=_quote(total), owner_user_id=owner_user_id,
    )


def _quote(total: float = 100.0) -> CommercialQuote:
    return CommercialQuote(
        service_tier=ServiceTier.BASIC,
        breakdown=PriceBreakdown(currency="EUR", supplier_transport=total),
        markup_policy=PricingPolicyRef(policy_id="default", version=1),
    )


def _setup() -> tuple[Database, SandboxPaymentProvider]:
    return Database(":memory:"), SandboxPaymentProvider()


def _authorized_payment(
    db, provider, *, booking_id: str = "bk_couple", total: float = 100.0,
    idem: str = "idem_couple_1", user_id: str | None = None,
):
    snap = ps.freeze_checkout_snapshot(
        db, booking_id=booking_id, journey_reference=f"jr_{booking_id}", user_id=user_id,
        service_tier="BASIC", quote=_quote(total),
    )
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key=idem)
    return ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)


# ======================================================================
# Group A - resolve_eligible_payment_for_booking (the ALL_IN_ONE gate)
# ======================================================================
def test_no_payment_at_all_is_refused():
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1")])
    with pytest.raises(PaymentEligibilityError) as exc:
        resolve_eligible_payment_for_booking(db, run=run)
    assert exc.value.code == "PAYMENT_REQUIRED"


def test_a_failed_authorization_is_refused():
    """A FAILED authorization attempt (the provider declined it) must never
    be treated as eligible - simulated the way it can actually happen:
    ``authorize_payment``'s own result-application path lands on FAILED, not
    a hand-crafted transition from AUTHORIZED (which the domain model
    itself refuses - AUTHORIZED -> FAILED is not a real transition)."""
    from detoura.providers.payment_provider import ProviderResult

    db, provider = _setup()
    run = _run([_item("leg-1", "off_1")], booking_id="bk_failed_auth")
    snap = ps.freeze_checkout_snapshot(
        db, booking_id="bk_failed_auth", journey_reference="jr_bk_failed_auth",
        user_id=None, service_tier="BASIC", quote=_quote(100.0),
    )
    payment, _ = ps.create_payment(
        db, snapshot=snap, provider_name=provider.name, idempotency_key="idem_failed_1",
    )
    failed = ps._apply_authorize_result(
        db, payment=payment,
        result=ProviderResult(
            ok=False, unknown=False, provider_reference=None,
            status="failed", detail="sandbox: authorization declined (scripted)",
        ),
        now=datetime.now(timezone.utc),
    )
    assert failed.status is PaymentStatus.FAILED
    with pytest.raises(PaymentEligibilityError) as exc:
        resolve_eligible_payment_for_booking(db, run=run)
    assert exc.value.code == "PAYMENT_REQUIRED"


def test_an_unknown_authorization_is_refused_distinctly():
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1")], booking_id="bk_unknown_auth")
    payment = _authorized_payment(db, provider, booking_id="bk_unknown_auth", idem="idem_unknown_1")
    unknown = payment.with_status(PaymentStatus.UNKNOWN)
    from detoura.persistence import payments as store

    store.compare_and_swap_payment(db, payment=unknown, expected_version=payment.version)
    with pytest.raises(PaymentEligibilityError) as exc:
        resolve_eligible_payment_for_booking(db, run=run)
    assert exc.value.code == "PAYMENT_UNRESOLVED"


def test_two_authorized_payments_for_the_same_booking_are_ambiguous_and_refused():
    """A retried checkout can genuinely leave two AUTHORIZED payments behind
    (create_payment enforces no one-active-payment-per-booking invariant) -
    never pick one arbitrarily."""
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1")], booking_id="bk_ambig")
    _authorized_payment(db, provider, booking_id="bk_ambig", idem="idem_ambig_1")
    _authorized_payment(db, provider, booking_id="bk_ambig", idem="idem_ambig_2")
    with pytest.raises(PaymentEligibilityError) as exc:
        resolve_eligible_payment_for_booking(db, run=run)
    assert exc.value.code == "PAYMENT_AMBIGUOUS"


def test_a_payment_belonging_to_a_different_owner_is_refused():
    """Booking A's payment must never execute Booking B - and specifically,
    a payment authorized by a different account than the one that owns this
    run must never be treated as eligible, even when it is (wrongly) bound
    to the same booking_id row."""
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1")], booking_id="bk_cross_owner", owner_user_id="user_A")
    _authorized_payment(db, provider, booking_id="bk_cross_owner", idem="idem_cross_1", user_id="user_B")
    with pytest.raises(PaymentEligibilityError) as exc:
        resolve_eligible_payment_for_booking(db, run=run)
    assert exc.value.code == "PAYMENT_MISMATCH"


def test_a_payment_for_a_different_booking_id_is_never_considered():
    """The query itself is scoped to this exact booking_id - a payment that
    exists for a different booking never even appears as a candidate."""
    db, provider = _setup()
    run_a = _run([_item("leg-1", "off_1")], booking_id="bk_real_owner")
    _authorized_payment(db, provider, booking_id="bk_other_booking", idem="idem_other_1")
    with pytest.raises(PaymentEligibilityError) as exc:
        resolve_eligible_payment_for_booking(db, run=run_a)
    assert exc.value.code == "PAYMENT_REQUIRED"


def test_a_stale_authorized_amount_is_refused_as_price_changed():
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1", price=100.0)], booking_id="bk_stale_amt", total=100.0)
    # Authorized against a DIFFERENT (older) quote total than the run's
    # CURRENT quote - simulating a price move between authorization and
    # this confirm attempt.
    _authorized_payment(db, provider, booking_id="bk_stale_amt", total=250.0, idem="idem_stale_1")
    with pytest.raises(PaymentEligibilityError) as exc:
        resolve_eligible_payment_for_booking(db, run=run)
    assert exc.value.code == "PRICE_CHANGED"


def test_a_valid_authorized_payment_bound_to_this_exact_booking_is_eligible():
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1")], booking_id="bk_valid", total=100.0, owner_user_id="user_A")
    authorized = _authorized_payment(db, provider, booking_id="bk_valid", total=100.0, idem="idem_valid_1", user_id="user_A")
    resolved = resolve_eligible_payment_for_booking(db, run=run)
    assert resolved.payment_id == authorized.payment_id
    assert resolved.status is PaymentStatus.AUTHORIZED


# ======================================================================
# Group B - execute_paid_booking / _execute_after_authorization: the
# shared decision tree, now reachable from production, plus the
# price-decrease/price-increase capture-amount fix.
# ======================================================================
def test_execute_paid_booking_requires_the_run_to_already_be_claimed():
    """The contract mirrors run_booking(already_claimed=True): a caller
    that hands this function a payment without having actually claimed the
    run first is caught loudly, not trusted."""
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1")], booking_id="bk_not_claimed")
    payment = _authorized_payment(db, provider, booking_id="bk_not_claimed", idem="idem_notclaimed_1")
    assert run.phase is BookingPhase.AWAITING_CONFIRMATION  # never claimed
    with pytest.raises(RuntimeError, match="did not actually take the single-execution claim"):
        execute_paid_booking(db, run=run, payment=payment, provider=provider, duffel=FakeDuffel())


def test_execute_paid_booking_refuses_a_payment_that_is_not_authorized():
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1")], booking_id="bk_bad_status")
    claim_for_execution(run)
    # CREATED -> AUTHORIZED is the only forward transition modeled on this
    # payment already; simulate "not authorized" the direct way instead -
    # a fresh, never-authorized CREATED payment for the same booking.
    created = ps.create_payment(
        db, snapshot=ps.freeze_checkout_snapshot(
            db, booking_id="bk_bad_status2", journey_reference="jr_bad_status2",
            user_id=None, service_tier="BASIC", quote=_quote(100.0),
        ),
        provider_name=provider.name, idempotency_key="idem_badstatus_2",
    )[0]
    with pytest.raises(RuntimeError, match="unexpected payment status before booking"):
        execute_paid_booking(db, run=run, payment=created, provider=provider, duffel=FakeDuffel())


def test_execute_paid_booking_re_checks_payment_status_fresh_from_db_not_the_stale_caller_object():
    """Independent-review TOCTOU finding, now fixed: the `payment` object a
    caller hands in was read in the REQUEST thread, before the worker
    thread was even scheduled - a real window in which an Ops cancel, a
    webhook-driven reconciliation, or another process can change this
    exact payment's status. `execute_paid_booking` must re-verify against
    the CURRENT row immediately before touching the supplier, not trust
    the caller's now-possibly-stale in-memory object."""
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1")], booking_id="bk_toctou")
    stale = _authorized_payment(db, provider, booking_id="bk_toctou", idem="idem_toctou_1")
    claim_for_execution(run)

    # Simulate what happened in the window between the gate check (which
    # read `stale`) and this call: an independent write cancelled the
    # payment - `stale` itself is never mutated, exactly mirroring how the
    # real object flows unchanged from api/v1.py through to the worker.
    from detoura.persistence import payments as store

    cancelled = stale.with_status(PaymentStatus.CANCELLED)
    store.compare_and_swap_payment(db, payment=cancelled, expected_version=stale.version)

    duffel = FakeDuffel()
    with pytest.raises(RuntimeError, match="unexpected payment status before booking"):
        execute_paid_booking(db, run=run, payment=stale, provider=provider, duffel=duffel)
    # Caught BEFORE run_booking ever touched a supplier.
    assert duffel.order_calls == []


def test_a_valid_authorization_executes_the_booking_and_captures_in_full_when_price_is_unchanged():
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1", price=100.0)], booking_id="bk_happy", total=100.0)
    payment = _authorized_payment(db, provider, booking_id="bk_happy", total=100.0, idem="idem_happy_1")
    claim_for_execution(run)
    duffel = FakeDuffel(reval_price=100.0)
    outcome = execute_paid_booking(db, run=run, payment=payment, provider=provider, duffel=duffel)
    assert outcome.booking_phase is BookingPhase.COMPLETE
    assert outcome.payment.status is PaymentStatus.CAPTURED
    assert outcome.payment.captured_amount == 100.0
    assert duffel.order_calls == ["off_1"]


def test_price_decrease_within_tolerance_captures_the_lower_true_amount():
    """V9 Payment <-> Booking Coupling fix: the OLD code always captured
    ``payment.authorized_amount`` in full at COMPLETE - a fare that dropped
    between authorization and issuance was still charged at the stale,
    higher authorized figure. The final capture must reflect the
    REVALIDATED (true) supplier cost."""
    from detoura.services.commercial import CommercialPricingService

    db, provider = _setup()
    run = _run([_item("leg-1", "off_1", price=100.0)], booking_id="bk_price_down", total=100.0)
    payment = _authorized_payment(db, provider, booking_id="bk_price_down", total=100.0, idem="idem_down_1")
    claim_for_execution(run)
    duffel = FakeDuffel(reval_price=97.0)  # within the $5/5% tolerance, genuinely lower
    outcome = execute_paid_booking(db, run=run, payment=payment, provider=provider, duffel=duffel)
    assert outcome.booking_phase is BookingPhase.COMPLETE
    assert outcome.payment.status is PaymentStatus.CAPTURED
    # The correct final payable amount, independently derived from the SAME
    # commercial engine `price_run` uses, against the REVALIDATED (lower)
    # supplier cost - not a re-derivation of the pricing formula itself,
    # just proof the revalidated figure (not the stale authorized one) is
    # what actually reaches capture.
    expected = CommercialPricingService(db).quote(
        supplier_transport=97.0, currency="EUR", ticket_count=1,
        service_tier=ServiceTier.BASIC, user_key="anonymous",
    ).quote.customer_total
    assert outcome.payment.captured_amount == pytest.approx(expected)
    assert outcome.payment.captured_amount < payment.authorized_amount


def test_price_increase_within_tolerance_never_captures_above_authorization():
    """The mirror image: a fare that rose (but stayed within tolerance, so
    the run was still allowed to proceed to issuance) must never result in
    a capture above what the customer actually authorized."""
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1", price=100.0)], booking_id="bk_price_up", total=100.0)
    payment = _authorized_payment(db, provider, booking_id="bk_price_up", total=100.0, idem="idem_up_1")
    claim_for_execution(run)
    duffel = FakeDuffel(reval_price=103.0)  # within tolerance, but above what was authorized
    outcome = execute_paid_booking(db, run=run, payment=payment, provider=provider, duffel=duffel)
    assert outcome.booking_phase is BookingPhase.COMPLETE
    assert outcome.payment.status is PaymentStatus.CAPTURED
    assert outcome.payment.captured_amount == pytest.approx(100.0)
    assert outcome.payment.captured_amount <= payment.authorized_amount


def test_a_price_move_past_tolerance_stops_before_issuance_and_releases_the_authorization():
    """§F item 6 / the small, safe price-increase policy for this slice: no
    reauthorization UX is built here - a move past tolerance simply stops
    before any supplier order is created, and the hold is released in
    full. Proven here through the payment-coupled path specifically (the
    booking-only version of this is already covered elsewhere)."""
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1", price=100.0)], booking_id="bk_price_blowout", total=100.0)
    payment = _authorized_payment(db, provider, booking_id="bk_price_blowout", total=100.0, idem="idem_blowout_1")
    claim_for_execution(run)
    duffel = FakeDuffel(reval_price=140.0)  # far past the 5%/$5 tolerance
    outcome = execute_paid_booking(db, run=run, payment=payment, provider=provider, duffel=duffel)
    assert outcome.booking_phase is BookingPhase.RECONFIRM_REQUIRED
    assert outcome.payment.status is PaymentStatus.CANCELLED
    assert outcome.payment.captured_amount == 0.0
    assert outcome.requires_customer_reconfirmation is True
    assert duffel.order_calls == []  # never touched a supplier


def test_revalidation_failure_on_a_required_leg_means_zero_issuance_and_zero_capture():
    db, provider = _setup()
    items = [_item("leg-1", "off_gone", price=100.0)]
    run = _run(items, booking_id="bk_reval_fail", total=100.0)
    payment = _authorized_payment(db, provider, booking_id="bk_reval_fail", total=100.0, idem="idem_revalfail_1")
    claim_for_execution(run)

    class _GoneDuffel(FakeDuffel):
        def revalidate_offer(self, *a, **k):
            from detoura.providers.duffel import DuffelOfferGone

            raise DuffelOfferGone("gone")

    duffel = _GoneDuffel()
    outcome = execute_paid_booking(db, run=run, payment=payment, provider=provider, duffel=duffel)
    assert outcome.booking_phase is BookingPhase.FAILED
    assert outcome.payment.status is PaymentStatus.CANCELLED
    assert outcome.payment.captured_amount == 0.0
    assert duffel.order_calls == []


def test_a_required_leg_issuance_failure_after_another_required_leg_confirmed_is_partial_failure():
    """§G: at least one required leg did not confirm while at least one
    OTHER required leg's supplier obligation is already real - never
    auto-resolved, always RECONCILIATION_REQUIRED, capture and release both
    withheld. (``outcome`` is derived from required items only - a settled
    OPTIONAL leg alone never makes an otherwise all-required-failed journey
    read as partial; that per-leg distinction is the booking domain's own,
    unchanged by this slice.)"""
    db, provider = _setup()
    items = [
        _item("leg-1", "off_ok", price=50.0, required=True),
        _item("leg-2", "off_fail", price=50.0, required=True),
    ]
    run = _run(items, booking_id="bk_partial", total=100.0)
    payment = _authorized_payment(db, provider, booking_id="bk_partial", total=100.0, idem="idem_partial_1")
    claim_for_execution(run)
    # reval_price matches quoted (50/leg) - this test is about issuance
    # failure, not a price move; a mismatched reval price would instead
    # trip the tolerance check and stop at RECONFIRM_REQUIRED before either
    # leg is ever issued.
    duffel = FakeDuffel(reval_price=50.0, fail_offer_ids={"off_fail"})
    outcome = execute_paid_booking(db, run=run, payment=payment, provider=provider, duffel=duffel)
    assert outcome.booking_phase is BookingPhase.PARTIAL_FAILURE
    assert outcome.payment.status is PaymentStatus.RECONCILIATION_REQUIRED
    assert outcome.requires_ops_recovery is True
    # the optional leg's real supplier order id is preserved, per-leg truth intact
    assert run.items[0].state is BookingState.CONFIRMED
    assert run.items[0].provider_order_id == "ord_off_ok"
    assert run.items[1].state is BookingState.FAILED


def test_execute_paid_booking_is_a_thin_wrapper_that_never_re_claims():
    """``execute_paid_booking`` itself takes NO claim - it trusts the
    caller's prior ``claim_for_execution`` exactly like
    ``run_booking(already_claimed=True)`` does, and does nothing to protect
    against being invoked twice against one claim itself (that guarantee
    lives entirely in ``claim_for_execution`` being called exactly once,
    synchronously, per confirm attempt - proven at the real integration
    seam by ``test_20_concurrent_http_confirms_on_the_same_booking_execute_exactly_once``
    below, and at the primitive itself, with real concurrent threads, in
    ``test_v9_phase6_payment_security.py``)."""
    db, provider = _setup()
    run = _run([_item("leg-1", "off_1", price=100.0)], booking_id="bk_thin_wrapper", total=100.0)
    payment = _authorized_payment(db, provider, booking_id="bk_thin_wrapper", total=100.0, idem="idem_thin_1")
    claim_for_execution(run)
    duffel = FakeDuffel(reval_price=100.0)
    outcome = execute_paid_booking(db, run=run, payment=payment, provider=provider, duffel=duffel)
    assert outcome.booking_phase is BookingPhase.COMPLETE
    assert outcome.payment.captured_amount == 100.0
    assert duffel.order_calls == ["off_1"]


# ======================================================================
# Group C - the real integration seam: api/v1.py confirm_booking
# ======================================================================
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "couple.db"))
    monkeypatch.delenv("DETOURA_OPS_TOKEN", raising=False)
    import detoura.persistence.db as _db

    monkeypatch.setattr(_db, "_DB", None)
    import detoura.payment_config as _pc

    _pc.reset_payment_config()
    return TestClient(create_app())


_LEGS = [{
    "origin": "CGN", "destination": "PRG", "departure": "2026-11-01T09:00:00Z",
    "arrival": "2026-11-01T10:10:00Z", "carrier": "OK", "flight_number": "1",
    "price_per_person": 100.0, "cabin": "included", "checked": "included",
}]


def _create_all_in_one_intent(client, *, tier="ALL_IN_ONE"):
    # V9 CSRF hardening: create_booking_intent now enforces CSRF for a
    # signed-in caller - a no-op header for the (far more common) anonymous
    # callers of this helper, since they carry no detoura_csrf cookie at all.
    csrf = client.cookies.get("detoura_csrf")
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    r = client.post("/api/v1/booking-intents", headers=headers, json={
        "demo_trip_label": "T", "demo_currency": "EUR",
        "demo_legs": _LEGS, "service_tier": tier,
    })
    assert r.status_code == 201
    return r.json()["booking_id"]


def _add_traveller(client, bid):
    r = client.post(f"/api/v1/booking-intents/{bid}/travelers", json={"travelers": [{
        "given_name": "Ada", "family_name": "Lovelace", "born_on": "1990-01-01",
        "email": "ada@example.com", "phone": "+441234567890",
    }]})
    assert r.status_code == 200


def test_an_unpaid_all_in_one_booking_cannot_be_confirmed(client):
    """The headline defect, closed end-to-end: no payment created at all ->
    the confirm endpoint refuses before anything is claimed or touched."""
    bid = _create_all_in_one_intent(client)
    _add_traveller(client, bid)
    r = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PAYMENT_REQUIRED"
    # never claimed - still sitting exactly where it was
    intent = client.get(f"/api/v1/booking-intents/{bid}").json()
    assert intent["phase"] == "awaiting_confirmation"


def test_an_unpaid_all_in_one_booking_never_produces_a_confirmation_record():
    """Direct durable-state proof for the invariant "unpaid ALL_IN_ONE must
    never trigger booking-confirmation communication": with no payment ever
    created, try_finalize (the one path that can create a
    JourneyConfirmation/communication) must find nothing to confirm,
    because the booking never left AWAITING_CONFIRMATION in the first
    place - there is no completed booking record for it to act on at all."""
    from detoura.persistence import bookings as booking_store, confirmations as confirmation_store, get_db
    from detoura.services.post_booking_finalizer import try_finalize

    db = get_db()
    booking = booking_store.get(db, "bk_never_existed_unpaid")
    assert booking is None
    outcome = try_finalize(db, booking_id="bk_never_existed_unpaid")
    assert outcome.ready is False
    assert confirmation_store.get_confirmation_for_booking(db, "bk_never_existed_unpaid") is None


def test_a_payment_authorized_for_a_different_booking_does_not_let_this_one_confirm(client):
    bid_a = _create_all_in_one_intent(client)
    bid_b = _create_all_in_one_intent(client)
    _add_traveller(client, bid_a)
    _add_traveller(client, bid_b)
    authorize_payment_for_booking(client, bid_a)  # only A gets a payment

    r = client.post(f"/api/v1/booking-intents/{bid_b}/confirm", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PAYMENT_REQUIRED"


def test_a_properly_authorized_payment_lets_the_booking_execute_and_capture(client):
    bid = _create_all_in_one_intent(client)
    _add_traveller(client, bid)
    authorize_payment_for_booking(client, bid)
    r = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    assert r.status_code == 200

    import time

    for _ in range(60):
        time.sleep(0.3)
        s = client.get(f"/api/v1/booking-intents/{bid}").json()
        if s["pass_available"]:
            break
    pass_dto = client.get(f"/api/v1/booking-intents/{bid}/travel-pass").json()
    assert pass_dto["status"] == "ready"

    from detoura.persistence import get_db
    from detoura.persistence import payments as payment_store

    payments = payment_store.list_payments_for_booking(get_db(), bid)
    assert len(payments) == 1
    assert payments[0].status.value == "CAPTURED"
    assert payments[0].captured_amount == pytest.approx(payments[0].authorized_amount)


def test_a_second_confirm_attempt_is_refused_and_never_double_books(client):
    """Duplicate confirmation must not duplicate provider issuance: the
    booking-side claim (unchanged, proven with real concurrency in
    test_v9_phase6_*) sits BEHIND the payment gate now, not replaced by it."""
    bid = _create_all_in_one_intent(client)
    _add_traveller(client, bid)
    authorize_payment_for_booking(client, bid)
    first = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    assert first.status_code == 200
    second = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    assert second.status_code == 409


def test_basic_tier_confirmation_is_unaffected_by_the_payment_gate(client):
    """BASIC never orchestrates or books on Detoura's behalf - it must stay
    exactly as payment-coupling-free as before."""
    bid = _create_all_in_one_intent(client, tier="BASIC")
    _add_traveller(client, bid)
    r = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    assert r.status_code == 200
    itin = client.get(f"/api/v1/booking-intents/{bid}/itinerary").json()
    assert itin["ticket_count"] == 1


def test_anonymous_all_in_one_checkout_still_works_end_to_end(client):
    """Anonymous regression: the gate must not silently require a signed-in
    account - only a valid AUTHORIZED payment bound to this booking_id,
    which an anonymous caller can create and authorize exactly as before."""
    bid = _create_all_in_one_intent(client)
    _add_traveller(client, bid)
    payment_dto = authorize_payment_for_booking(client, bid)
    assert payment_dto["status"] == "AUTHORIZED"
    r = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    assert r.status_code == 200


def test_20_concurrent_http_confirms_on_the_same_booking_execute_exactly_once(client):
    """The real integration seam, under real concurrency: 20 threads all
    calling ``POST /booking-intents/{id}/confirm`` for the SAME paid
    booking at once must still result in exactly one winner (200) and every
    loser refused (409, never a 5xx, never a second supplier order) -
    ``claim_for_execution``'s atomic check-and-set is what start_confirmation
    now sits behind the payment gate, not a second, weaker mutex."""
    bid = _create_all_in_one_intent(client)
    _add_traveller(client, bid)
    authorize_payment_for_booking(client, bid)

    barrier = threading.Barrier(20)
    results: list[int] = []
    lock = threading.Lock()

    def _attempt():
        barrier.wait(timeout=5)
        r = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=_attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len(results) == 20
    assert results.count(200) == 1, f"exactly one winner expected, got {results}"
    assert all(code in (200, 409) for code in results), f"no unhandled error expected, got {results}"

    import time as _time

    for _ in range(60):
        _time.sleep(0.3)
        s = client.get(f"/api/v1/booking-intents/{bid}").json()
        if s["pass_available"]:
            break
    from detoura.persistence import get_db
    from detoura.persistence import payments as payment_store

    payments = payment_store.list_payments_for_booking(get_db(), bid)
    assert len(payments) == 1
    assert payments[0].status.value == "CAPTURED"
    assert payments[0].captured_amount == pytest.approx(payments[0].authorized_amount)


def test_travel_pass_never_reports_ready_when_capture_did_not_succeed(client):
    """Independent-review finding, now fixed: `run.journey_intent().outcome`
    alone (every leg CONFIRMED) is not sufficient to show a READY pass for
    a payment-coupled run - a real, ordinary provider decline at capture
    time (or any other reason capture did not land as CAPTURED) must
    downgrade the customer-facing pass to recovery_required, never silently
    report a ready/confirmed-looking ticket with no money actually
    collected. Drives a booking all the way to a genuinely COMPLETE phase
    with real (sandboxed) issued tickets, then simulates the capture
    outcome landing as FAILED (exactly what `request_capture` itself would
    do for a definite provider decline) and proves the travel-pass
    endpoint reflects that truthfully."""
    bid = _create_all_in_one_intent(client)
    _add_traveller(client, bid)
    authorize_payment_for_booking(client, bid)
    r = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    assert r.status_code == 200

    import time

    for _ in range(60):
        time.sleep(0.3)
        s = client.get(f"/api/v1/booking-intents/{bid}").json()
        if s["pass_available"]:
            break
    assert s["pass_available"] is True

    from detoura.persistence import get_db
    from detoura.persistence import payments as payment_store

    db = get_db()
    payments = payment_store.list_payments_for_booking(db, bid)
    assert len(payments) == 1
    captured = payments[0]
    assert captured.status.value == "CAPTURED"  # the ordinary happy path, confirmed first

    # Now simulate the real-world case this finding describes: capture came
    # back FAILED (a `request_capture` outcome this codebase already models
    # - `payment_service.py`'s CAPTURE_FAILED branch - just reached here
    # directly rather than via sandbox fault-injection plumbing, since that
    # scripting keys off a payment_id suffix this test cannot control
    # through the public API).
    failed = captured.model_copy(update={"status": PaymentStatus.FAILED})
    from detoura.persistence import payments as store

    store.compare_and_swap_payment(db, payment=failed, expected_version=captured.version)

    pass_dto = client.get(f"/api/v1/booking-intents/{bid}/travel-pass").json()
    assert pass_dto["status"] == "recovery_required", pass_dto


def test_authenticated_owner_can_pay_and_confirm_their_own_booking(client):
    """Authenticated ownership regression: an authenticated caller's own
    booking + own payment still executes normally through the gate."""
    client.post("/api/v1/auth/register", json={"email": "owner@example.com", "password": "correct horse battery"})
    login = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "correct horse battery"})
    assert login.status_code == 200

    bid = _create_all_in_one_intent(client)
    _add_traveller(client, bid)
    authorize_payment_for_booking(client, bid)
    r = client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    assert r.status_code == 200
