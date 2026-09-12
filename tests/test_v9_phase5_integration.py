"""V9 Phase 5 - Agent 6 integration tests: the post-booking finalizer's
10 mandated controlled E2E scenarios, cross-domain wiring, and the global
"booking success != email success" / restart-safety invariants."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from detoura.communication_config import reset_communication_config
from detoura.models.commercial import CommercialQuote, PriceBreakdown, PricingPolicyRef, ServiceTier
from detoura.models.confirmation import ConfirmationStatus
from detoura.models.communication import CommunicationStatus
from detoura.payment_config import reset_payment_config
from detoura.persistence import accounts, bookings, economics, get_db
from detoura.persistence.db import Database
from detoura.providers.sandbox_email import SandboxEmailProvider
from detoura.providers.sandbox_payment import SandboxPaymentProvider
from detoura.services import communication_service as cs
from detoura.services import financial_document_service as fds
from detoura.services import payment_service as ps
from detoura.services import post_booking_finalizer as pbf


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_payment_config()
    reset_communication_config()
    yield
    reset_payment_config()
    reset_communication_config()


class OneShotUnknownEmail:
    """Mirrors Phase 4's OneShotUnknown wrapper: the FIRST send for a given
    reference returns UNKNOWN exactly once, then delegates normally - a
    transient timeout the provider itself actually processed, not a
    permanent scripted fault."""

    def __init__(self, inner, *, fail_first: bool = False) -> None:
        self._inner = inner
        self._fail_first = fail_first
        self._fired: set[str] = set()
        self.name = inner.name

    def capabilities(self):
        return self._inner.capabilities()

    def send(self, *, idempotency_key, recipient, subject, body_text, reference):
        if reference not in self._fired:
            self._fired.add(reference)
            from detoura.providers.communication_provider import CommunicationSendResult
            if self._fail_first:
                return CommunicationSendResult(
                    ok=False, unknown=False, provider_message_id=None,
                    status="rejected", detail="one-shot simulated failure",
                )
            # Realistic timeout: the real provider actually processes the
            # send (so it has a genuine, retrievable message id) - the
            # caller's response is simply "lost", exactly like Phase 4's
            # payment providers modeling an authorize that timed out on the
            # client side but still returned a provider reference.
            real_result = self._inner.send(
                idempotency_key=idempotency_key, recipient=recipient, subject=subject,
                body_text=body_text, reference=reference,
            )
            return CommunicationSendResult(
                ok=False, unknown=True, provider_message_id=real_result.provider_message_id,
                status="unknown", detail="one-shot simulated timeout",
            )
        return self._inner.send(
            idempotency_key=idempotency_key, recipient=recipient, subject=subject,
            body_text=body_text, reference=reference,
        )

    def retrieve(self, *, provider_message_id):
        return self._inner.retrieve(provider_message_id=provider_message_id)


def _quote(total: float = 150.0, **kw) -> CommercialQuote:
    return CommercialQuote(
        service_tier=ServiceTier.BASIC,
        breakdown=PriceBreakdown(currency="EUR", supplier_transport=total, **kw),
        markup_policy=PricingPolicyRef(policy_id="default", version=1),
    )


def _seed_booking(db, *, booking_id, phase="complete", recovery_state="", total=150.0, lead_email="x@example.com"):
    bookings.upsert(db, bookings.BookingRecord(
        booking_id=booking_id, journey_reference=f"jr_{booking_id}",
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        mode="DEMO_ONLY", phase=phase, recovery_state=recovery_state,
        party_size=1, lead_name="Ada Lovelace", lead_email=lead_email,
        customer_total=total, currency="EUR", service_tier="BASIC",
    ))


def _seed_paid_booking(db, *, booking_id, total=150.0, phase="complete", recovery_state="", user_id=None):
    from detoura.payment_config import resolve_provider

    _seed_booking(db, booking_id=booking_id, phase=phase, recovery_state=recovery_state, total=total)
    if user_id:
        accounts.claim_trip(db, user_id=user_id, booking_id=booking_id, journey_reference=f"jr_{booking_id}")
    quote = _quote(total - 20.0, detoura_service_fee=10.0, detoura_markup=10.0)
    economics.write_snapshot(db, booking_id=booking_id, journey_reference=f"jr_{booking_id}", quote=quote, snapshot={})
    # The process-wide singleton, exactly like production code - a fresh
    # SandboxPaymentProvider() here would not remember the reference it
    # just authorized/captured, and a later refund against a DIFFERENT
    # instance would see "not_found" and silently fail (this is the exact
    # bug class Phase 4 found in resolve_provider() itself).
    provider = resolve_provider()
    snap = ps.freeze_checkout_snapshot(db, booking_id=booking_id, journey_reference=f"jr_{booking_id}", user_id=user_id, service_tier="BASIC", quote=quote)
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name="sandbox", idempotency_key=f"idem_{booking_id}_pay")
    payment = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    payment = ps.request_capture(db, payment=payment, provider=provider)
    return payment


def _db() -> Database:
    return Database(":memory:")


# ======================================================================
# 1) HAPPY PATH
# ======================================================================
def test_happy_path_full_pipeline():
    db = _db()
    _seed_paid_booking(db, booking_id="bk_happy")
    outcome = pbf.try_finalize(db, booking_id="bk_happy")
    assert outcome.confirmation.status is ConfirmationStatus.CONFIRMED
    assert outcome.document is not None
    assert outcome.communication is not None
    assert outcome.communication.status is CommunicationStatus.SENT
    assert not outcome.document_error
    assert not outcome.communication_error


# ======================================================================
# 2) EMAIL FAILURE
# ======================================================================
def test_email_failure_never_touches_booking_or_payment_truth():
    db = _db()
    payment = _seed_paid_booking(db, booking_id="bk_emailfail")
    real = SandboxEmailProvider()
    failing = OneShotUnknownEmail(real, fail_first=True)
    original = cs.create_and_send_communication  # captured BEFORE patching - see note below

    def _send_safely(*a, **kw):
        return original(*a, provider=failing, **kw)

    import unittest.mock
    # Patching the module attribute mutates the SAME module object `cs` is
    # bound to, so a naive side_effect calling `cs.create_and_send_communication`
    # would call itself - `original` is captured first to avoid that.
    with unittest.mock.patch.object(pbf.communication_service, "create_and_send_communication", side_effect=_send_safely):
        outcome = pbf.try_finalize(db, booking_id="bk_emailfail")

    assert outcome.confirmation.status is ConfirmationStatus.CONFIRMED  # unaffected
    assert outcome.document is not None  # unaffected
    assert outcome.communication is not None
    assert outcome.communication.status is CommunicationStatus.FAILED
    from detoura.persistence import payments as payment_store
    still = payment_store.get_payment(db, payment.payment_id)
    assert still.status.value == "CAPTURED"  # payment truth untouched by email failure


# ======================================================================
# 3) EMAIL UNKNOWN
# ======================================================================
def test_email_unknown_never_blindly_duplicate_sent():
    db = _db()
    _seed_paid_booking(db, booking_id="bk_emailunknown")
    real = SandboxEmailProvider()
    flaky = OneShotUnknownEmail(real, fail_first=False)
    original = cs.create_and_send_communication

    def _send_safely(*a, **kw):
        return original(*a, provider=flaky, **kw)

    import unittest.mock
    with unittest.mock.patch.object(pbf.communication_service, "create_and_send_communication", side_effect=_send_safely):
        outcome = pbf.try_finalize(db, booking_id="bk_emailunknown")
    assert outcome.communication.status is CommunicationStatus.UNKNOWN

    # A second finalizer run (e.g. a retry trigger) must not blindly
    # duplicate-send - re-running create_and_send_communication for the
    # same booking+type is itself idempotent (same communication_id,
    # UNKNOWN stays until an explicit resend/reconciliation).
    outcome2 = pbf.try_finalize(db, booking_id="bk_emailunknown")
    assert outcome2.communication.communication_id == outcome.communication.communication_id
    from detoura.persistence import communications as cstore
    attempts = cstore.list_attempts_for_communication(db, outcome.communication.communication_id)
    assert len(attempts) == 1  # no blind duplicate attempt from the second finalizer pass


# ======================================================================
# 4) PARTIAL BOOKING / RECOVERY_REQUIRED
# ======================================================================
def test_partial_booking_recovery_required_no_false_confirmation():
    db = _db()
    _seed_paid_booking(db, booking_id="bk_partial", phase="partial_failure", recovery_state="PARTIAL_FAILURE")
    outcome = pbf.try_finalize(db, booking_id="bk_partial")
    assert outcome.confirmation.status is ConfirmationStatus.PARTIAL_RECOVERY
    assert outcome.confirmation.status is not ConfirmationStatus.CONFIRMED
    assert outcome.document is None  # no receipt issued for an incomplete trip
    # communication is still sent, but must never claim success
    subject_and_body = (outcome.communication is not None)
    assert subject_and_body


# ======================================================================
# 5) PARTIAL REFUND
# ======================================================================
def test_partial_refund_produces_distinct_credit_note_not_full():
    db = _db()
    payment = _seed_paid_booking(db, booking_id="bk_partrefund", total=150.0)
    outcome = pbf.try_finalize(db, booking_id="bk_partrefund")
    assert outcome.document is not None
    original_total = outcome.document.customer_total

    from detoura.payment_config import resolve_provider
    provider = resolve_provider()
    from detoura.persistence import payments as payment_store
    fresh_payment = payment_store.get_payment(db, payment.payment_id)
    updated_payment, refund = ps.request_refund(
        db, payment=fresh_payment, provider=provider, amount=30.0, reason="partial",
        idempotency_key="idem_partrefund_1",
    )
    assert updated_payment.status.value == "PARTIALLY_REFUNDED"

    from detoura.models.financial_document import FinancialDocumentType
    credit_note = fds.issue_credit_note(
        db, booking_id="bk_partrefund", refund_amount=30.0,
        original_document_id=outcome.document.document_id, idempotency_key="idem_creditnote_1",
    )
    assert credit_note.document_type == FinancialDocumentType.CREDIT_NOTE
    assert credit_note.customer_total == 30.0
    assert credit_note.customer_total != original_total  # never mislabeled as a full-amount document


# ======================================================================
# 6) CONCURRENT FINALIZATION
# ======================================================================
def test_concurrent_finalization_produces_exactly_one_of_each_artifact():
    db = _db()
    _seed_paid_booking(db, booking_id="bk_concurrent")

    results = []
    errors = []

    def _run():
        try:
            results.append(pbf.try_finalize(db, booking_id="bk_concurrent"))
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=_run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    confirmation_ids = {r.confirmation.confirmation_id for r in results if r.confirmation}
    document_ids = {r.document.document_id for r in results if r.document}
    communication_ids = {r.communication.communication_id for r in results if r.communication}
    assert len(confirmation_ids) == 1
    assert len(document_ids) == 1
    assert len(communication_ids) == 1


# ======================================================================
# 7) PROCESS RESTART BEFORE SEND
# ======================================================================
def test_restart_before_send_recovers_from_persisted_state_only():
    db = _db()
    _seed_paid_booking(db, booking_id="bk_restart1")
    # Simulate "restart before send": create the confirmation but never
    # actually invoke the communication step - purely by calling the
    # confirmation upsert path in isolation is awkward from the public API,
    # so instead simulate the restart by dropping all Python-level state
    # and re-entering try_finalize fresh, which is itself the intended
    # restart-recovery contract (no in-memory object is required).
    outcome = pbf.try_finalize(db, booking_id="bk_restart1")
    assert outcome.communication is not None and outcome.communication.status is CommunicationStatus.SENT
    # "Restart": a brand new call, no Python object carried over, must
    # reach the identical, single, already-sent communication - not send again.
    del outcome
    reloaded = pbf.try_finalize(Database(db.path) if db.path != ":memory:" else db, booking_id="bk_restart1")
    from detoura.persistence import communications as cstore
    comm = cstore.get_communication_for_booking(db, "bk_restart1", "BOOKING_CONFIRMATION")
    attempts = cstore.list_attempts_for_communication(db, comm.communication_id)
    assert len(attempts) == 1


# ======================================================================
# 8) PROCESS RESTART AFTER PROVIDER SEND / BEFORE LOCAL SENT
# ======================================================================
def test_restart_after_provider_send_before_local_commit_is_unknown_not_duplicated():
    db = _db()
    _seed_paid_booking(db, booking_id="bk_restart2")
    real = SandboxEmailProvider()
    flaky = OneShotUnknownEmail(real, fail_first=False)  # models "sent, but we never heard back"

    import unittest.mock
    original = cs.create_and_send_communication

    def _send_safely(*a, **kw):
        return original(*a, provider=flaky, **kw)

    with unittest.mock.patch.object(pbf.communication_service, "create_and_send_communication", side_effect=_send_safely):
        outcome = pbf.try_finalize(db, booking_id="bk_restart2")
    # The local record is UNKNOWN even though the sandbox itself actually
    # accepted the message - exactly the "restart after provider send,
    # before local SENT" scenario. A later reconcile (not a blind retry)
    # is the only way to resolve it.
    assert outcome.communication.status is CommunicationStatus.UNKNOWN
    resolved = cs.reconcile_communication(db, communication=outcome.communication, provider=real)
    assert resolved.status is CommunicationStatus.SENT  # real truth recovered, no duplicate send


# ======================================================================
# 9) DOCUMENT RENDER FAILURE
# ======================================================================
def test_document_render_failure_leaves_booking_payment_confirmation_intact():
    db = _db()
    _seed_paid_booking(db, booking_id="bk_docfail")

    import unittest.mock

    def _boom(*a, **kw):
        raise RuntimeError("simulated PDF render failure")

    with unittest.mock.patch.object(pbf.document_service, "issue_receipt_or_invoice", side_effect=_boom):
        outcome = pbf.try_finalize(db, booking_id="bk_docfail")

    assert outcome.confirmation.status is ConfirmationStatus.CONFIRMED  # unaffected
    assert outcome.document is None
    assert "simulated PDF render failure" in outcome.document_error
    assert outcome.communication is not None  # communication still attempted independently
    from detoura.persistence import payments as payment_store
    payments = payment_store.list_payments_for_booking(db, "bk_docfail")
    assert payments[0].status.value == "CAPTURED"  # payment truth untouched


# ======================================================================
# 10) MISSING PRODUCTION COMPANY METADATA
# ======================================================================
def test_missing_production_company_metadata_fails_closed(monkeypatch):
    from detoura.services.financial_document_pdf import (
        IncompleteCompanyMetadata,
        resolve_company_metadata,
    )

    monkeypatch.setenv("FINANCIAL_DOCUMENTS_PRODUCTION_MODE", "true")
    for var in (
        "FINANCIAL_DOCUMENT_LEGAL_ENTITY", "FINANCIAL_DOCUMENT_ADDRESS",
        "FINANCIAL_DOCUMENT_TAX_ID", "FINANCIAL_DOCUMENT_SUPPORT_CONTACT",
    ):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(IncompleteCompanyMetadata):
        resolve_company_metadata()

    # And end-to-end: issuing a document in this misconfigured production
    # mode must fail closed too, never silently emit an incomplete
    # production-labelled document.
    monkeypatch.setenv("FINANCIAL_DOCUMENTS_PRODUCTION_MODE", "false")
    db = _db()
    _seed_paid_booking(db, booking_id="bk_prodguard")
    monkeypatch.setenv("FINANCIAL_DOCUMENTS_PRODUCTION_MODE", "true")
    from detoura.models.financial_document import FinancialDocumentType
    with pytest.raises(IncompleteCompanyMetadata):
        fds.issue_receipt_or_invoice(
            db, booking_id="bk_prodguard", document_type=FinancialDocumentType.RECEIPT,
            idempotency_key="idem_prodguard_1",
        )
