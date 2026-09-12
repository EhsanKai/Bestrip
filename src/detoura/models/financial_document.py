"""Financial documents - receipt, invoice, credit note (V9 Phase 5).

A financial document is a *rendering* of financial truth that already exists
elsewhere. It computes nothing. Every figure on it is copied from exactly two
pre-existing, already-correct sources:

* :class:`~detoura.persistence.economics.EconomicsRow` - the immutable ledger
  row written once per booking by ``services/booking_commercial.py``. It is
  the truth about what the customer was *priced*: supplier components,
  Detoura's markup and service fee, discount, tax, customer price.
* :class:`~detoura.models.payment.PaymentTransaction` - the truth about what
  money *actually moved*: ``captured_amount`` and ``refunded_amount``.

Nothing in this module (or in the persistence/service/PDF modules beside it)
may recompute a customer price, consult a Bootstrap Market Prior, or import
an optimizer estimate module. That is enforced statically by
``tests/test_v9_phase5_financial_documents.py`` against real import
statements, not by this docstring.

Money convention follows the rest of the codebase exactly: integer minor
units at rest (``models/money.py``), float on the Pydantic model.


Immutability
------------
A document is frozen at construction and, once persisted, has no update
path - ``persistence/financial_documents.py`` deliberately exposes no
``update_document``. Immutability here is structural (the write does not
exist), not a convention someone must remember. Consequently there is no
``version`` field: a version counter only earns its place when something can
change, and nothing can.


Why not ``supersedes_document_id`` for a credit note
----------------------------------------------------
The obvious field name for "this credit note is about receipt X" is
``supersedes_document_id``, and it is wrong. "Supersede" means *replace*: the
superseded document is no longer the operative one and should not be relied
on. That is precisely the opposite of what a credit note does. Receipt X
recorded a true fact - money was captured on this date for this booking - and
that fact stays true forever after a refund. The refund is a *second*, later
fact. A credit note is an addendum to the financial record, not a correction
of it; a reader must hold both documents at once to see what happened, and
invalidating the receipt would erase the capture that the refund refers to.
(Tax authorities generally reason the same way: a credit note is issued
*alongside* the original invoice, which remains valid and on file.)

So this model carries two separate, mutually exclusive relationships:

``adjusts_document_id``
    "This document is *against* that one, which remains fully valid."
    A credit note sets this and only this. Required on ``CREDIT_NOTE``,
    forbidden on ``RECEIPT``/``INVOICE`` (a receipt adjusts nothing).

``supersedes_document_id``
    "That document was wrong; this one replaces it." Genuinely needed - an
    invoice issued with an incorrect address or a mistaken figure cannot be
    edited (see Immutability above), so the only way to correct it is to
    issue a replacement. Reserved for that case, and never used for refunds.

Because the two mean different things, a single document may set at most one
of them.


Why ``FinancialDocumentStatus`` is derived and never stored
-----------------------------------------------------------
Whether document A has been superseded is not a property of A - it is the
existence of some later document B pointing at A. Storing a status column on
A would require *mutating an issued document* to keep it accurate, which is
exactly the invariant this module exists to protect. So status is a query
(``persistence.financial_documents.document_status``), computed from the
relationship, and there is no status column to drift out of sync.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Half-a-cent tolerance for comparing money that has been through float.
#: Mirrors ``services/payment_service.CENTS`` deliberately - two modules
#: disagreeing about what "equal amounts" means is how a full refund gets
#: labelled partial.
CENTS = 0.005


class FinancialDocumentType(str, Enum):
    """The three documents this phase issues, and nothing else.

    ``RECEIPT`` and ``INVOICE`` present the *same* underlying figures from
    the same ``EconomicsRow`` in two different layouts; they are separate
    types rather than a display flag because which one a customer is
    entitled to is a commercial/legal question, not a rendering preference.
    """

    RECEIPT = "RECEIPT"
    """Confirms money was captured. Issued after a successful capture."""
    INVOICE = "INVOICE"
    """The same commercial breakdown in invoice form, for a customer who
    needs a document addressed to them for their own accounting."""
    CREDIT_NOTE = "CREDIT_NOTE"
    """Records that money went *back*. Always ``adjusts`` an earlier
    receipt or invoice, which it never invalidates."""


class FinancialDocumentStatus(str, Enum):
    """A *derived* view of a document, never a stored column.

    See the module docstring: computing this from the relationship graph is
    what lets an issued document stay byte-for-byte immutable.
    """

    ISSUED = "ISSUED"
    """The operative document. The normal and overwhelmingly common state."""
    SUPERSEDED = "SUPERSEDED"
    """A later document declares it replaces this one. Only reachable
    through ``supersedes_document_id``; a credit note never causes this."""


class CreditScope(str, Enum):
    """Whether a credit note covers the whole capture or part of it.

    Derived arithmetically, never asserted - see
    :attr:`FinancialDocument.credit_scope`.
    """

    FULL = "FULL"
    PARTIAL = "PARTIAL"


#: The commercial line items. Either every one is known, or (credit notes
#: only) none is - see :meth:`FinancialDocument._check_components`.
COMPONENT_FIELDS = (
    "supplier_transport",
    "supplier_baggage",
    "supplier_fees",
    "detoura_markup",
    "detoura_service_fee",
    "discount",
    "tax",
)


class FinancialDocument(BaseModel):
    """One issued financial document. Frozen, and never updated after issue.

    All component amounts are ``float | None`` on purpose. ``None`` means
    UNKNOWN, exactly as it does in ``persistence/economics.py`` - it is not
    zero, and it is never a placeholder for a figure we could have looked up
    but did not. The only legitimate use is a partial credit note, where how
    a partial refund apportions across supplier cost, markup, service fee
    and tax is a commercial decision Detoura has not made (see
    ``docs/V9_PHASE5_FINANCIAL_DOCUMENT_LEGAL_NOTES.md``). Inventing a split
    there would be fabricating financial truth, so the document states the
    credited total and leaves the attribution honestly absent.
    """

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1, max_length=64)
    document_type: FinancialDocumentType
    document_number: str = Field(min_length=1, max_length=64)
    """Human-facing reference. See
    ``persistence.financial_documents.allocate_document_number`` for what
    this does and does not promise legally."""
    booking_id: str = Field(min_length=1, max_length=200)
    journey_reference: str = Field(min_length=1, max_length=200)
    user_id: str | None = None
    currency: str = Field(min_length=3, max_length=3)

    supplier_transport: float | None = Field(default=None, ge=0.0)
    supplier_baggage: float | None = Field(default=None, ge=0.0)
    supplier_fees: float | None = Field(default=None, ge=0.0)
    detoura_markup: float | None = Field(default=None, ge=0.0)
    detoura_service_fee: float | None = Field(default=None, ge=0.0)
    discount: float | None = Field(default=None, ge=0.0)
    tax: float | None = Field(default=None, ge=0.0)
    """Whatever the economics ledger recorded as tax. It is a single
    undifferentiated figure with no rate, jurisdiction or per-line
    attribution, which is NOT sufficient for a VAT invoice - a documented
    open item, not a solved problem. Never synthesised here."""

    customer_total: float = Field(ge=0.0)
    """What this document is *for*. Receipt/invoice: the booking's customer
    price. Credit note: the amount credited by this note alone."""
    captured_amount: float = Field(ge=0.0)
    """Money actually captured on the booking, from ``PaymentTransaction``,
    as at issuance. For a credit note this is the ORIGINAL captured amount
    that :attr:`credit_scope` measures against."""
    refunded_amount: float = Field(default=0.0, ge=0.0)
    """Money actually refunded on the booking as at issuance, cumulative
    (a credit note's own amount included)."""

    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    adjusts_document_id: str | None = None
    supersedes_document_id: str | None = None
    is_production: bool = False
    """``False`` marks a sandbox/test document, which the PDF must also
    visibly say. Only ever ``True`` when complete company/legal metadata was
    configured - issuance fails closed otherwise."""

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def _check_refund_never_exceeds_capture(self) -> "FinancialDocument":
        if self.refunded_amount - self.captured_amount > CENTS:
            raise ValueError(
                f"refunded_amount {self.refunded_amount} exceeds captured_amount "
                f"{self.captured_amount}"
            )
        return self

    @model_validator(mode="after")
    def _check_components(self) -> "FinancialDocument":
        """Components are all-known or (credit notes only) all-unknown, and
        when known they must reconcile to ``customer_total``.

        A partially-filled breakdown is rejected outright: a document that
        shows some line items and silently omits others reads as a complete
        breakdown that does not add up, which is worse than no breakdown.
        """
        values = [getattr(self, name) for name in COMPONENT_FIELDS]
        known = [v for v in values if v is not None]
        if known and len(known) != len(values):
            missing = [n for n in COMPONENT_FIELDS if getattr(self, n) is None]
            raise ValueError(
                "a partial line-item breakdown is not a breakdown; either every "
                f"component is known or none is (missing: {', '.join(missing)})"
            )
        if not known:
            if self.document_type is not FinancialDocumentType.CREDIT_NOTE:
                raise ValueError(
                    f"a {self.document_type.value} must carry its full line-item "
                    "breakdown; only a credit note may leave the attribution of "
                    "a partial refund honestly unknown"
                )
            return self

        total = (
            self.supplier_transport
            + self.supplier_baggage
            + self.supplier_fees
            + self.detoura_markup
            + self.detoura_service_fee
            + self.tax
            - self.discount
        )
        if abs(total - self.customer_total) > CENTS:
            raise ValueError(
                f"line items sum to {total:.4f} but customer_total is "
                f"{self.customer_total:.4f}; a document whose figures do not "
                "reconcile is never issued"
            )
        return self

    @model_validator(mode="after")
    def _check_relationships(self) -> "FinancialDocument":
        if self.adjusts_document_id and self.supersedes_document_id:
            raise ValueError(
                "adjusts and supersedes mean different things (addendum vs "
                "replacement); a document may declare at most one"
            )
        for name in ("adjusts_document_id", "supersedes_document_id"):
            if getattr(self, name) == self.document_id:
                raise ValueError(f"{name} must not point at the document itself")

        if self.document_type is FinancialDocumentType.CREDIT_NOTE:
            if not self.adjusts_document_id:
                raise ValueError(
                    "a credit note must name the receipt/invoice it is against "
                    "via adjusts_document_id"
                )
        elif self.adjusts_document_id:
            raise ValueError(
                f"a {self.document_type.value} adjusts nothing; adjusts_document_id "
                "is for credit notes only"
            )
        return self

    @model_validator(mode="after")
    def _check_credit_note_amounts(self) -> "FinancialDocument":
        """A credit note must be classifiable as FULL or PARTIAL by pure
        arithmetic. Anything that would make that ambiguous is refused at
        construction, so :attr:`credit_scope` can never have to guess."""
        if self.document_type is not FinancialDocumentType.CREDIT_NOTE:
            return self
        if self.customer_total <= CENTS:
            raise ValueError("a credit note must credit a positive amount")
        if self.captured_amount <= CENTS:
            raise ValueError(
                "a credit note against a zero/unknown capture cannot be "
                "classified as full or partial; refusing to issue one whose "
                "scope would be a guess"
            )
        if self.customer_total - self.captured_amount > CENTS:
            raise ValueError(
                f"credit note amount {self.customer_total} exceeds the captured "
                f"amount {self.captured_amount} it is against"
            )
        return self

    # ------------------------------------------------------------------
    # Derived views - all pure functions of stored figures
    # ------------------------------------------------------------------
    @property
    def has_line_item_breakdown(self) -> bool:
        return self.supplier_transport is not None

    @property
    def supplier_total(self) -> float | None:
        if not self.has_line_item_breakdown:
            return None
        return round(
            self.supplier_transport + self.supplier_baggage + self.supplier_fees, 2
        )

    @property
    def credit_scope(self) -> CreditScope | None:
        """FULL or PARTIAL for a credit note, ``None`` for anything else.

        Derived *only* by comparing this note's credited amount against the
        original captured amount. There is deliberately no parameter, flag
        or constructor argument by which a caller can assert the answer.

        This is the direct lesson of a real bug in
        ``services/ticket_operations.py``, where the equivalent label was::

            partial = bool((paid and refund_amount + 0.01 < paid)
                           or penalty > 0.01)

        which fails two ways. If ``paid`` is 0/None the first clause is
        falsy, so a refund measured against an unknown total is silently
        labelled FULL. And ``penalty > 0.01`` asserts PARTIAL from a
        side-signal, so a genuinely full refund that merely *has* a penalty
        figure recorded is mislabelled. Here, the zero/unknown-capture case
        is refused at construction and no side-signal exists to consult.
        """
        if self.document_type is not FinancialDocumentType.CREDIT_NOTE:
            return None
        if abs(self.customer_total - self.captured_amount) < CENTS:
            return CreditScope.FULL
        return CreditScope.PARTIAL

    @property
    def net_amount_retained(self) -> float:
        """Captured minus refunded, as at issuance."""
        return round(self.captured_amount - self.refunded_amount, 2)
