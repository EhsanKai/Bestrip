"""V9 Phase 6 Payment Security adversarial slice.

Fresh attack pass on the payment architecture built in V9 Phase 4 (closed/
approved) plus this slice's own fix: closing the ``run_paid_booking``
atomic single-execution-claim gap (see
``booking_orchestrator.claim_for_execution`` and ``run_booking``'s
``already_claimed`` parameter).

Deliberately does NOT re-test what ``tests/test_v9_phase4_*.py`` already
covers well (payment creation idempotency, amount/currency tampering at the
API, basic webhook signature/replay/dedup, CSRF, cross-user IDOR on
payments, ops gating, refund-amount bounds, reconciliation classification).
This file targets what a fresh adversarial pass over those same invariants,
plus the newly-closed ``run_paid_booking`` gap, still needed: real
multi-threaded races (not sequential CAS simulations) on capture/refund/
cancel, the booking-execution single-execution claim under concurrency
(both directly on ``run_booking`` and through ``run_paid_booking``),
webhook payload-trust and out-of-order/unknown-reference handling, and the
Stripe test/live guard exercised through the real ``resolve_provider``
configuration chain rather than only at the provider constructor.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, datetime, timezone

import pytest

from detoura.models.booking import BookingState, PriceTolerance
from detoura.models.commercial import CommercialQuote, PriceBreakdown, PricingPolicyRef, ServiceTier
from detoura.models.payment import PaymentStatus
from detoura.models.travel_pass import PassMode
from detoura.models.traveler import Traveler, TravelerParty
from detoura.persistence.db import Database
from detoura.providers.sandbox_payment import SandboxPaymentProvider
from detoura.services import payment_service as ps
from detoura.services.booking_orchestrator import (
    BookingPhase,
    BookingRun,
    ItemProgress,
    claim_for_execution,
    run_booking,
)
from detoura.services.payment_booking_orchestrator import run_paid_booking


# ======================================================================
# Shared fixtures
# ======================================================================
class FakeDuffel:
    """Same shape as the Phase 4 orchestrator suite's double, kept local so
    this file has no cross-file test coupling."""

    def __init__(self, *, fail_offer_ids: set[str] | None = None) -> None:
        self.fail_offer_ids = fail_offer_ids or set()
        self.order_calls: list[str] = []
        self._lock = threading.Lock()

    def revalidate_offer(self, offer_id, origin_airport, destination_airport, *, travelers):
        class _R:
            price_per_person = 100.0
            provider_ref = None
            operator = ""

        return _R()

    def get_offer(self, offer_id):
        return {"total_amount": "100.00", "total_currency": "EUR", "passengers": [{"id": "pas_0"}]}

    def create_test_order(self, offer_id, *, passengers, expected_amount, expected_currency):
        with self._lock:
            self.order_calls.append(offer_id)
        from detoura.providers.duffel import DuffelOrderError

        if offer_id in self.fail_offer_ids:
            raise DuffelOrderError("sandbox declined order (scripted)", code="ORDER_DECLINED")
        return {"id": f"ord_{offer_id}"}


def _traveler() -> Traveler:
    return Traveler(
        given_name="Ada", family_name="Lovelace", born_on=date(1990, 1, 1),
        email="ada@example.com", phone="+491511234567",
    )


def _item(item_id: str, offer_id: str, *, required: bool = True) -> ItemProgress:
    now = datetime.now(timezone.utc)
    return ItemProgress(
        item_id=item_id, origin_city="BER", origin_airport="BER",
        destination_city="LHR", destination_airport="LHR",
        departure=now, arrival=now, carrier="XY", flight_number="123",
        offer_id=offer_id, provider="duffel", travelers=1,
        quoted_price=100.0, currency="EUR", required=required,
    )


def _run(items: list[ItemProgress], *, booking_id="bk_pay6", total: float = 100.0,
          phase: BookingPhase = BookingPhase.AWAITING_CONFIRMATION) -> BookingRun:
    return BookingRun(
        booking_id=booking_id, journey_reference=f"jr_{booking_id}", mode=PassMode.SANDBOX_BOOKED,
        trip_label="test trip", route_cities=("BER", "LHR"), currency="EUR",
        discovered_total=total, tolerance=PriceTolerance(absolute=5.0, percentage=5.0),
        items=items, party=TravelerParty(travelers=(_traveler(),)), phase=phase,
    )


def _quote(total: float = 100.0) -> CommercialQuote:
    return CommercialQuote(
        service_tier=ServiceTier.BASIC,
        breakdown=PriceBreakdown(currency="EUR", supplier_transport=total),
        markup_policy=PricingPolicyRef(policy_id="default", version=1),
    )


def _setup() -> tuple[Database, SandboxPaymentProvider]:
    return Database(":memory:"), SandboxPaymentProvider()


def _authorized_payment(db, provider, *, total: float = 50.0, idem: str = "idem_base_1"):
    snap = ps.freeze_checkout_snapshot(
        db, booking_id="bk_base", journey_reference="jr_base", user_id=None,
        service_tier="BASIC", quote=_quote(total),
    )
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key=idem)
    return ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)


# ======================================================================
# Group A - the run_paid_booking / run_booking single-execution claim
# (V9 Phase 6 Payment Security Blocker 1 closure)
# ======================================================================
def test_direct_run_booking_calls_on_the_same_run_race_to_exactly_one_execution():
    """The primitive itself, isolated from payment: two threads calling
    ``run_booking`` directly (no ``already_claimed``) on the SAME run must
    result in exactly one execution - proving the claim is a real mutex, not
    merely a happy-path convention `start_confirmation` happens to follow."""
    duffel = FakeDuffel()
    run = _run([_item("leg-1", "off_1")])
    barrier = threading.Barrier(2)
    errors: list[Exception] = []
    completed = []

    def _attempt():
        try:
            barrier.wait(timeout=5)
            run_booking(run, duffel=duffel, sleep=lambda _s: None)
            completed.append(True)
        except ValueError as error:
            errors.append(error)

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(completed) == 1, "exactly one thread must complete run_booking"
    assert len(errors) == 1, "the loser must be rejected by the claim, not silently proceed"
    assert "cannot confirm from phase" in str(errors[0])
    assert duffel.order_calls == ["off_1"]  # never two provider orders for one leg


def test_run_booking_rejects_a_lying_already_claimed_caller():
    """A caller passing ``already_claimed=True`` without having actually
    taken the claim first must be rejected loudly, not trusted - the
    structural safety net for the one legitimate exception
    (`booking_flow.start_confirmation`) to the default auto-claim."""
    run = _run([_item("leg-1", "off_1")])
    assert run.phase is BookingPhase.AWAITING_CONFIRMATION  # NOT actually claimed (still not REVALIDATING)
    with pytest.raises(RuntimeError, match="did not actually take the single-execution claim"):
        run_booking(run, duffel=FakeDuffel(), already_claimed=True, sleep=lambda _s: None)


def test_run_paid_booking_concurrent_calls_same_run_result_in_exactly_one_booking_execution():
    """The end-to-end path: two concurrent `run_paid_booking` calls for the
    SAME run and the SAME idempotency_key (a realistic double-submit) must
    still produce exactly one supplier order and no unhandled exception -
    the loser gets a truthful, non-mutating outcome instead of crashing or
    silently double-booking."""
    db, provider = _setup()
    duffel = FakeDuffel()
    run = _run([_item("leg-1", "off_1")], booking_id="bk_race_paid")
    quote = _quote(100.0)

    barrier = threading.Barrier(2)
    outcomes = []
    errors: list[Exception] = []

    def _attempt():
        try:
            barrier.wait(timeout=5)
            outcome = run_paid_booking(
                db, run=run, quote=quote, provider=provider, provider_name=provider.name,
                idempotency_key="idem_race_paidbooking_1", duffel=duffel,
            )
            outcomes.append(outcome)
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"run_paid_booking must never raise an unhandled exception on a race: {errors}"
    assert len(outcomes) == 2
    assert duffel.order_calls == ["off_1"], "exactly one provider order, never two, for one confirmed leg"

    winners = [o for o in outcomes if o.booking_phase is BookingPhase.COMPLETE]
    losers = [o for o in outcomes if o.booking_phase is not BookingPhase.COMPLETE]
    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0].payment.status is PaymentStatus.CAPTURED
    assert losers[0].requires_ops_recovery is True
    assert "already claimed" in losers[0].summary

    # The final, persisted payment row reflects the WINNER's real outcome -
    # the loser never wrote to it (no torn/overwritten state from the race).
    final = ps.store.get_payment(db, winners[0].payment.payment_id)
    assert final.status is PaymentStatus.CAPTURED
    assert final.captured_amount == 100.0


def test_run_paid_booking_sequential_retry_after_success_does_not_reexecute_booking():
    """Same-run sequential retry (V9 Phase 6): calling `run_paid_booking`
    again, with the SAME idempotency_key, after the run already reached
    COMPLETE must never re-issue the leg - proven against a REAL retry, not
    just the concurrency case above."""
    db, provider = _setup()
    duffel = FakeDuffel()
    run = _run([_item("leg-1", "off_1")], booking_id="bk_retry_paid")
    quote = _quote(100.0)

    first = run_paid_booking(
        db, run=run, quote=quote, provider=provider, provider_name=provider.name,
        idempotency_key="idem_retry_paidbooking_1", duffel=duffel,
    )
    assert first.booking_phase is BookingPhase.COMPLETE
    assert duffel.order_calls == ["off_1"]

    # The payment itself is idempotent (`create_payment`/`authorize_payment`
    # both return the SAME already-CAPTURED row for this idempotency_key -
    # `authorize_payment`'s own no-op path never re-authorizes it). What
    # matters for THIS slice is the booking side: a retry must never
    # re-issue the already-confirmed leg. `run_paid_booking`'s own
    # pre-existing defensive guard ("any status but AUTHORIZED here is a
    # programming error") means a same-idempotency-key retry after success
    # surfaces as a loud RuntimeError rather than a graceful idempotent
    # return - a real, pre-existing (not introduced by this slice) rough
    # edge, tracked in the Phase 6 report rather than patched here (out of
    # this slice's bounded scope: the claim/booking-execution guarantee,
    # not payment-status-guard ergonomics). The safety property that DOES
    # matter is asserted regardless of which branch fires: no second order.
    try:
        second = run_paid_booking(
            db, run=run, quote=quote, provider=provider, provider_name=provider.name,
            idempotency_key="idem_retry_paidbooking_1", duffel=duffel,
        )
    except RuntimeError as error:
        assert "unexpected payment status before booking" in str(error)
    else:
        assert second.payment.payment_id == first.payment.payment_id
    assert duffel.order_calls == ["off_1"], "a sequential retry must never re-issue an already-confirmed leg"


def test_run_paid_booking_retry_after_pre_booking_failure_does_not_reexecute():
    """A run that failed before any supplier commitment (phase FAILED,
    authorization released) must not be silently re-executed by a later
    `run_paid_booking` call either - `claim_for_execution` only accepts
    AWAITING_CONFIRMATION/RECONFIRM_REQUIRED, and FAILED is neither."""
    db, provider = _setup()
    duffel = FakeDuffel(fail_offer_ids={"off_1"})
    run = _run([_item("leg-1", "off_1")], booking_id="bk_failretry_paid")
    quote = _quote(100.0)

    first = run_paid_booking(
        db, run=run, quote=quote, provider=provider, provider_name=provider.name,
        idempotency_key="idem_failretry_1", duffel=duffel,
    )
    assert run.phase is BookingPhase.FAILED
    assert first.payment.status is PaymentStatus.CANCELLED  # authorization released, no money captured

    second = run_paid_booking(
        db, run=run, quote=quote, provider=provider, provider_name=provider.name,
        idempotency_key="idem_failretry_2", duffel=duffel,
    )
    # A NEW payment is authorized (different idempotency_key - a genuine
    # fresh attempt), but the booking side must still refuse to re-execute
    # on a run that is not in a claimable phase; the fresh authorization is
    # held, never blindly booked against a dead run.
    assert second.payment.payment_id != first.payment.payment_id
    assert second.payment.status is PaymentStatus.AUTHORIZED
    assert second.requires_ops_recovery is True
    assert duffel.order_calls == ["off_1"], "still only the one (failed) order attempt ever made"


# ======================================================================
# Group B - real multi-threaded races on capture/refund/cancel
# ======================================================================
def test_concurrent_capture_race_results_in_exactly_one_real_capture():
    """Two threads, each simulating a separate HTTP request re-reading the
    payment fresh, call request_capture concurrently. Exactly one must
    actually capture; the other must fail cleanly (StaleVersion or an
    idempotent no-op), never double-capture."""
    db, provider = _setup()
    payment = _authorized_payment(db, provider, total=75.0, idem="idem_capturerace_1")

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def _attempt():
        try:
            barrier.wait(timeout=5)
            fresh = ps.store.get_payment(db, payment.payment_id)
            results.append(ps.request_capture(db, payment=fresh, provider=provider))
        except (ps.store.StaleVersion, ValueError) as error:
            # ValueError: found by this slice's independent adversarial
            # review - a "fresh" read can still land in the window where
            # the OTHER thread has already moved the payment to
            # CAPTURE_PENDING (mid-flight, not yet CAPTURED), which
            # request_capture's own leading guard rejects with a plain
            # ValueError, not StaleVersion. A safe rejection either way;
            # see test_ops_capture_converts_a_genuine_concurrent_capture_
            # race_to_a_clean_409_not_a_500 for the HTTP-layer fix this
            # finding prompted.
            errors.append(error)

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # Both may "succeed" at the Python level (request_capture has its own
    # idempotent no-op for an already-CAPTURED payment), OR the loser may
    # observe a StaleVersion - either is safe. What must NEVER happen is two
    # real captured amounts.
    final = ps.store.get_payment(db, payment.payment_id)
    assert final.status is PaymentStatus.CAPTURED
    assert final.captured_amount == 75.0
    # The sandbox provider's own idempotency-key cache means both racers,
    # if both reach the provider, present the SAME derived idempotency key
    # only when they read the SAME version - assert no corruption regardless
    # of which branch each thread took.
    for r in results:
        assert r.captured_amount in (0.0, 75.0)
    assert final.captured_amount == 75.0


def test_concurrent_refund_race_never_exceeds_captured_amount():
    """Two threads request a full refund of the SAME captured payment
    concurrently, each with its own idempotency_key (simulating two
    independent double-submitted requests, not one retried one). Exactly
    one must succeed; refunded_amount must never exceed captured_amount."""
    db, provider = _setup()
    payment = _authorized_payment(db, provider, total=40.0, idem="idem_refundrace_1")
    captured = ps.request_capture(db, payment=payment, provider=provider)
    assert captured.status is PaymentStatus.CAPTURED

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def _attempt(key: str):
        try:
            barrier.wait(timeout=5)
            fresh = ps.store.get_payment(db, captured.payment_id)
            results.append(ps.request_refund(
                db, payment=fresh, provider=provider, amount=40.0, reason="race",
                idempotency_key=key,
            ))
        except (ps.store.StaleVersion, ValueError) as error:
            # ValueError covers both `InvalidRefundAmount` (amount exceeds
            # what is left) and request_refund's plain "cannot refund from
            # status X" guard (the loser may observe the WINNER's refund
            # having already moved the payment to REFUNDED before this
            # thread's own call runs, if the race resolves sequentially
            # rather than truly overlapping - still a safe rejection, never
            # a second refund).
            errors.append(error)

    threads = [
        threading.Thread(target=_attempt, args=(k,))
        for k in ("idem_refundrace_a", "idem_refundrace_b")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    final = ps.store.get_payment(db, captured.payment_id)
    assert final.refunded_amount <= final.captured_amount + 0.005
    assert final.refunded_amount == 40.0, "exactly one refund of the full amount, never zero, never double"
    # The loser either hit StaleVersion (never applied), or its own
    # request_refund raised InvalidRefundAmount (nothing left to refund) -
    # never a second successful refund.
    successes = [
        (pay, rf) for (pay, rf) in results
        if rf.status.value == "SUCCEEDED"
    ]
    assert len(successes) == 1


def test_ops_capture_converts_a_genuine_concurrent_capture_race_to_a_clean_409_not_a_500(monkeypatch):
    """V9 Phase 6 Payment Security fix (independent-review finding): a
    genuinely concurrent capture request can move a payment's real stored
    status past `ops_capture`'s own pre-check in the narrow window between
    that read and `payment_service.request_capture`'s own call - which then
    raises a plain ``ValueError`` (e.g. "...cannot capture from status
    CAPTURE_PENDING"), not ``StaleVersion``. Reproduced under real
    multi-threaded load during this slice's adversarial review
    (test_concurrent_capture_race_results_in_exactly_one_real_capture above
    proves the money-safety invariant already holds regardless; this test
    proves the HTTP layer's own error handling for that same race class).
    Exercises the fix deterministically (forcing the exact exception)
    rather than depending on winning a real race window."""
    db, provider = _setup()
    payment = _authorized_payment(db, provider, total=20.0, idem="idem_opscapturerace_1")

    from fastapi import HTTPException

    from detoura.api import ops_payments

    def _raise_concurrent_capture(*args, **kwargs):
        raise ValueError(f"{payment.payment_id}: cannot capture from status CAPTURE_PENDING")

    monkeypatch.setattr(ops_payments, "get_db", lambda: db)
    monkeypatch.setattr(ops_payments, "resolve_provider", lambda: provider)
    monkeypatch.setattr(ops_payments.ps, "request_capture", _raise_concurrent_capture)

    with pytest.raises(HTTPException) as excinfo:
        ops_payments.ops_capture(payment.payment_id, actor="test-actor")
    assert excinfo.value.status_code == 409
    assert "CAPTURE_PENDING" in str(excinfo.value.detail)


def test_concurrent_capture_and_cancel_race_never_both_succeed():
    """One thread captures, another races to cancel the SAME authorization.
    The sandbox provider's own `already_captured` guard means these can
    never both truly succeed - assert the final state is coherent (captured
    XOR cancelled), never a corrupted mix."""
    db, provider = _setup()
    payment = _authorized_payment(db, provider, total=60.0, idem="idem_capcancelrace_1")

    barrier = threading.Barrier(2)
    outcomes = {}
    lock = threading.Lock()

    def _capture():
        barrier.wait(timeout=5)
        fresh = ps.store.get_payment(db, payment.payment_id)
        try:
            r = ps.request_capture(db, payment=fresh, provider=provider)
            with lock:
                outcomes["capture"] = r.status
        except (ps.store.StaleVersion, ValueError):
            # StaleVersion: lost the CAS race. Plain ValueError: by the time
            # this thread ran, the OTHER thread's cancel had already landed
            # (sequential-looking resolution of a race that still genuinely
            # overlapped at the barrier) - either way, a clean rejection,
            # never a capture on top of a cancelled authorization.
            with lock:
                outcomes["capture"] = "stale_or_invalid"

    def _cancel():
        barrier.wait(timeout=5)
        fresh = ps.store.get_payment(db, payment.payment_id)
        try:
            r = ps.cancel_authorization(db, payment=fresh, provider=provider)
            with lock:
                outcomes["cancel"] = r.status
        except (ps.store.StaleVersion, ValueError):
            with lock:
                outcomes["cancel"] = "stale_or_invalid"

    t1, t2 = threading.Thread(target=_capture), threading.Thread(target=_cancel)
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    final = ps.store.get_payment(db, payment.payment_id)
    # Never both: a payment cannot be simultaneously CAPTURED (money taken)
    # and CANCELLED (authorization released) - the domain's own transition
    # table already forbids this statically; this proves it holds under a
    # genuine race, not just sequential calls.
    assert final.status in (PaymentStatus.CAPTURED, PaymentStatus.CANCELLED, PaymentStatus.AUTHORIZED)
    assert not (final.status is PaymentStatus.CANCELLED and final.captured_amount > 0)


# ======================================================================
# Group C - webhook hardening: payload trust, unknown reference, ordering
# ======================================================================
def test_webhook_forged_captured_status_and_amount_in_payload_never_moves_real_state():
    """A webhook payload that CLAIMS a captured status/amount contradicting
    real provider truth must not corrupt local state - the handler only
    ever uses the payload to decide WHICH payment to re-check, then asks the
    provider itself (`retrieve`), never trusts the payload's own financial
    claims (§O)."""
    db, provider = _setup()
    payment = _authorized_payment(db, provider, total=90.0, idem="idem_webhookforge_1")
    assert payment.status is PaymentStatus.AUTHORIZED

    forged_payload = json.dumps({
        "id": "evt_forged_1", "type": "payment.updated",
        "provider_reference": payment.provider_payment_reference,
        "status": "captured",  # LIE: real sandbox truth is still "authorized"
        "amount": 999999,  # LIE: absurd amount, never read by the handler at all
        "currency": "XXX",
    }).encode()
    sig = SandboxPaymentProvider.sign(forged_payload)
    event = provider.verify_event(payload=forged_payload, signature=sig)
    assert event.status == "captured"  # the (untrusted) payload's own claim

    from detoura.persistence import payments as store
    claimed = store.claim_provider_event(
        db, provider="sandbox", provider_event_id=event.provider_event_id,
        payment_id=None, event_type=event.event_type, payload=event.payload,
    )
    assert claimed
    rows = db.query(
        "SELECT payment_id FROM payment_transactions WHERE provider_payment_reference=?",
        (payment.provider_payment_reference,),
    )
    assert rows
    reread = store.get_payment(db, rows[0]["payment_id"])
    updated, finding = ps.reconcile_payment(db, payment=reread, provider=provider)

    # Real provider truth (still just "authorized") wins - never the
    # forged "captured"/999999 the payload claimed.
    assert updated.status is PaymentStatus.AUTHORIZED
    assert updated.captured_amount == 0.0


def test_webhook_event_for_an_unrecognised_payment_reference_is_a_safe_noop():
    """An event whose provider_reference matches no local payment (a
    reference for a different environment/account, or simply garbage) must
    be claimed/deduped and processed without touching any payment or
    raising - never assumed to belong to something by guesswork."""
    db, provider = _setup()
    payload = json.dumps({
        "id": "evt_unknown_ref_1", "type": "payment.updated",
        "provider_reference": "sbx_ref_does_not_exist", "status": "captured",
    }).encode()
    sig = SandboxPaymentProvider.sign(payload)
    event = provider.verify_event(payload=payload, signature=sig)

    from detoura.persistence import payments as store
    claimed = store.claim_provider_event(
        db, provider="sandbox", provider_event_id=event.provider_event_id,
        payment_id=None, event_type=event.event_type, payload=event.payload,
    )
    assert claimed
    rows = db.query(
        "SELECT payment_id FROM payment_transactions WHERE provider_payment_reference=?",
        (event.provider_reference,),
    )
    assert rows == []  # nothing to reconcile - and nothing crashes because of that
    store.mark_provider_event_processed(db, provider="sandbox", provider_event_id=event.provider_event_id)


def test_out_of_order_stale_webhook_never_regresses_an_already_captured_payment():
    """A payment is already CAPTURED. A late/out-of-order event for the same
    reference arrives (a different event id - not a dup) restating a status
    the provider itself no longer reports. Reconciling against REAL current
    provider truth (still "captured") must leave the payment exactly as it
    was, never regress it."""
    db, provider = _setup()
    payment = _authorized_payment(db, provider, total=30.0, idem="idem_outoforder_1")
    captured = ps.request_capture(db, payment=payment, provider=provider)
    assert captured.status is PaymentStatus.CAPTURED

    stale_event_payload = json.dumps({
        "id": "evt_stale_1", "type": "payment.updated",
        "provider_reference": captured.provider_payment_reference, "status": "authorized",
    }).encode()
    sig = SandboxPaymentProvider.sign(stale_event_payload)
    event = provider.verify_event(payload=stale_event_payload, signature=sig)

    from detoura.persistence import payments as store
    store.claim_provider_event(
        db, provider="sandbox", provider_event_id=event.provider_event_id,
        payment_id=captured.payment_id, event_type=event.event_type, payload=event.payload,
    )
    # payments.py's webhook handler only reconciles for payments sitting in
    # a non-final in-flight status; CAPTURED is not one of them (see
    # api/payments.py's provider_webhook) - reconcile is never even
    # attempted for an already-settled payment on webhook receipt. Assert
    # that directly against real provider truth too, as defence in depth:
    # even if it WERE attempted, provider truth (still "captured") would
    # keep it there.
    reread = store.get_payment(db, captured.payment_id)
    assert reread.status is PaymentStatus.CAPTURED
    updated, finding = ps.reconcile_payment(db, payment=reread, provider=provider)
    assert updated.status is PaymentStatus.CAPTURED
    assert updated.captured_amount == 30.0


# ======================================================================
# Group D - Stripe test/live guard through the real resolve_provider chain
# ======================================================================
def test_resolve_provider_fails_closed_for_a_live_key_even_with_live_charging_enabled(monkeypatch):
    """The full configuration chain, not just the provider constructor in
    isolation: PAYMENT_PROVIDER=stripe + live charging enabled + a LIVE key
    with no explicit override must still refuse, never silently hand back a
    provider that could charge real money."""
    import detoura.payment_config as pc

    monkeypatch.setenv("PAYMENT_PROVIDER", "stripe")
    monkeypatch.setenv("PAYMENT_LIVE_CHARGING_ENABLED", "true")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_definitely_real_looking")
    pc.reset_payment_config()
    try:
        from detoura.providers.stripe_payment import StripeConfigurationError

        with pytest.raises(StripeConfigurationError):
            pc.resolve_provider()
    finally:
        pc.reset_payment_config()


def test_resolve_provider_fails_closed_when_stripe_selected_live_enabled_but_key_missing(monkeypatch):
    import detoura.payment_config as pc

    monkeypatch.setenv("PAYMENT_PROVIDER", "stripe")
    monkeypatch.setenv("PAYMENT_LIVE_CHARGING_ENABLED", "true")
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    pc.reset_payment_config()
    try:
        from detoura.providers.stripe_payment import StripeConfigurationError

        with pytest.raises(StripeConfigurationError):
            pc.resolve_provider()
    finally:
        pc.reset_payment_config()


def test_resolve_provider_with_live_charging_and_a_real_test_key_returns_stripe_test_mode(monkeypatch):
    """The only way to actually get a Stripe instance out of resolve_provider
    at all: BOTH the master kill switch AND a clearly-test-shaped key. Even
    then, it is unambiguously test mode."""
    import detoura.payment_config as pc
    from detoura.providers.stripe_payment import is_test_key

    monkeypatch.setenv("PAYMENT_PROVIDER", "stripe")
    monkeypatch.setenv("PAYMENT_LIVE_CHARGING_ENABLED", "true")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc123")
    pc.reset_payment_config()
    try:
        provider = pc.resolve_provider()
        assert provider.name == "stripe"
        assert is_test_key(provider.secret_key)
    finally:
        pc.reset_payment_config()


def test_resolve_provider_missing_provider_env_defaults_to_sandbox_never_stripe(monkeypatch):
    """No PAYMENT_PROVIDER set at all (a bare/misconfigured deployment) must
    default to the safe sandbox adapter, not silently attempt Stripe."""
    import detoura.payment_config as pc

    monkeypatch.delenv("PAYMENT_PROVIDER", raising=False)
    monkeypatch.delenv("PAYMENT_LIVE_CHARGING_ENABLED", raising=False)
    pc.reset_payment_config()
    try:
        provider = pc.resolve_provider()
        assert provider.name == "sandbox"
    finally:
        pc.reset_payment_config()
