"""Issuing financial documents (V9 Phase 5).

This service *combines* two existing sources of truth and computes no money
of its own:

* ``persistence/economics.py`` - the immutable ``EconomicsRow`` written once
  per booking by ``services/booking_commercial.py::finalize_economics``,
  giving the commercial breakdown the customer was priced.
* ``persistence/payments.py`` - the ``PaymentTransaction`` rows, giving what
  was actually captured and refunded.

If the economics row does not exist, issuance fails loudly
(:class:`EconomicsNotAvailable`) rather than reconstructing a breakdown from
anything else. A missing ledger row means the caller ran before the booking
reached its terminal economics-writing state, and the correct response is to
say so, not to produce a plausible document from a booking record or a live
re-quote.

Nothing here imports the Market Prior, the opportunity/beam-search/candidate
funnel machinery, or any optimizer estimate module - statically enforced by
the phase tests.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from ..models.financial_document import (
    CENTS,
    CreditScope,
    FinancialDocument,
    FinancialDocumentType,
)
from ..persistence import bookings as bookings_store
from ..persistence import economics as economics_store
from ..persistence import financial_documents as store
from ..persistence import payments as payments_store
from ..persistence.db import Database
from .financial_document_pdf import (
    CompanyMetadata,
    ItineraryLeg,
    render_document_pdf,
    resolve_company_metadata,
)


class EconomicsNotAvailable(Exception):
    """No ``EconomicsRow`` exists for this booking, so there is no priced
    breakdown to render.

    This is a real signal, not a transient miss: the economics row is
    written exactly once, at booking-terminal time. Seeing this means either
    the finalizer has not run yet (issue the document after it does) or the
    booking never reached a terminal state (in which case no receipt is
    owed). Never satisfied by falling back to another source.
    """


class NoCapturedPayment(Exception):
    """No money has been captured for this booking, so there is nothing to
    receipt or credit."""


class CurrencyMismatch(Exception):
    """A payment's currency differs from the economics ledger's. Refusing to
    mix currencies on one document rather than silently summing them."""


class OriginalDocumentNotFound(Exception):
    """The receipt/invoice a credit note claims to be against does not
    exist, or belongs to a different booking."""


class InvalidCreditAmount(ValueError):
    """The requested credit is not a positive amount within what remains
    refundable."""


def _document_id() -> str:
    return f"findoc_{secrets.token_urlsafe(16)}"


def _payment_facts(
    db: Database, booking_id: str, currency: str
) -> tuple[float, float, str | None]:
    """``(captured, refunded, owner_user_id)`` across this booking's payments.

    Read from ``PaymentTransaction`` - the record of money that actually
    moved - never inferred from the priced total. One pass, so the totals
    and the owner can never come from two different reads of a table another
    process is writing to.
    """
    captured = 0.0
    refunded = 0.0
    owner: str | None = None
    for payment in payments_store.list_payments_for_booking(db, booking_id):
        if owner is None and payment.user_id:
            owner = payment.user_id
        if payment.captured_amount <= 0 and payment.refunded_amount <= 0:
            continue
        if payment.currency != currency:
            raise CurrencyMismatch(
                f"booking {booking_id}: payment {payment.payment_id} is in "
                f"{payment.currency} but the economics ledger is in {currency}; "
                "refusing to combine currencies on one document"
            )
        captured += payment.captured_amount
        refunded += payment.refunded_amount
    return round(captured, 2), round(refunded, 2), owner


def _itinerary(db: Database, booking_id: str) -> list[ItineraryLeg]:
    """The customer-facing itinerary summary.

    This is the privacy boundary: ``BookingItemRecord`` carries
    ``provider_order_id``, ``offer_id`` and ``provider``, and none of them
    crosses into :class:`ItineraryLeg`. The renderer is therefore
    structurally unable to print them.
    """
    record = bookings_store.get(db, booking_id)
    if record is None:
        return []
    legs: list[ItineraryLeg] = []
    for item in record.items:
        legs.append(
            ItineraryLeg(
                sequence=item.sequence,
                origin=item.origin_city or item.origin_airport,
                destination=item.destination_city or item.destination_airport,
                departure=item.departure,
                arrival=item.arrival,
                carrier=item.carrier_name or item.carrier,
                flight_number=item.flight_number,
            )
        )
    return legs


def _issue(
    db: Database,
    *,
    document: FinancialDocument,
    idempotency_key: str,
    company: CompanyMetadata,
    itinerary: list[ItineraryLeg],
    adjusts_document_number: str | None = None,
) -> FinancialDocument:
    """Render and persist, returning the stored document.

    If a concurrent caller won the same ``idempotency_key``, the stored
    document is returned and ours is discarded - the customer keeps one
    document with one number.
    """
    pdf = render_document_pdf(
        document,
        company=company,
        itinerary=itinerary,
        adjusts_document_number=adjusts_document_number,
    )
    stored, _created = store.issue_document(
        db, document=document, idempotency_key=idempotency_key, pdf_bytes=pdf
    )
    return stored


def issue_receipt_or_invoice(
    db: Database,
    *,
    booking_id: str,
    document_type: FinancialDocumentType,
    idempotency_key: str,
    now: datetime | None = None,
) -> FinancialDocument:
    """Issue a ``RECEIPT`` or ``INVOICE`` for a booking's captured payment.

    Idempotent: the same ``idempotency_key`` returns the document already
    issued, with the same number and the same figures, and never burns a
    second number.
    """
    if document_type is FinancialDocumentType.CREDIT_NOTE:
        raise ValueError("use issue_credit_note for credit notes")

    existing = store.get_document_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        return existing

    row = economics_store.get(db, booking_id)
    if row is None:
        raise EconomicsNotAvailable(
            f"no economics ledger row for booking {booking_id}; the booking has "
            "not reached a terminal economics-writing state (or the finalizer "
            "has not run). Refusing to fabricate a financial document."
        )

    captured, refunded, owner = _payment_facts(db, booking_id, row.currency)
    if captured <= CENTS:
        raise NoCapturedPayment(
            f"booking {booking_id} has no captured payment; there is nothing to "
            f"issue a {document_type.value.lower()} for"
        )

    company, is_production = resolve_company_metadata()
    issued_at = now or datetime.now(timezone.utc)
    number = store.allocate_document_number(
        db, document_type=document_type, issued_at=issued_at
    )

    document = FinancialDocument(
        document_id=_document_id(),
        document_type=document_type,
        document_number=number,
        booking_id=booking_id,
        journey_reference=row.journey_reference,
        user_id=owner,
        currency=row.currency,
        # Straight from the ledger. Not recomputed, not rounded differently,
        # not re-derived from a live quote.
        supplier_transport=row.supplier_transport,
        supplier_baggage=row.supplier_baggage,
        supplier_fees=row.supplier_fees,
        detoura_markup=row.markup,
        detoura_service_fee=row.service_fee,
        discount=row.discount,
        tax=row.tax,
        customer_total=row.customer_price,
        captured_amount=captured,
        refunded_amount=refunded,
        issued_at=issued_at,
        is_production=is_production,
    )
    return _issue(
        db,
        document=document,
        idempotency_key=idempotency_key,
        company=company,
        itinerary=_itinerary(db, booking_id),
    )


def issue_credit_note(
    db: Database,
    *,
    booking_id: str,
    refund_amount: float,
    original_document_id: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> FinancialDocument:
    """Issue a ``CREDIT_NOTE`` for a refund against an earlier document.

    **Full vs partial is never asserted by the caller.** There is no
    ``partial=`` parameter and no flag on the result. The distinction is a
    pure arithmetic comparison of ``refund_amount`` against the captured
    amount actually recorded on the payment transactions, performed by
    :attr:`FinancialDocument.credit_scope`.

    That is a direct response to the bug this codebase already had in
    ``services/ticket_operations.py``::

        partial = bool((paid and refund_amount + 0.01 < paid)
                       or penalty > 0.01)

    where a falsy ``paid`` made an unmeasurable refund read as FULL, and a
    non-zero penalty asserted PARTIAL regardless of the amounts. Here, a
    zero/unknown capture raises rather than defaulting either way, and there
    is no side-signal in scope to consult.

    The credit note ``adjusts`` the original; it does not supersede it. The
    original receipt still truthfully records that money was captured.
    """
    existing = store.get_document_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        return existing

    row = economics_store.get(db, booking_id)
    if row is None:
        raise EconomicsNotAvailable(
            f"no economics ledger row for booking {booking_id}; refusing to "
            "issue a credit note against a booking with no commercial record."
        )

    original = store.get_document(db, original_document_id)
    if original is None or original.booking_id != booking_id:
        raise OriginalDocumentNotFound(
            f"{original_document_id!r} is not a document of booking {booking_id}"
        )
    if original.document_type is FinancialDocumentType.CREDIT_NOTE:
        raise OriginalDocumentNotFound(
            "a credit note is issued against a receipt or invoice, not against "
            "another credit note"
        )

    captured, refunded_to_date, _owner = _payment_facts(db, booking_id, row.currency)
    if captured <= CENTS:
        # The historical bug's blind spot, made explicit: with nothing
        # captured there is no denominator, so "full or partial?" has no
        # answer and we refuse instead of defaulting to either.
        raise NoCapturedPayment(
            f"booking {booking_id} has no captured payment to credit against; "
            "a refund's scope cannot be classified against a zero capture"
        )

    amount = round(float(refund_amount), 2)
    if amount <= CENTS:
        raise InvalidCreditAmount("a credit note must credit a positive amount")

    # Over-crediting is guarded against the credit notes ALREADY ISSUED, not
    # against the payment's refunded_amount. The two orderings - refund the
    # payment then document it, or document it then refund - are both real,
    # and keying the guard off payment state would reject the first ordering
    # outright (the refund is already counted there) while under-counting the
    # second. How much has been credited is a fact about documents, which is
    # this module's own domain.
    already_credited = round(
        sum(
            d.customer_total
            for d in store.list_documents_for_booking(db, booking_id)
            if d.document_type is FinancialDocumentType.CREDIT_NOTE
        ),
        2,
    )
    remaining = round(captured - already_credited, 2)
    if amount - remaining > CENTS:
        raise InvalidCreditAmount(
            f"cannot credit {amount:.2f}: {already_credited:.2f} of the "
            f"{captured:.2f} captured is already credited, leaving "
            f"{remaining:.2f}"
        )

    company, is_production = resolve_company_metadata()
    issued_at = now or datetime.now(timezone.utc)
    number = store.allocate_document_number(
        db, document_type=FinancialDocumentType.CREDIT_NOTE, issued_at=issued_at
    )

    # Whether this credit is FULL or PARTIAL follows from the arithmetic
    # below; it is computed here only to decide whether a line-item
    # breakdown can honestly be stated, never to label the document.
    credits_whole_capture = abs(amount - captured) < CENTS
    if credits_whole_capture:
        # Everything comes back, so every original line item comes back with
        # it - a faithful mirror, still reconciling exactly.
        components = dict(
            supplier_transport=original.supplier_transport,
            supplier_baggage=original.supplier_baggage,
            supplier_fees=original.supplier_fees,
            detoura_markup=original.detoura_markup,
            detoura_service_fee=original.detoura_service_fee,
            discount=original.discount,
            tax=original.tax,
        )
        # Only mirror when the mirrored items actually reconcile to the
        # credited amount; otherwise fall back to stating no attribution.
        mirrored_total = (
            (components["supplier_transport"] or 0.0)
            + (components["supplier_baggage"] or 0.0)
            + (components["supplier_fees"] or 0.0)
            + (components["detoura_markup"] or 0.0)
            + (components["detoura_service_fee"] or 0.0)
            + (components["tax"] or 0.0)
            - (components["discount"] or 0.0)
        )
        if (
            not original.has_line_item_breakdown
            or abs(mirrored_total - amount) > CENTS
        ):
            components = dict.fromkeys(components, None)
    else:
        # A partial refund. How it apportions across supplier cost, markup,
        # service fee and tax is a commercial decision Detoura has not made
        # (see the Phase 5 legal notes). UNKNOWN, never an invented split.
        components = dict(
            supplier_transport=None, supplier_baggage=None, supplier_fees=None,
            detoura_markup=None, detoura_service_fee=None, discount=None, tax=None,
        )

    document = FinancialDocument(
        document_id=_document_id(),
        document_type=FinancialDocumentType.CREDIT_NOTE,
        document_number=number,
        booking_id=booking_id,
        journey_reference=row.journey_reference,
        user_id=original.user_id,
        currency=row.currency,
        customer_total=amount,
        captured_amount=captured,
        # Money-movement truth as at issuance, read from PaymentTransaction -
        # never this note's amount added to a previous reading, which would
        # double-count whenever the refund has already settled.
        refunded_amount=refunded_to_date,
        issued_at=issued_at,
        adjusts_document_id=original.document_id,
        is_production=is_production,
        **components,
    )
    return _issue(
        db,
        document=document,
        idempotency_key=idempotency_key,
        company=company,
        itinerary=_itinerary(db, booking_id),
        adjusts_document_number=original.document_number,
    )


def credit_scope_for(document: FinancialDocument) -> CreditScope | None:
    """Convenience re-export of the derived scope, for callers that would
    otherwise be tempted to recompute it. There is exactly one
    implementation, on the model."""
    return document.credit_scope
