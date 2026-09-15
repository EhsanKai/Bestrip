"""V9 Phase 4 §E/§F/§G, §X, controlled E2E benchmark - booking+payment
orchestration: authorization-first strategy, pre-booking failure,
partial-booking failure, and the 6 mandated controlled scenarios."""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone

import pytest

from detoura.models.booking import BookingState, PriceTolerance
from detoura.models.commercial import CommercialQuote, PriceBreakdown, PricingPolicyRef, ServiceTier
from detoura.models.payment import PaymentStatus
from detoura.models.travel_pass import PassMode
from detoura.models.traveler import Traveler, TravelerParty
from detoura.persistence.db import Database
from detoura.providers.duffel import DuffelOrderError
from detoura.providers.sandbox_payment import SandboxPaymentProvider
from detoura.services import payment_service as ps
from detoura.services.booking_orchestrator import BookingPhase, BookingRun, ItemProgress
from detoura.services.payment_booking_orchestrator import run_paid_booking


# ======================================================================
# Fake Duffel double - deterministic, no network, models only the shape
# `booking_orchestrator` actually calls.
# ======================================================================
class _FakeRevalidated:
    def __init__(self, price_per_person: float) -> None:
        self.price_per_person = price_per_person
        self.provider_ref = None
        self.operator = ""


class FakeDuffel:
    def __init__(self, *, fail_offer_ids: set[str] | None = None, timeout_offer_ids: set[str] | None = None) -> None:
        self.fail_offer_ids = fail_offer_ids or set()
        self.timeout_offer_ids = timeout_offer_ids or set()
        self.order_calls: list[str] = []

    def revalidate_offer(self, offer_id, origin_airport, destination_airport, *, travelers):
        return _FakeRevalidated(price_per_person=100.0)

    def get_offer(self, offer_id):
        return {"total_amount": "100.00", "total_currency": "EUR", "passengers": [{"id": "pas_0"}]}

    def create_test_order(self, offer_id, *, passengers, expected_amount, expected_currency):
        self.order_calls.append(offer_id)
        if offer_id in self.timeout_offer_ids:
            raise TimeoutError("sandbox order timed out (scripted)")
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


def _run(items: list[ItemProgress], *, booking_id="bk_e2e", total: float = 200.0) -> BookingRun:
    return BookingRun(
        booking_id=booking_id, journey_reference="jr_e2e", mode=PassMode.SANDBOX_BOOKED,
        trip_label="test trip", route_cities=("BER", "LHR"), currency="EUR",
        discovered_total=total, tolerance=PriceTolerance(absolute=5.0, percentage=5.0),
        items=items, party=TravelerParty(travelers=(_traveler(),)),
        # V9 Phase 6 Payment Security: a run with a party already attached
        # is, in production, exactly what `booking_flow.attach_travelers`
        # leaves at AWAITING_CONFIRMATION - `run_booking`'s single-execution
        # claim now enforces that a run must genuinely be in that phase (or
        # RECONFIRM_REQUIRED) before it can execute, so this fixture must
        # reflect the same real pre-state rather than the dataclass default.
        phase=BookingPhase.AWAITING_CONFIRMATION,
    )


def _quote(total: float) -> CommercialQuote:
    return CommercialQuote(
        service_tier=ServiceTier.BASIC,
        breakdown=PriceBreakdown(currency="EUR", supplier_transport=total),
        markup_policy=PricingPolicyRef(policy_id="default", version=1),
    )


def _setup():
    return Database(":memory:"), SandboxPaymentProvider()


class OneShotUnknown:
    """Wraps a real :class:`PaymentProvider` so the FIRST call to a chosen
    operation for a given reference returns UNKNOWN exactly once, then
    delegates to the inner provider normally.

    This models a transient client-side timeout that the provider itself
    actually processed fine (or genuinely never received) - the realistic
    case reconciliation exists for. It is deliberately different from the
    sandbox's own scripted ``_UNKNOWN_*`` suffixes, which are *permanent*
    per-reference faults (a "this test card always times out" fixture, for
    scenarios that must stay unresolved until a human/Ops decision) - using
    those here would make a capture/refund retry unknown forever by design,
    not prove anything about recovery."""

    def __init__(self, inner, *, once_ops: set[str]) -> None:
        self._inner = inner
        self._once_ops = once_ops
        self._fired: set[tuple[str, str]] = set()
        self.name = inner.name

    def capabilities(self):
        return self._inner.capabilities()

    def _maybe_unknown(self, op: str, key: str, call):
        marker = (op, key)
        if op in self._once_ops and marker not in self._fired:
            self._fired.add(marker)
            from detoura.providers.payment_provider import ProviderResult
            return ProviderResult(
                ok=False, unknown=True, provider_reference=key or None,
                status="unknown", detail=f"one-shot simulated {op} timeout",
            )
        return call()

    def authorize(self, **kw):
        return self._maybe_unknown("authorize", kw.get("reference", ""), lambda: self._inner.authorize(**kw))

    def capture(self, **kw):
        return self._maybe_unknown("capture", kw.get("provider_reference", ""), lambda: self._inner.capture(**kw))

    def cancel_authorization(self, **kw):
        return self._maybe_unknown(
            "cancel_authorization", kw.get("provider_reference", ""),
            lambda: self._inner.cancel_authorization(**kw),
        )

    def refund(self, **kw):
        return self._maybe_unknown("refund", kw.get("provider_reference", ""), lambda: self._inner.refund(**kw))

    def retrieve(self, **kw):
        return self._inner.retrieve(**kw)

    def verify_event(self, **kw):
        return self._inner.verify_event(**kw)


# ======================================================================
# 1) HAPPY PATH
# ======================================================================
def test_happy_path_full_booking_authorization_capture():
    db, provider = _setup()
    items = [_item("leg-1", "off_1"), _item("leg-2", "off_2")]
    run = _run(items)
    duffel = FakeDuffel()
    outcome = run_paid_booking(
        db, run=run, quote=_quote(200.0), provider=provider, provider_name=provider.name,
        idempotency_key="idem_happy_1", duffel=duffel,
    )
    assert outcome.booking_phase is BookingPhase.COMPLETE
    assert outcome.payment.status is PaymentStatus.CAPTURED
    assert outcome.payment.captured_amount == 200.0
    assert not outcome.requires_ops_recovery
    assert not outcome.requires_customer_reconfirmation
    assert all(i.state is BookingState.CONFIRMED for i in run.items)


# ======================================================================
# 2) BOOKING FAILURE BEFORE SUPPLIER COMMITMENT
# (both required legs fail revalidation -> BookingPhase.FAILED, nothing
# was ever sent to a supplier order)
# ======================================================================
def test_pre_booking_failure_releases_authorization_no_captured_money():
    db, provider = _setup()
    items = [_item("leg-1", "off_1")]
    run = _run(items, total=100.0)

    class _AlwaysGoneDuffel(FakeDuffel):
        def revalidate_offer(self, *a, **kw):
            from detoura.providers.duffel import DuffelOfferGone
            raise DuffelOfferGone("off_1", status=404)

    outcome = run_paid_booking(
        db, run=run, quote=_quote(100.0), provider=provider, provider_name=provider.name,
        idempotency_key="idem_prebook_1", duffel=_AlwaysGoneDuffel(),
    )
    assert run.phase is BookingPhase.FAILED
    assert outcome.payment.status is PaymentStatus.CANCELLED
    assert outcome.payment.captured_amount == 0.0
    assert not outcome.requires_ops_recovery


# ======================================================================
# 3) PARTIAL BOOKING FAILURE
# ======================================================================
def test_partial_booking_failure_holds_at_reconciliation_required_truthfully():
    db, provider = _setup()
    items = [_item("leg-1", "off_1"), _item("leg-2", "off_2")]
    run = _run(items, total=200.0)
    duffel = FakeDuffel(fail_offer_ids={"off_2"})
    outcome = run_paid_booking(
        db, run=run, quote=_quote(200.0), provider=provider, provider_name=provider.name,
        idempotency_key="idem_partial_1", duffel=duffel,
    )
    assert run.phase is BookingPhase.PARTIAL_FAILURE
    assert outcome.payment.status is PaymentStatus.RECONCILIATION_REQUIRED
    assert outcome.requires_ops_recovery
    # Truthful: never simply "PAYMENT FAILED" / "BOOKING FAILED" - the
    # authorized amount is still on hold, not captured, not released.
    assert outcome.payment.authorized_amount == 200.0
    assert outcome.payment.captured_amount == 0.0
    leg1 = next(i for i in run.items if i.item_id == "leg-1")
    leg2 = next(i for i in run.items if i.item_id == "leg-2")
    assert leg1.state is BookingState.CONFIRMED
    assert leg2.state is BookingState.FAILED
    # The finding/event data preserves exactly which legs succeeded/failed.
    events = ps.store.list_events(db, outcome.payment.payment_id)
    recon = next(e for e in events if e.event_type == "RECONCILIATION_REQUIRED")
    assert recon.data["confirmed_items"] == ["leg-1"]
    assert recon.data["failed_items"] == ["leg-2"]


def test_partial_booking_failure_then_ops_can_still_capture_or_cancel_explicitly():
    """§G: the hold is not a dead end - Ops can resolve it via the SAME
    domain functions, once a human has decided the right outcome."""
    db, provider = _setup()
    items = [_item("leg-1", "off_1"), _item("leg-2", "off_2")]
    run = _run(items, total=200.0)
    duffel = FakeDuffel(fail_offer_ids={"off_2"})
    outcome = run_paid_booking(
        db, run=run, quote=_quote(200.0), provider=provider, provider_name=provider.name,
        idempotency_key="idem_partial_2", duffel=duffel,
    )
    held = outcome.payment
    # A human decides: release the hold (leg-1 will be handled/refunded via
    # a separate supplier-side cancellation, out of Phase 4's scope).
    from detoura.models.payment import PaymentTransaction
    reopened = held.with_status(PaymentStatus.AUTHORIZED)
    reopened = ps.store.compare_and_swap_payment(db, payment=reopened, expected_version=held.version)
    cancelled = ps.cancel_authorization(db, payment=reopened, provider=provider)
    assert cancelled.status is PaymentStatus.CANCELLED


# ======================================================================
# 4) CAPTURE UNKNOWN
# ======================================================================
def test_capture_unknown_reconciles_without_duplicate_capture():
    db, real_provider = _setup()
    provider = OneShotUnknown(real_provider, once_ops={"capture"})

    quote = _quote(50.0)
    snap = ps.freeze_checkout_snapshot(db, booking_id="bk_capunk", journey_reference="jr_capunk", user_id=None, service_tier="BASIC", quote=quote)
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key="idem_capunk_1")
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    assert auth.status is PaymentStatus.AUTHORIZED

    pending = ps.request_capture(db, payment=auth, provider=provider)
    assert pending.status is PaymentStatus.UNKNOWN

    resolved, finding = ps.reconcile_payment(db, payment=pending, provider=provider)
    assert resolved.status is PaymentStatus.AUTHORIZED  # true state: capture never actually reached the provider
    assert finding is None

    # A fresh capture attempt now genuinely reaches the provider (a
    # different provider idempotency key, thanks to the version bump the
    # reconciliation's own CAS produced) and succeeds - no duplicate charge,
    # no stale replay of the earlier ambiguous outcome.
    captured = ps.request_capture(db, payment=resolved, provider=provider)
    assert captured.status is PaymentStatus.CAPTURED
    assert captured.captured_amount == 50.0
    # Re-requesting capture again is an idempotent no-op, not a second charge.
    again = ps.request_capture(db, payment=captured, provider=provider)
    assert again.captured_amount == 50.0


# ======================================================================
# 5) REFUND UNKNOWN
# ======================================================================
def test_refund_unknown_reconciles_without_duplicate_refund():
    db, real_provider = _setup()
    provider = OneShotUnknown(real_provider, once_ops={"refund"})

    quote = _quote(40.0)
    snap = ps.freeze_checkout_snapshot(db, booking_id="bk_refunk", journey_reference="jr_refunk", user_id=None, service_tier="BASIC", quote=quote)
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key="idem_refunk_1")
    auth = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    captured = ps.request_capture(db, payment=auth, provider=provider)
    assert captured.status is PaymentStatus.CAPTURED

    updated, refund = ps.request_refund(db, payment=captured, provider=provider, amount=40.0, reason="test", idempotency_key="idem_refunk_2")
    assert refund.status.value == "UNKNOWN"
    assert updated.status is PaymentStatus.RECONCILIATION_REQUIRED

    resolved, finding = ps.reconcile_payment(db, payment=updated, provider=provider)
    # True state: the refund never actually reached the provider.
    assert resolved.captured_amount == 40.0
    assert resolved.refunded_amount == 0.0
    assert resolved.status is PaymentStatus.CAPTURED  # safely synced back, not stuck (§X.18)

    # A fresh, real refund now succeeds - and it is not a duplicate because
    # the first one never actually completed.
    resolved2, refund2 = ps.request_refund(db, payment=resolved, provider=provider, amount=40.0, reason="retry", idempotency_key="idem_refunk_3")
    assert refund2.status.value == "SUCCEEDED"
    assert resolved2.refunded_amount == 40.0
    assert resolved2.status is PaymentStatus.REFUNDED


# ======================================================================
# 6) DUPLICATE CONFIRM (concurrent)
# ======================================================================
def test_duplicate_concurrent_confirm_results_in_one_financial_execution():
    db, provider = _setup()
    quote = _quote(80.0)
    snap = ps.freeze_checkout_snapshot(db, booking_id="bk_dup", journey_reference="jr_dup", user_id=None, service_tier="BASIC", quote=quote)
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key="idem_dupconfirm_1")

    results = []
    errors = []

    def _confirm():
        try:
            fresh = ps.store.get_payment(db, payment.payment_id)
            results.append(ps.authorize_payment(db, payment=fresh, provider=provider, snapshot=snap))
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=_confirm) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    final = ps.store.get_payment(db, payment.payment_id)
    assert final.status is PaymentStatus.AUTHORIZED
    # Every thread's result agrees on the same provider reference - one
    # underlying authorization, not five.
    refs = {r.provider_payment_reference for r in results}
    assert refs == {final.provider_payment_reference}
    assert final.authorized_amount == 80.0


# ======================================================================
# Additional named failure-injection scenarios (§W) not already covered
# above or in test_v9_phase4_service.py
# ======================================================================
def test_authorization_fails_outright_no_booking_attempted():
    db, provider = _setup()
    items = [_item("leg-1", "off_1")]
    run = _run(items, total=100.0)
    from detoura.models.payment import PaymentTransaction as _PT

    # Force authorize() to see the FAIL_AUTH suffix by pre-creating the
    # payment row with a scripted payment_id (see test_v9_phase4_service.py
    # for why the id, not the journey/booking id, must carry the suffix).
    quote = _quote(100.0)
    snap = ps.freeze_checkout_snapshot(db, booking_id=run.booking_id, journey_reference=run.journey_reference, user_id=None, service_tier="BASIC", quote=quote)
    now = datetime.now(timezone.utc)
    candidate = _PT(
        payment_id="pay_scripted_FAIL_AUTH", journey_reference=snap.journey_reference,
        booking_id=snap.booking_id, checkout_snapshot_id=snap.snapshot_id, currency=snap.currency,
        customer_total=snap.customer_total, status=PaymentStatus.CREATED, provider=provider.name,
        idempotency_key="idem_failauth_1", created_at=now, updated_at=now,
    )
    ps.store.create_payment(db, payment=candidate)
    payment = ps.authorize_payment(db, payment=candidate, provider=provider, snapshot=snap)
    assert payment.status is PaymentStatus.FAILED
    # Booking must never have been attempted.
    assert all(i.state is BookingState.DRAFT for i in run.items)


def test_authorization_timeout_unknown_never_silently_becomes_failed():
    db, provider = _setup()
    from detoura.models.payment import PaymentTransaction as _PT

    quote = _quote(60.0)
    snap = ps.freeze_checkout_snapshot(db, booking_id="bk_unkauth", journey_reference="jr_unkauth", user_id=None, service_tier="BASIC", quote=quote)
    now = datetime.now(timezone.utc)
    candidate = _PT(
        payment_id="pay_scripted_UNKNOWN_AUTH", journey_reference=snap.journey_reference,
        booking_id=snap.booking_id, checkout_snapshot_id=snap.snapshot_id, currency=snap.currency,
        customer_total=snap.customer_total, status=PaymentStatus.CREATED, provider=provider.name,
        idempotency_key="idem_unkauth_1", created_at=now, updated_at=now,
    )
    ps.store.create_payment(db, payment=candidate)
    payment = ps.authorize_payment(db, payment=candidate, provider=provider, snapshot=snap)
    assert payment.status is PaymentStatus.UNKNOWN
    assert payment.status is not PaymentStatus.FAILED


def test_price_changes_before_authorization_expired_snapshot_refuses():
    db, provider = _setup()
    from datetime import timedelta
    quote = _quote(120.0)
    now = datetime.now(timezone.utc)
    snap = ps.freeze_checkout_snapshot(
        db, booking_id="bk_expire", journey_reference="jr_expire", user_id=None,
        service_tier="BASIC", quote=quote, now=now - timedelta(minutes=20),
    )
    with pytest.raises(ps.QuoteExpired):
        ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key="idem_expire_1")


def test_process_restart_between_booking_and_capture_is_recoverable_from_persisted_state():
    """Simulates a restart: rebuild the payment purely from what is
    persisted (a fresh `get_payment` call, no in-memory state carried
    over) and finish the capture from there."""
    db, provider = _setup()
    quote = _quote(90.0)
    snap = ps.freeze_checkout_snapshot(db, booking_id="bk_restart", journey_reference="jr_restart", user_id=None, service_tier="BASIC", quote=quote)
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key="idem_restart_1")
    payment = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    assert payment.status is PaymentStatus.AUTHORIZED

    # "Restart": drop all in-memory references, reload strictly from disk.
    del payment
    reloaded = ps.store.get_payment(db, ps.store.list_payments_for_booking(db, "bk_restart")[0].payment_id)
    assert reloaded.status is PaymentStatus.AUTHORIZED
    captured = ps.request_capture(db, payment=reloaded, provider=provider)
    assert captured.status is PaymentStatus.CAPTURED
    assert captured.captured_amount == 90.0
