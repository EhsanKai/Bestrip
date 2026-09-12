"""V9 Phase 4 §A-§K, §X - the payment service: idempotency, refunds,
reconciliation, allocations, concurrency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from detoura.models.commercial import CommercialQuote, PriceBreakdown, PricingPolicyRef, ServiceTier
from detoura.models.payment import PaymentStatus, RefundStatus
from detoura.persistence.db import Database
from detoura.persistence import payments as store
from detoura.providers.sandbox_payment import SandboxPaymentProvider
from detoura.services import payment_service as ps


def _quote(total: float = 100.0, **kw) -> CommercialQuote:
    return CommercialQuote(
        service_tier=kw.pop("service_tier", ServiceTier.BASIC),
        breakdown=PriceBreakdown(currency="EUR", supplier_transport=total, **kw),
        markup_policy=PricingPolicyRef(policy_id="default", version=1),
    )


def _setup():
    db = Database(":memory:")
    provider = SandboxPaymentProvider()
    return db, provider


def _snapshot_and_payment(db, provider, *, total=100.0, idem="idem_key_00000001", user_id=None):
    quote = _quote(total)
    snap = ps.freeze_checkout_snapshot(
        db, booking_id="bk_1", journey_reference="jr_1", user_id=user_id,
        service_tier="BASIC", quote=quote,
    )
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key=idem)
    return snap, payment


# ======================================================================
# §J idempotency - creation
# ======================================================================
def test_duplicate_create_payment_returns_same_row_not_a_second_one():
    db, provider = _setup()
    snap, p1 = _snapshot_and_payment(db, provider, idem="idem_key_dup_1")
    p2, created2 = ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key="idem_key_dup_1")
    assert created2 is False
    assert p2.payment_id == p1.payment_id
    assert len(store.list_payments_for_booking(db, "bk_1")) == 1


def test_expired_snapshot_refuses_payment_creation():
    db, provider = _setup()
    quote = _quote(50.0)
    now = datetime.now(timezone.utc)
    snap = ps.freeze_checkout_snapshot(
        db, booking_id="bk_1", journey_reference="jr_1", user_id=None,
        service_tier="BASIC", quote=quote, now=now - timedelta(minutes=30),
    )
    with pytest.raises(ps.QuoteExpired):
        ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key="idem_key_expired1")


# ======================================================================
# §J idempotency - authorize/capture/cancel/refund
# ======================================================================
def test_duplicate_authorize_calls_are_idempotent_no_op():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider)
    a1 = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    a2 = ps.authorize_payment(db, payment=a1, provider=provider, snapshot=snap)
    assert a1.status == a2.status == PaymentStatus.AUTHORIZED
    assert a1.provider_payment_reference == a2.provider_payment_reference


def test_duplicate_capture_is_idempotent_customer_never_double_charged():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=75.0)
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    c1 = ps.request_capture(db, payment=auth, provider=provider)
    c2 = ps.request_capture(db, payment=c1, provider=provider)
    assert c1.captured_amount == c2.captured_amount == 75.0


def test_duplicate_refund_with_same_idempotency_key_never_double_refunds():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=60.0)
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    captured = ps.request_capture(db, payment=auth, provider=provider)
    p1, r1 = ps.request_refund(db, payment=captured, provider=provider, amount=20.0, reason="x", idempotency_key="rf_key_00001")
    p2, r2 = ps.request_refund(db, payment=p1, provider=provider, amount=20.0, reason="x", idempotency_key="rf_key_00001")
    assert r1.refund_id == r2.refund_id
    assert p2.refunded_amount == 20.0  # never 40


# ======================================================================
# §I refunds
# ======================================================================
def test_full_refund_marks_payment_refunded_not_partial():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=40.0)
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    captured = ps.request_capture(db, payment=auth, provider=provider)
    updated, refund = ps.request_refund(db, payment=captured, provider=provider, amount=40.0, reason="x", idempotency_key="full_rf_1")
    assert updated.status == PaymentStatus.REFUNDED
    assert refund.status == RefundStatus.SUCCEEDED


def test_partial_refund_never_labeled_full():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=40.0)
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    captured = ps.request_capture(db, payment=auth, provider=provider)
    updated, refund = ps.request_refund(db, payment=captured, provider=provider, amount=10.0, reason="x", idempotency_key="partial_rf_1")
    assert updated.status == PaymentStatus.PARTIALLY_REFUNDED
    assert updated.status != PaymentStatus.REFUNDED


def test_refund_above_captured_amount_rejected():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=40.0)
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    captured = ps.request_capture(db, payment=auth, provider=provider)
    with pytest.raises(ps.InvalidRefundAmount):
        ps.request_refund(db, payment=captured, provider=provider, amount=41.0, reason="x", idempotency_key="over_rf_1")


def test_refund_sequence_cannot_exceed_captured_amount_across_multiple_refunds():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=40.0)
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    captured = ps.request_capture(db, payment=auth, provider=provider)
    p1, _ = ps.request_refund(db, payment=captured, provider=provider, amount=30.0, reason="x", idempotency_key="seq_rf_1")
    with pytest.raises(ps.InvalidRefundAmount):
        ps.request_refund(db, payment=p1, provider=provider, amount=15.0, reason="x", idempotency_key="seq_rf_2")


def test_refund_from_wrong_status_rejected():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=40.0)
    with pytest.raises(ValueError):
        ps.request_refund(db, payment=payment, provider=provider, amount=10.0, reason="x", idempotency_key="wrong_status_1")


def _payment_with_scripted_id(db, provider, *, suffix, total=30.0, booking_id="bk_x"):
    """The sandbox's fault injection keys off the *reference* handed to
    ``authorize`` - which is always ``payment.payment_id`` (never the
    journey/booking id) - so to script a captured payment's later
    refund/capture behaviour the payment_id itself must carry the magic
    suffix. Construct the row directly rather than through
    ``ps.create_payment`` (which always mints a random id)."""
    from datetime import datetime, timezone

    from detoura.models.payment import PaymentStatus as _PS, PaymentTransaction

    quote = _quote(total)
    snap = ps.freeze_checkout_snapshot(
        db, booking_id=booking_id, journey_reference="jr_x", user_id=None,
        service_tier="BASIC", quote=quote,
    )
    now = datetime.now(timezone.utc)
    candidate = PaymentTransaction(
        payment_id=f"pay_scripted_{suffix.lstrip('_').lower()}{suffix}",
        journey_reference=snap.journey_reference, booking_id=snap.booking_id,
        checkout_snapshot_id=snap.snapshot_id, currency=snap.currency,
        customer_total=snap.customer_total, status=_PS.CREATED,
        provider=provider.name, idempotency_key=f"idem_scripted_{suffix}",
        created_at=now, updated_at=now,
    )
    payment, _created = store.create_payment(db, payment=candidate)
    return snap, payment


def test_scripted_refund_failure_leaves_captured_amount_untouched():
    db, provider = _setup()
    snap, payment = _payment_with_scripted_id(db, provider, suffix="_FAIL_REFUND")
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    captured = ps.request_capture(db, payment=auth, provider=provider)
    updated, refund = ps.request_refund(db, payment=captured, provider=provider, amount=10.0, reason="x", idempotency_key="idem_failrefund_2")
    assert refund.status == RefundStatus.FAILED
    assert updated.status == PaymentStatus.CAPTURED
    assert updated.refunded_amount == 0.0


def test_scripted_refund_unknown_escalates_to_reconciliation_required():
    db, provider = _setup()
    snap, payment = _payment_with_scripted_id(db, provider, suffix="_UNKNOWN_REFUND", booking_id="bk_3")
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    captured = ps.request_capture(db, payment=auth, provider=provider)
    updated, refund = ps.request_refund(db, payment=captured, provider=provider, amount=10.0, reason="x", idempotency_key="idem_unkrefund_2")
    assert refund.status == RefundStatus.UNKNOWN
    assert updated.status == PaymentStatus.RECONCILIATION_REQUIRED


# ======================================================================
# §K/§P reconciliation
# ======================================================================
def test_reconcile_unknown_authorize_syncs_to_real_provider_truth():
    db, provider = _setup()
    quote = _quote(20.0)
    snap = ps.freeze_checkout_snapshot(db, booking_id="bk_4", journey_reference="jr_4", user_id=None, service_tier="BASIC", quote=quote)
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key="idem_recon_1")
    result = provider.authorize(idempotency_key="a1", amount=20.0, currency="EUR", reference=payment.payment_id + "_UNKNOWN_AUTH")
    updated = ps._apply_authorize_result(db, payment=payment, result=result, now=datetime.now(timezone.utc))
    assert updated.status == PaymentStatus.UNKNOWN
    resolved, finding = ps.reconcile_payment(db, payment=updated, provider=provider)
    assert resolved.status == PaymentStatus.AUTHORIZED
    assert finding is None


def test_reconcile_with_no_provider_reference_creates_review_required_finding():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=20.0)
    # payment is still CREATED, no provider_payment_reference at all
    resolved, finding = ps.reconcile_payment(db, payment=payment, provider=provider)
    assert finding is not None
    from detoura.models.payment import ReconciliationClassification
    assert finding.classification == ReconciliationClassification.REVIEW_REQUIRED


def test_reconciliation_never_auto_syncs_a_dangerous_mismatch():
    """§P: local CAPTURED but provider says FAILED must never be silently
    auto-corrected - it is CRITICAL, a human decides."""
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=20.0)
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    captured = ps.request_capture(db, payment=auth, provider=provider)
    # Simulate a provider retrieve reporting something the local record
    # disagrees with, by monkeypatching a fake provider for this one call.
    class FakeMismatch:
        name = "sandbox"
        def retrieve(self, *, provider_reference):
            from detoura.providers.payment_provider import ProviderResult
            return ProviderResult(ok=True, unknown=False, provider_reference=provider_reference, status="failed")
    resolved, finding = ps.reconcile_payment(db, payment=captured, provider=FakeMismatch())
    from detoura.models.payment import ReconciliationClassification
    assert finding is not None
    assert finding.classification == ReconciliationClassification.CRITICAL
    assert resolved.status == PaymentStatus.RECONCILIATION_REQUIRED
    assert resolved.captured_amount == captured.captured_amount  # untouched


def test_reconciling_a_payment_that_already_matches_provider_truth_is_a_safe_no_op():
    """§O/§P regression: an out-of-order or routine reconcile call on a
    payment whose local status already exactly matches provider truth must
    never be flagged REVIEW_REQUIRED/escalated - that would falsely alarm
    on every healthy payment a webhook or Ops health-check reconciles."""
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=40.0)
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    captured = ps.request_capture(db, payment=auth, provider=provider)
    resolved, finding = ps.reconcile_payment(db, payment=captured, provider=provider)
    assert resolved.status == PaymentStatus.CAPTURED
    assert finding is None
    # Also true one step earlier, for AUTHORIZED matching "authorized".
    snap2, payment2 = _snapshot_and_payment(db, provider, total=25.0, idem="idem_match_auth_1", user_id=None)
    auth2 = ps.authorize_payment(db, payment=payment2, provider=provider, snapshot=snap2)
    resolved2, finding2 = ps.reconcile_payment(db, payment=auth2, provider=provider)
    assert resolved2.status == PaymentStatus.AUTHORIZED
    assert finding2 is None


def test_cancel_authorization_from_created_never_sent_to_provider():
    """§F regression: a payment abandoned before ever being confirmed
    (never authorized, no provider reference) must be cleanly cancellable
    - the exact case `cancel_authorization`'s own "never sent to the
    provider" branch is written for."""
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=15.0)
    assert payment.status == PaymentStatus.CREATED
    cancelled = ps.cancel_authorization(db, payment=payment, provider=provider)
    assert cancelled.status == PaymentStatus.CANCELLED


def test_resolve_finding_never_changes_payment_status():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=20.0)
    _, finding = ps.reconcile_payment(db, payment=payment, provider=provider)
    before = store.get_payment(db, payment.payment_id)
    ok = store.resolve_finding(db, finding.finding_id)
    assert ok
    after = store.get_payment(db, payment.payment_id)
    assert before.status == after.status  # resolving a finding never touches payment state


# ======================================================================
# §H allocations
# ======================================================================
def test_allocations_written_at_creation_and_sum_to_customer_total():
    db, provider = _setup()
    quote = _quote(100.0, detoura_service_fee=8.0, detoura_markup=5.0, tax=2.0)
    snap = ps.freeze_checkout_snapshot(db, booking_id="bk_alloc", journey_reference="jr_alloc", user_id=None, service_tier="BASIC", quote=quote)
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key="idem_alloc_1")
    allocations = store.list_allocations(db, payment.payment_id)
    total = round(sum(a.amount for a in allocations), 2)
    assert total == payment.customer_total


def test_allocations_distinguish_supplier_from_detoura_revenue():
    db, provider = _setup()
    quote = _quote(100.0, detoura_service_fee=8.0, detoura_markup=5.0)
    snap = ps.freeze_checkout_snapshot(db, booking_id="bk_alloc2", journey_reference="jr_alloc2", user_id=None, service_tier="BASIC", quote=quote)
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key="idem_alloc_2")
    allocations = store.list_allocations(db, payment.payment_id)
    from detoura.models.payment import AllocationComponent
    components = {a.component for a in allocations}
    assert AllocationComponent.SUPPLIER_COST in components
    assert AllocationComponent.DETOURA_SERVICE_FEE in components
    assert AllocationComponent.DETOURA_MARKUP in components


# ======================================================================
# §J concurrency - CAS
# ======================================================================
def test_concurrent_stale_write_is_rejected_not_silently_overwritten():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider)
    fresh = store.get_payment(db, payment.payment_id)
    # Two independent "workers" both read the same version...
    worker_a = fresh.with_status(PaymentStatus.AUTHORIZED).model_copy(update={"authorized_amount": 100.0})
    worker_b = fresh.with_status(PaymentStatus.AUTHORIZED).model_copy(update={"authorized_amount": 100.0})
    store.compare_and_swap_payment(db, payment=worker_a, expected_version=fresh.version)
    with pytest.raises(store.StaleVersion):
        store.compare_and_swap_payment(db, payment=worker_b, expected_version=fresh.version)


# ======================================================================
# §Q audit trail
# ======================================================================
def test_every_irreversible_step_has_a_ledger_event():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=50.0)
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    captured = ps.request_capture(db, payment=auth, provider=provider)
    ps.request_refund(db, payment=captured, provider=provider, amount=50.0, reason="x", idempotency_key="ledger_rf_1")
    events = [e.event_type for e in store.list_events(db, payment.payment_id)]
    for expected in ("PAYMENT_CREATED", "AUTHORIZATION_REQUESTED", "AUTHORIZED",
                     "CAPTURE_REQUESTED", "CAPTURED", "REFUND_REQUESTED", "REFUND_SUCCEEDED"):
        assert expected in events, f"{expected} missing from ledger"


def test_ledger_events_never_contain_payment_credentials():
    db, provider = _setup()
    snap, payment = _snapshot_and_payment(db, provider, total=50.0)
    ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    events = store.list_events(db, payment.payment_id)
    import json
    blob = json.dumps([e.model_dump(mode="json") for e in events]).lower()
    for forbidden in ("card_number", "cvc", "cvv", "pan", "sk_test_", "sk_live_"):
        assert forbidden not in blob
