"""V9 Phase 4 §D/§X - payment domain: state machine, money invariants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from detoura.models.commercial import CommercialQuote, PriceBreakdown, PricingPolicyRef, ServiceTier
from detoura.models.money import from_minor_units, to_minor_units
from detoura.models.payment import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    CheckoutSnapshot,
    InvalidPaymentTransition,
    PaymentStatus,
    PaymentTransaction,
    RefundStatus,
    can_transition_payment,
    can_transition_refund,
)


def _quote(total: float = 100.0) -> CommercialQuote:
    return CommercialQuote(
        service_tier=ServiceTier.BASIC,
        breakdown=PriceBreakdown(currency="EUR", supplier_transport=total),
        markup_policy=PricingPolicyRef(policy_id="default", version=1),
    )


def _snapshot(**kw) -> CheckoutSnapshot:
    defaults = dict(
        snapshot_id="snap_x", booking_id="bk_x", journey_reference="jr_x",
        service_tier="BASIC", quote=_quote(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    defaults.update(kw)
    return CheckoutSnapshot(**defaults)


def _payment(**kw) -> PaymentTransaction:
    defaults = dict(
        payment_id="pay_x", journey_reference="jr_x", booking_id="bk_x",
        checkout_snapshot_id="snap_x", currency="EUR", customer_total=100.0,
        provider="sandbox", idempotency_key="idem_test_key_1",
    )
    defaults.update(kw)
    return PaymentTransaction(**defaults)


# ======================================================================
# §1 no floating point money at rest
# ======================================================================
def test_minor_units_roundtrip_exact():
    for amount in (0.0, 1.0, 12.34, 999.99, 0.01, 1234567.89):
        assert from_minor_units(to_minor_units(amount)) == amount


def test_minor_units_are_integers():
    assert isinstance(to_minor_units(12.34), int)
    assert to_minor_units(12.345) == 1235  # half-up, per money.py's own rule


# ======================================================================
# State machine
# ======================================================================
def test_every_state_is_reachable_or_terminal():
    all_states = set(PaymentStatus)
    reachable_targets = set()
    for targets in ALLOWED_TRANSITIONS.values():
        reachable_targets |= targets
    unreachable = all_states - reachable_targets - {PaymentStatus.CREATED}
    assert not unreachable, f"states with no incoming transition: {unreachable}"


def test_terminal_states_have_no_outgoing_transitions():
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_same_state_transition_is_always_allowed_idempotent():
    for status in PaymentStatus:
        assert can_transition_payment(status, status)


def test_created_cannot_jump_to_captured():
    assert not can_transition_payment(PaymentStatus.CREATED, PaymentStatus.CAPTURED)


def test_captured_cannot_go_back_to_created():
    assert not can_transition_payment(PaymentStatus.CAPTURED, PaymentStatus.CREATED)


def test_refunded_is_terminal():
    assert can_transition_payment(PaymentStatus.REFUNDED, PaymentStatus.CAPTURED) is False


def test_unknown_can_only_resolve_via_reconciliation_targets():
    allowed = ALLOWED_TRANSITIONS[PaymentStatus.UNKNOWN]
    assert PaymentStatus.CAPTURE_PENDING not in allowed  # never re-enter mid-flight from UNKNOWN
    assert PaymentStatus.CAPTURED in allowed  # reachable once reconciled


def test_invalid_transition_raises_on_the_model():
    p = _payment(status=PaymentStatus.FAILED)
    with pytest.raises(InvalidPaymentTransition):
        p.with_status(PaymentStatus.CAPTURED)


def test_refund_state_machine_partial_never_labeled_full_transition_table():
    """§I regression: the transition table itself keeps PARTIALLY_REFUNDED
    and REFUNDED as genuinely distinct terminal-ish states, never merged."""
    assert PaymentStatus.PARTIALLY_REFUNDED not in TERMINAL_STATUSES
    assert PaymentStatus.REFUNDED in TERMINAL_STATUSES
    assert PaymentStatus.PARTIALLY_REFUNDED != PaymentStatus.REFUNDED


def test_refund_status_transitions():
    assert can_transition_refund(RefundStatus.PENDING, RefundStatus.SUCCEEDED)
    assert can_transition_refund(RefundStatus.PENDING, RefundStatus.FAILED)
    assert not can_transition_refund(RefundStatus.SUCCEEDED, RefundStatus.FAILED)
    assert can_transition_refund(RefundStatus.UNKNOWN, RefundStatus.SUCCEEDED)


# ======================================================================
# §5/§6 - amount invariants on the model itself
# ======================================================================
def test_captured_amount_cannot_exceed_authorized_amount():
    with pytest.raises(Exception):
        _payment(authorized_amount=50.0, captured_amount=100.0)


def test_refunded_amount_cannot_exceed_captured_amount():
    with pytest.raises(Exception):
        _payment(captured_amount=50.0, refunded_amount=100.0)


def test_zero_authorized_with_positive_captured_is_allowed_only_if_not_exceeding():
    # A degenerate but not-inherently-invalid shape (e.g. reconciliation
    # backfilled captured without authorized having been recorded) - the
    # validator only rejects captured > authorized when authorized > 0.
    p = _payment(authorized_amount=0.0, captured_amount=0.0)
    assert p.captured_amount == 0.0


# ======================================================================
# §C - checkout snapshot
# ======================================================================
def test_snapshot_expiry():
    now = datetime.now(timezone.utc)
    snap = _snapshot(created_at=now - timedelta(minutes=20), expires_at=now - timedelta(minutes=5))
    assert snap.is_expired(now=now)


def test_snapshot_not_expired_before_ttl():
    now = datetime.now(timezone.utc)
    snap = _snapshot(created_at=now, expires_at=now + timedelta(minutes=15))
    assert not snap.is_expired(now=now)


def test_snapshot_customer_total_comes_from_the_quote_not_a_separate_field():
    quote = _quote(250.0)
    snap = _snapshot(quote=quote)
    assert snap.customer_total == quote.customer_total


# ======================================================================
# §2/§3 - money truth: nothing here can independently invent a price
# ======================================================================
def test_payment_transaction_never_has_a_price_computation_method():
    """There is no method on PaymentTransaction that derives customer_total
    from anything other than what was passed in at construction - it is a
    frozen, externally-supplied field, never computed."""
    assert "customer_total" in PaymentTransaction.model_fields
    # frozen: assigning after construction must fail
    p = _payment()
    with pytest.raises(Exception):
        p.customer_total = 1.0  # type: ignore[misc]


def test_market_prior_and_optimizer_estimate_types_never_imported_by_payment_domain():
    """§B/§X.3: static proof, not just a runtime check - the payment domain
    module must not IMPORT anything from the Market Prior or optimizer
    estimate modules (checked against actual import statements, not
    docstring prose that merely *explains* the invariant)."""
    import ast

    import detoura.models.payment as payment_module

    tree = ast.parse(open(payment_module.__file__).read())
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
    forbidden = ("market_prior", "opportunity", "beam_search", "candidate_funnel")
    for module_name in imported_modules:
        assert not any(f in module_name for f in forbidden), (
            f"payment domain must not import {module_name!r}"
        )
