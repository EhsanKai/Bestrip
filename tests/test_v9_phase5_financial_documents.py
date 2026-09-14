"""V9 Phase 5 — financial documents: immutability, refund labelling,
numbering concurrency, arithmetic exactness, deterministic PDF, fail-closed
production metadata.
"""

from __future__ import annotations

import ast
import re
import threading
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from detoura.models.commercial import (
    CommercialQuote,
    PriceBreakdown,
    PricingPolicyRef,
    ServiceTier,
)
from detoura.models.financial_document import (
    CENTS,
    CreditScope,
    FinancialDocument,
    FinancialDocumentStatus,
    FinancialDocumentType,
)
from detoura.models.payment import PaymentStatus, PaymentTransaction
from detoura.persistence import bookings as bookings_store
from detoura.persistence import economics as economics_store
from detoura.persistence import financial_documents as store
from detoura.persistence import payments as payments_store
from detoura.persistence.bookings import BookingItemRecord, BookingRecord
from detoura.persistence.db import Database
from detoura.services import financial_document_service as service
from detoura.services.financial_document_pdf import (
    SANDBOX_WATERMARK,
    CompanyMetadata,
    IncompleteCompanyMetadata,
    ItineraryLeg,
    render_document_pdf,
    resolve_company_metadata,
)

BOOKING = "bk_phase5"
JOURNEY = "jr_phase5"


# ======================================================================
# Fixtures — own in-memory schema, without editing persistence/db.py
# ======================================================================
@pytest.fixture()
def db() -> Database:
    database = Database(":memory:")
    store.apply_schema(database)
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _clean_company_env(monkeypatch):
    """Sandbox by default, and no leakage between tests."""
    for name in (
        "FINANCIAL_DOCUMENTS_PRODUCTION_MODE",
        "FINANCIAL_DOCUMENT_COMPANY_NAME",
        "FINANCIAL_DOCUMENT_LEGAL_ENTITY",
        "FINANCIAL_DOCUMENT_ADDRESS",
        "FINANCIAL_DOCUMENT_TAX_ID",
        "FINANCIAL_DOCUMENT_SUPPORT_CONTACT",
    ):
        monkeypatch.delenv(name, raising=False)


def _quote(
    *,
    transport: float = 480.00,
    baggage: float = 45.00,
    fees: float = 12.50,
    service_fee: float = 29.00,
    markup: float = 33.75,
    discount: float = 15.00,
    tax: float = 13.11,
) -> CommercialQuote:
    return CommercialQuote(
        service_tier=ServiceTier.BASIC,
        breakdown=PriceBreakdown(
            currency="EUR",
            supplier_transport=transport,
            supplier_baggage=baggage,
            supplier_fees=fees,
            detoura_service_fee=service_fee,
            detoura_markup=markup,
            discount=discount,
            tax=tax,
        ),
        markup_policy=PricingPolicyRef(policy_id="default", version=1),
    )


def _seed_economics(db: Database, *, quote: CommercialQuote | None = None) -> float:
    quote = quote or _quote()
    economics_store.write_snapshot(
        db, booking_id=BOOKING, journey_reference=JOURNEY,
        quote=quote, snapshot={},
    )
    return quote.customer_total


def _seed_payment(
    db: Database, *, captured: float, refunded: float = 0.0,
    user_id: str | None = "usr_1", suffix: str = "1",
) -> PaymentTransaction:
    payment = PaymentTransaction(
        payment_id=f"pay_{suffix}", journey_reference=JOURNEY, booking_id=BOOKING,
        user_id=user_id, checkout_snapshot_id="snap_1", currency="EUR",
        customer_total=captured, status=PaymentStatus.CAPTURED, provider="sandbox",
        idempotency_key=f"idem_pay_key_{suffix}",
        authorized_amount=captured, captured_amount=captured,
        refunded_amount=refunded,
    )
    payments_store.create_payment(db, payment=payment)
    return payment


def _seed_booking(db: Database) -> None:
    now = datetime.now(timezone.utc)
    bookings_store.upsert(db, BookingRecord(
        booking_id=BOOKING, journey_reference=JOURNEY, created_at=now,
        updated_at=now, mode="LIVE", phase="COMPLETED", trip_label="Paris - Rome",
        lead_name="A Traveller", lead_email="t@example.com",
        items=[
            BookingItemRecord(
                sequence=1, origin_city="Paris", origin_airport="CDG",
                destination_city="Rome", destination_airport="FCO",
                departure=now + timedelta(days=30),
                arrival=now + timedelta(days=30, hours=2),
                carrier="AF", carrier_name="Air France", flight_number="1104",
                state="BOOKED", provider="duffel",
                provider_order_id="ord_SECRET_PROVIDER_ID",
                offer_id="off_SECRET_OFFER_ID",
            ),
        ],
    ))


def _document(**kw) -> FinancialDocument:
    defaults = dict(
        document_id="findoc_a", document_type=FinancialDocumentType.RECEIPT,
        document_number="RCPT-2026-000001", booking_id=BOOKING,
        journey_reference=JOURNEY, currency="EUR",
        supplier_transport=480.00, supplier_baggage=45.00, supplier_fees=12.50,
        detoura_service_fee=29.00, detoura_markup=33.75, discount=15.00,
        tax=13.11, customer_total=598.36, captured_amount=598.36,
        issued_at=datetime(2026, 3, 4, 9, 30, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return FinancialDocument(**defaults)


# ======================================================================
# 1. Immutability
# ======================================================================
def test_issued_document_is_frozen(db):
    doc = _document()
    with pytest.raises(ValidationError):
        doc.customer_total = 1.0
    with pytest.raises(ValidationError):
        doc.document_number = "RCPT-2026-000999"


def test_persistence_exposes_no_update_path():
    """Immutability is structural: there is no write path but issue."""
    writers = [
        name for name in dir(store)
        if any(name.startswith(p) for p in ("update", "set_", "delete", "edit", "patch"))
    ]
    assert writers == [], f"financial document store must expose no mutator: {writers}"


def test_stored_document_round_trips_unchanged(db):
    doc = _document()
    stored, created = store.issue_document(
        db, document=doc, idempotency_key="idem_findoc_key_1"
    )
    assert created is True
    assert store.get_document(db, doc.document_id) == stored == doc


def test_duplicate_idempotency_key_returns_the_same_document(db):
    first, created_a = store.issue_document(
        db, document=_document(), idempotency_key="idem_findoc_key_1"
    )
    second, created_b = store.issue_document(
        db,
        document=_document(document_id="findoc_b", document_number="RCPT-2026-000002"),
        idempotency_key="idem_findoc_key_1",
    )
    assert created_a is True and created_b is False
    assert second == first, "a retry must return the original, not a second document"
    assert len(store.list_documents_for_booking(db, BOOKING)) == 1


def test_credit_note_does_not_supersede_the_receipt(db):
    receipt, _ = store.issue_document(
        db, document=_document(), idempotency_key="idem_findoc_key_1"
    )
    store.issue_document(
        db,
        document=_document(
            document_id="findoc_cn", document_type=FinancialDocumentType.CREDIT_NOTE,
            document_number="CN-2026-000001", customer_total=598.36,
            adjusts_document_id=receipt.document_id,
        ),
        idempotency_key="idem_findoc_key_2",
    )
    # The capture the receipt records still happened.
    assert store.document_status(db, receipt.document_id) is FinancialDocumentStatus.ISSUED
    assert len(store.list_credit_notes_against(db, receipt.document_id)) == 1


def test_supersede_is_derived_not_stored(db):
    original, _ = store.issue_document(
        db, document=_document(), idempotency_key="idem_findoc_key_1"
    )
    store.issue_document(
        db,
        document=_document(
            document_id="findoc_fix", document_number="RCPT-2026-000002",
            supersedes_document_id=original.document_id,
        ),
        idempotency_key="idem_findoc_key_2",
    )
    assert store.document_status(db, original.document_id) is FinancialDocumentStatus.SUPERSEDED
    # ...without the original row having been touched.
    assert store.get_document(db, original.document_id) == original


def test_a_document_cannot_both_adjust_and_supersede():
    with pytest.raises(ValidationError):
        _document(
            document_type=FinancialDocumentType.CREDIT_NOTE,
            adjusts_document_id="findoc_x", supersedes_document_id="findoc_y",
        )


def test_receipt_may_not_adjust_anything():
    with pytest.raises(ValidationError):
        _document(adjusts_document_id="findoc_x")


def test_credit_note_must_name_what_it_is_against():
    with pytest.raises(ValidationError):
        _document(
            document_type=FinancialDocumentType.CREDIT_NOTE,
            supplier_transport=None, supplier_baggage=None, supplier_fees=None,
            detoura_service_fee=None, detoura_markup=None, discount=None, tax=None,
            customer_total=50.0,
        )


# ======================================================================
# 2. Partial vs full refund — the ticket_operations.py regression
# ======================================================================
def test_full_credit_note_is_labelled_full():
    doc = _document(
        document_id="findoc_cn", document_type=FinancialDocumentType.CREDIT_NOTE,
        document_number="CN-2026-000001", adjusts_document_id="findoc_a",
        customer_total=598.36, captured_amount=598.36,
    )
    assert doc.credit_scope is CreditScope.FULL


def test_partial_credit_note_is_labelled_partial():
    doc = _document(
        document_id="findoc_cn", document_type=FinancialDocumentType.CREDIT_NOTE,
        document_number="CN-2026-000001", adjusts_document_id="findoc_a",
        supplier_transport=None, supplier_baggage=None, supplier_fees=None,
        detoura_service_fee=None, detoura_markup=None, discount=None, tax=None,
        customer_total=100.00, captured_amount=598.36,
    )
    assert doc.credit_scope is CreditScope.PARTIAL


def test_regression_full_refund_with_a_penalty_is_never_labelled_partial(db):
    """Regression for the ticket_operations.py partial/full bug.

    The historical code was::

        partial = bool((paid and refund_amount + 0.01 < paid) or penalty > 0.01)

    so a *fully* refunded order that merely had a penalty figure recorded
    came out labelled PARTIAL. Here a penalty is not a thing the scope can
    consult: the label is a comparison of the credited amount against the
    captured amount and nothing else. The economics ledger carries a
    penalty-shaped cost field (``recovery_cost``), so we set one and prove
    it changes nothing.
    """
    _seed_economics(db)
    _seed_booking(db)
    total = 598.36
    _seed_payment(db, captured=total)
    economics_store.update_costs(
        db, BOOKING, actor="test", recovery_cost=42.00, payment_cost=3.50,
    )

    receipt = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    note = service.issue_credit_note(
        db, booking_id=BOOKING, refund_amount=total,
        original_document_id=receipt.document_id,
        idempotency_key="idem_credit_key_1",
    )
    assert note.credit_scope is CreditScope.FULL, (
        "a penalty/recovery cost must not make a full refund read as partial"
    )


def test_regression_scope_cannot_be_asserted_by_a_caller():
    """No parameter, anywhere, lets a caller declare the label."""
    import inspect

    signature = inspect.signature(service.issue_credit_note)
    for name in signature.parameters:
        assert "partial" not in name.lower(), name
        assert "full" not in name.lower(), name
    assert "partial" not in FinancialDocument.model_fields
    assert "credit_scope" not in FinancialDocument.model_fields, (
        "scope must be derived, never a stored field"
    )


def test_regression_zero_capture_never_defaults_to_full(db):
    """The historical bug's other half: a falsy ``paid`` silently produced a
    FULL label. Here it is refused outright."""
    with pytest.raises(ValidationError):
        _document(
            document_id="findoc_cn",
            document_type=FinancialDocumentType.CREDIT_NOTE,
            document_number="CN-2026-000001", adjusts_document_id="findoc_a",
            supplier_transport=None, supplier_baggage=None, supplier_fees=None,
            detoura_service_fee=None, detoura_markup=None, discount=None, tax=None,
            customer_total=50.0, captured_amount=0.0,
        )

    # ...and the service refuses too, rather than issuing a "full" credit
    # note against a capture of nothing.
    _seed_economics(db)
    _seed_payment(db, captured=0.0, refunded=0.0)
    original, _ = store.issue_document(
        db, document=_document(captured_amount=0.0),
        idempotency_key="idem_findoc_key_1",
    )
    with pytest.raises(service.NoCapturedPayment):
        service.issue_credit_note(
            db, booking_id=BOOKING, refund_amount=10.0,
            original_document_id=original.document_id,
            idempotency_key="idem_credit_key_9",
        )


def test_service_derived_scope_partial_and_full_differ(db):
    _seed_economics(db)
    _seed_booking(db)
    total = 598.36
    _seed_payment(db, captured=total)
    receipt = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    partial = service.issue_credit_note(
        db, booking_id=BOOKING, refund_amount=100.00,
        original_document_id=receipt.document_id,
        idempotency_key="idem_credit_key_1",
    )
    assert partial.credit_scope is CreditScope.PARTIAL
    # A partial credit note must not invent an apportionment.
    assert partial.has_line_item_breakdown is False
    assert partial.tax is None

    remainder = service.issue_credit_note(
        db, booking_id=BOOKING, refund_amount=round(total - 100.00, 2),
        original_document_id=receipt.document_id,
        idempotency_key="idem_credit_key_2",
    )
    # Still PARTIAL: it credits part of the capture, not all of it.
    assert remainder.credit_scope is CreditScope.PARTIAL

    with pytest.raises(service.InvalidCreditAmount):
        service.issue_credit_note(
            db, booking_id=BOOKING, refund_amount=0.50,
            original_document_id=receipt.document_id,
            idempotency_key="idem_credit_key_3",
        )


def test_credit_note_cannot_exceed_the_capture(db):
    _seed_economics(db)
    _seed_payment(db, captured=598.36)
    receipt = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    with pytest.raises(service.InvalidCreditAmount):
        service.issue_credit_note(
            db, booking_id=BOOKING, refund_amount=999.00,
            original_document_id=receipt.document_id,
            idempotency_key="idem_credit_key_1",
        )


def test_full_credit_note_mirrors_the_original_line_items(db):
    _seed_economics(db)
    total = 598.36
    _seed_payment(db, captured=total)
    receipt = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    note = service.issue_credit_note(
        db, booking_id=BOOKING, refund_amount=total,
        original_document_id=receipt.document_id,
        idempotency_key="idem_credit_key_1",
    )
    assert note.credit_scope is CreditScope.FULL
    assert note.has_line_item_breakdown is True
    assert note.supplier_transport == receipt.supplier_transport
    assert note.tax == receipt.tax


def test_full_credit_note_drops_mirror_when_it_would_not_reconcile(db):
    """Adversarial QA claim: a FULL credit note against a receipt whose
    captured amount never reached the priced total must not mirror the
    original's line items when they would sum to something other than the
    amount actually being credited.

    Reproduces: receipt issued against a partial capture (so its line items
    reconcile to the *priced* ``customer_total``, not to what was captured);
    later the booking is credited in full for only what was ever actually
    captured (less than the priced total). A naive mirror would restate the
    original's full-price line items on a document whose own total is the
    smaller captured amount - internally inconsistent. The service must
    detect that and fall back to an honest "unknown attribution" credit note
    instead, exactly as it already does for a partial refund.
    """
    total = _seed_economics(db)  # priced total, e.g. 598.36
    only_ever_captured = round(total - 48.36, 2)
    _seed_payment(db, captured=only_ever_captured)
    receipt = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    # The receipt's own line items reconcile to the full priced total, which
    # is *not* what was captured - this is the pre-existing, legitimate case
    # of a receipt issued against a partial capture.
    assert receipt.customer_total == total
    assert receipt.captured_amount == only_ever_captured

    note = service.issue_credit_note(
        db, booking_id=BOOKING, refund_amount=only_ever_captured,
        original_document_id=receipt.document_id,
        idempotency_key="idem_credit_key_1",
    )
    assert note.credit_scope is CreditScope.FULL
    assert note.customer_total == only_ever_captured
    # Must NOT silently mirror the original's (larger, non-reconciling)
    # line items onto a smaller total - that would be an internally
    # inconsistent document.
    assert note.has_line_item_breakdown is False
    assert note.supplier_transport is None
    assert note.tax is None


# ======================================================================
# 3. Static import isolation
# ======================================================================
def test_financial_document_domain_never_imports_market_prior_or_optimizer():
    """Static proof against real import statements, not docstring prose."""
    import detoura.models.financial_document as model_module
    import detoura.persistence.financial_documents as store_module
    import detoura.services.financial_document_pdf as pdf_module
    import detoura.services.financial_document_service as service_module

    forbidden = (
        "market_prior", "opportunity", "beam_search", "candidate_funnel",
        "acquisition", "reoptimizer", "planner", "baseline",
    )
    for module in (model_module, store_module, service_module, pdf_module):
        with open(module.__file__) as handle:
            tree = ast.parse(handle.read())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{a.name}" for a in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for name in imported:
            assert not any(f in name for f in forbidden), (
                f"{module.__name__} must not import {name!r}"
            )


# ======================================================================
# 4. Concurrent document numbering
# ======================================================================
def test_concurrent_numbering_is_unique_and_gapless(db):
    threads_count = 12
    per_thread = 6
    issued_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    results: list[list[str]] = [[] for _ in range(threads_count)]
    errors: list[BaseException] = []
    start = threading.Barrier(threads_count)

    def worker(index: int) -> None:
        try:
            start.wait(timeout=10)
            for _ in range(per_thread):
                results[index].append(
                    store.allocate_document_number(
                        db,
                        document_type=FinancialDocumentType.RECEIPT,
                        issued_at=issued_at,
                    )
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    numbers = [n for batch in results for n in batch]
    expected = threads_count * per_thread
    assert len(numbers) == expected
    assert len(set(numbers)) == expected, "a number was handed out twice"

    sequences = sorted(int(n.rsplit("-", 1)[1]) for n in numbers)
    assert sequences == list(range(1, expected + 1)), (
        "the sequence must be contiguous - a gap means a lost increment"
    )
    assert all(n.startswith("RCPT-2026-") for n in numbers)


def test_numbering_series_are_independent_per_type_and_year(db):
    receipt = store.allocate_document_number(
        db, document_type=FinancialDocumentType.RECEIPT,
        issued_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    invoice = store.allocate_document_number(
        db, document_type=FinancialDocumentType.INVOICE,
        issued_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    note = store.allocate_document_number(
        db, document_type=FinancialDocumentType.CREDIT_NOTE,
        issued_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    next_year = store.allocate_document_number(
        db, document_type=FinancialDocumentType.RECEIPT,
        issued_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    assert receipt == "RCPT-2026-000001"
    assert invoice == "INV-2026-000001"
    assert note == "CN-2026-000001"
    assert next_year == "RCPT-2027-000001"
    assert len({receipt, invoice, note, next_year}) == 4


def test_concurrent_issuance_produces_distinct_documents(db):
    _seed_economics(db)
    _seed_payment(db, captured=598.36)
    errors: list[BaseException] = []
    issued: list[FinancialDocument] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker(index: int) -> None:
        try:
            start.wait(timeout=10)
            doc = service.issue_receipt_or_invoice(
                db, booking_id=BOOKING,
                document_type=FinancialDocumentType.RECEIPT,
                idempotency_key=f"idem_conc_key_{index}",
            )
            with lock:
                issued.append(doc)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len({d.document_number for d in issued}) == 8
    assert len({d.document_id for d in issued}) == 8


# ======================================================================
# 5. Arithmetic exactness
# ======================================================================
def test_line_items_reconcile_to_customer_total():
    doc = _document()
    total = (
        doc.supplier_transport + doc.supplier_baggage + doc.supplier_fees
        + doc.detoura_service_fee + doc.detoura_markup + doc.tax - doc.discount
    )
    assert abs(total - doc.customer_total) <= CENTS
    assert doc.supplier_total == 537.50


def test_non_reconciling_document_is_refused():
    with pytest.raises(ValidationError):
        _document(customer_total=600.00)


def test_partial_breakdown_is_refused():
    with pytest.raises(ValidationError):
        _document(tax=None)


def test_receipt_may_not_omit_the_breakdown_entirely():
    with pytest.raises(ValidationError):
        _document(
            supplier_transport=None, supplier_baggage=None, supplier_fees=None,
            detoura_service_fee=None, detoura_markup=None, discount=None, tax=None,
        )


def test_refund_may_not_exceed_capture():
    with pytest.raises(ValidationError):
        _document(captured_amount=598.36, refunded_amount=600.00)


def test_issued_figures_come_straight_from_the_economics_ledger(db):
    quote = _quote()
    _seed_economics(db, quote=quote)
    _seed_payment(db, captured=quote.customer_total)
    doc = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.INVOICE,
        idempotency_key="idem_receipt_key_1",
    )
    row = economics_store.get(db, BOOKING)
    assert doc.supplier_transport == row.supplier_transport
    assert doc.supplier_baggage == row.supplier_baggage
    assert doc.supplier_fees == row.supplier_fees
    assert doc.detoura_markup == row.markup
    assert doc.detoura_service_fee == row.service_fee
    assert doc.discount == row.discount
    assert doc.tax == row.tax
    assert doc.customer_total == row.customer_price
    assert doc.captured_amount == quote.customer_total


def test_minor_unit_round_trip_through_persistence(db):
    quote = _quote(transport=0.01, baggage=0.02, fees=0.03, service_fee=0.04,
                   markup=0.05, discount=0.01, tax=0.06)
    _seed_economics(db, quote=quote)
    _seed_payment(db, captured=quote.customer_total)
    doc = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    assert store.get_document(db, doc.document_id) == doc


def test_missing_economics_row_fails_loudly(db):
    _seed_payment(db, captured=100.0)
    with pytest.raises(service.EconomicsNotAvailable):
        service.issue_receipt_or_invoice(
            db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
            idempotency_key="idem_receipt_key_1",
        )


def test_no_captured_payment_fails_loudly(db):
    _seed_economics(db)
    with pytest.raises(service.NoCapturedPayment):
        service.issue_receipt_or_invoice(
            db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
            idempotency_key="idem_receipt_key_1",
        )


def test_currency_mismatch_is_refused(db):
    _seed_economics(db)
    payments_store.create_payment(db, payment=PaymentTransaction(
        payment_id="pay_usd", journey_reference=JOURNEY, booking_id=BOOKING,
        checkout_snapshot_id="snap_1", currency="USD", customer_total=100.0,
        status=PaymentStatus.CAPTURED, provider="sandbox",
        idempotency_key="idem_pay_usd_key", authorized_amount=100.0,
        captured_amount=100.0,
    ))
    with pytest.raises(service.CurrencyMismatch):
        service.issue_receipt_or_invoice(
            db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
            idempotency_key="idem_receipt_key_1",
        )


def test_service_issuance_is_idempotent(db):
    _seed_economics(db)
    _seed_payment(db, captured=598.36)
    first = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    second = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    assert first == second
    assert len(store.list_documents_for_booking(db, BOOKING)) == 1


# ======================================================================
# 6. Deterministic PDF rendering
# ======================================================================
def _legs() -> list[ItineraryLeg]:
    return [
        ItineraryLeg(
            sequence=1, origin="Paris", destination="Rome",
            departure=datetime(2026, 4, 1, 8, 15, tzinfo=timezone.utc),
            arrival=datetime(2026, 4, 1, 10, 20, tzinfo=timezone.utc),
            carrier="Air France", flight_number="1104",
        ),
    ]


def test_pdf_rendering_is_byte_identical():
    doc = _document()
    company = CompanyMetadata(name="Detoura")
    first = render_document_pdf(doc, company=company, itinerary=_legs())
    second = render_document_pdf(doc, company=company, itinerary=_legs())
    assert first == second, "the same document must render to identical bytes"


def test_pdf_is_a_structurally_valid_document():
    pdf = render_document_pdf(_document(), company=CompanyMetadata(name="Detoura"))
    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert b"/Type /Catalog" in pdf
    assert b"/Type /Page" in pdf
    assert b"xref" in pdf and b"startxref" in pdf
    # The xref offsets must actually point at their objects.
    tail = pdf.rsplit(b"startxref\n", 1)[1]
    xref_offset = int(tail.split(b"\n", 1)[0])
    assert pdf[xref_offset:xref_offset + 4] == b"xref"
    for number in (1, 2, 3):
        assert f"{number} 0 obj".encode() in pdf


def test_pdf_carries_no_generation_timestamp_or_random_id():
    pdf = render_document_pdf(_document(), company=CompanyMetadata(name="Detoura"))
    assert b"/CreationDate" not in pdf
    assert b"/ModDate" not in pdf
    assert b"/ID" not in pdf


def test_pdf_contains_the_required_content():
    doc = _document()
    company = CompanyMetadata(
        name="Detoura", legal_entity="Detoura BV 12345678",
        address="1 Example Street, Amsterdam", tax_id="NL000000000B01",
        support_contact="support@example.com",
    )
    pdf = render_document_pdf(doc, company=company, itinerary=_legs())
    for expected in (
        b"Detoura BV 12345678", b"NL000000000B01", b"RECEIPT",
        b"RCPT-2026-000001", b"2026-03-04", JOURNEY.encode(),
        b"Paris", b"Rome", b"Air France", b"1104",
        b"480.00", b"45.00", b"12.50", b"29.00", b"33.75", b"13.11", b"598.36",
    ):
        assert expected in pdf, expected


def test_pdf_never_leaks_provider_identifiers(db):
    """The renderer is given ItineraryLeg, which has no field for them."""
    _seed_economics(db)
    _seed_booking(db)
    _seed_payment(db, captured=598.36)
    doc = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    pdf = store.get_document_pdf(db, doc.document_id)
    assert pdf is not None
    assert b"ord_SECRET_PROVIDER_ID" not in pdf
    assert b"off_SECRET_OFFER_ID" not in pdf
    assert b"duffel" not in pdf.lower()
    assert {f.name for f in ItineraryLeg.__dataclass_fields__.values()}.isdisjoint(
        {"provider_order_id", "offer_id", "provider"}
    )


def test_stored_pdf_matches_a_fresh_render(db):
    _seed_economics(db)
    _seed_booking(db)
    _seed_payment(db, captured=598.36)
    doc = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    stored = store.get_document_pdf(db, doc.document_id)
    company, _ = resolve_company_metadata()
    fresh = render_document_pdf(
        doc, company=company, itinerary=service._itinerary(db, BOOKING)
    )
    assert stored == fresh


def test_long_itinerary_paginates_rather_than_truncating():
    legs = [
        ItineraryLeg(
            sequence=i, origin=f"Origin City {i}", destination=f"Destination City {i}",
            departure=datetime(2026, 4, 1, 8, 15, tzinfo=timezone.utc),
            arrival=datetime(2026, 4, 1, 10, 20, tzinfo=timezone.utc),
            carrier="Air France", flight_number=f"{1000 + i}",
        )
        for i in range(1, 61)
    ]
    pdf = render_document_pdf(
        _document(), company=CompanyMetadata(name="Detoura"), itinerary=legs
    )
    page_count = int(re.search(rb"/Type /Pages /Kids \[[^\]]*\] /Count (\d+)", pdf)[1])
    assert page_count > 1, "60 legs must not be crammed onto one page"
    # Every page in /Kids must actually exist as an object.
    assert pdf.count(b"/Type /Page /Parent") == page_count
    assert b"Destination City 60" in pdf, "no leg may be silently dropped"
    assert f"Page {page_count} of {page_count}".encode() in pdf
    # The breakdown still renders after the itinerary.
    assert b"598.36" in pdf


def test_credit_note_pdf_states_scope_and_omits_invented_apportionment():
    note = _document(
        document_id="findoc_cn", document_type=FinancialDocumentType.CREDIT_NOTE,
        document_number="CN-2026-000001", adjusts_document_id="findoc_a",
        supplier_transport=None, supplier_baggage=None, supplier_fees=None,
        detoura_service_fee=None, detoura_markup=None, discount=None, tax=None,
        customer_total=100.00, captured_amount=598.36, refunded_amount=100.00,
    )
    pdf = render_document_pdf(
        note, company=CompanyMetadata(name="Detoura"),
        adjusts_document_number="RCPT-2026-000001",
    )
    assert b"CREDIT NOTE" in pdf
    assert b"Partial refund" in pdf
    assert b"RCPT-2026-000001" in pdf
    assert b"not determined" in pdf


# ======================================================================
# 7. Production metadata fails closed; sandbox is visibly marked
# ======================================================================
def _set_full_company(monkeypatch) -> None:
    monkeypatch.setenv("FINANCIAL_DOCUMENT_COMPANY_NAME", "Detoura")
    monkeypatch.setenv("FINANCIAL_DOCUMENT_LEGAL_ENTITY", "Detoura BV 12345678")
    monkeypatch.setenv("FINANCIAL_DOCUMENT_ADDRESS", "1 Example Street, Amsterdam")
    monkeypatch.setenv("FINANCIAL_DOCUMENT_TAX_ID", "NL000000000B01")
    monkeypatch.setenv("FINANCIAL_DOCUMENT_SUPPORT_CONTACT", "support@example.com")


def test_production_without_company_metadata_fails_closed(monkeypatch, db):
    monkeypatch.setenv("FINANCIAL_DOCUMENTS_PRODUCTION_MODE", "true")
    with pytest.raises(IncompleteCompanyMetadata):
        resolve_company_metadata()

    _seed_economics(db)
    _seed_payment(db, captured=598.36)
    with pytest.raises(IncompleteCompanyMetadata):
        service.issue_receipt_or_invoice(
            db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
            idempotency_key="idem_receipt_key_1",
        )
    assert store.list_documents_for_booking(db, BOOKING) == [], (
        "a failed production issuance must leave no document behind"
    )


def test_production_partial_metadata_names_what_is_missing(monkeypatch):
    monkeypatch.setenv("FINANCIAL_DOCUMENTS_PRODUCTION_MODE", "1")
    monkeypatch.setenv("FINANCIAL_DOCUMENT_COMPANY_NAME", "Detoura")
    with pytest.raises(IncompleteCompanyMetadata) as excinfo:
        resolve_company_metadata()
    message = str(excinfo.value)
    for field in ("legal_entity", "address", "tax_id", "support_contact"):
        assert field in message
    assert "name" not in message.replace("legal_entity", "")


def test_production_with_full_metadata_succeeds(monkeypatch, db):
    monkeypatch.setenv("FINANCIAL_DOCUMENTS_PRODUCTION_MODE", "true")
    _set_full_company(monkeypatch)
    _seed_economics(db)
    _seed_payment(db, captured=598.36)
    doc = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    assert doc.is_production is True
    pdf = store.get_document_pdf(db, doc.document_id)
    assert SANDBOX_WATERMARK.encode() not in pdf
    assert b"Detoura BV 12345678" in pdf


def test_sandbox_is_the_default_and_is_visibly_marked(db):
    _seed_economics(db)
    _seed_payment(db, captured=598.36)
    doc = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    assert doc.is_production is False
    pdf = store.get_document_pdf(db, doc.document_id)
    assert SANDBOX_WATERMARK.encode() in pdf
    assert b"not a valid tax document" in pdf


def test_sandbox_tolerates_missing_company_metadata():
    company, is_production = resolve_company_metadata()
    assert is_production is False
    assert company.missing_for_production()  # nothing configured, and that is fine
    pdf = render_document_pdf(_document(), company=company)
    assert SANDBOX_WATERMARK.encode() in pdf


# ======================================================================
# 8. Ownership-checked reads
# ======================================================================
def test_ownership_checked_reads_are_idor_safe(db):
    _seed_economics(db)
    _seed_payment(db, captured=598.36, user_id="usr_owner")
    doc = service.issue_receipt_or_invoice(
        db, booking_id=BOOKING, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key="idem_receipt_key_1",
    )
    assert doc.user_id == "usr_owner"
    assert store.get_document_for_user(db, doc.document_id, user_id="usr_owner") == doc
    assert store.get_document_for_user(db, doc.document_id, user_id="usr_other") is None
    assert store.get_document_for_user(db, "findoc_nope", user_id="usr_owner") is None
    assert store.get_document_pdf_for_user(
        db, doc.document_id, user_id="usr_other"
    ) is None
    assert store.get_document_pdf_for_user(
        db, doc.document_id, user_id="usr_owner"
    ) is not None
