# V9 Phase 5 — Financial documents: legal and tax open items

**Status: none of the items below are implemented.** This document exists so
that the gap between "Detoura can produce a receipt-shaped PDF" and "Detoura
issues legally valid invoices" is written down explicitly rather than
discovered by a customer, an accountant or a tax authority.

Scope: `models/financial_document.py`,
`persistence/financial_documents.py`, `services/financial_document_service.py`,
`services/financial_document_pdf.py`.

## What Phase 5 actually delivers

A deterministic, immutable, idempotently-issued document record with a PDF
rendering, built by combining two sources of truth that already existed and
were already correct:

- `EconomicsRow` (`persistence/economics.py`) — the commercial breakdown the
  customer was priced, written once at booking-terminal time.
- `PaymentTransaction` (`persistence/payments.py`) — what money actually
  moved (`captured_amount`, `refunded_amount`).

No figure on any document is computed by this phase. If the economics row is
absent, issuance raises `EconomicsNotAvailable` rather than reconstructing a
breakdown from anything else.

By default every document is issued in **sandbox mode**: `is_production` is
`False` and the PDF carries `TEST DOCUMENT - NOT FOR TAX PURPOSES` at the top
and again in the footer. Production issuance
(`FINANCIAL_DOCUMENTS_PRODUCTION_MODE=true`) **fails closed** if any required
company/legal metadata is missing.

---

## Open items

### 1. Document numbering is legally neutral, not legally compliant

`allocate_document_number` produces `RCPT-2026-000123` — a Detoura-internal
reference. It is concurrency-safe and unique, and that is all it claims.

It does **not** satisfy the sequential-numbering rules most jurisdictions
apply to invoices:

- **Gaps are possible.** A number is allocated immediately before the insert.
  A crash, or losing an idempotency race, between the two consumes a number
  that never appears on a document. Many tax regimes require an unbroken
  sequence, or require gaps to be explained.
- **No registered series.** The series resets each calendar year and is keyed
  by document *type*, not by legal entity, establishment, or a series
  registered with an authority.
- **Not reported anywhere.** Several jurisdictions require invoice series to
  be declared, or invoices to be transmitted to a tax authority at or near
  issuance (see item 6).
- **Receipts and credit notes share the mechanism** with invoices, though the
  legal requirements on each differ.

**Open:** decide the legal entity/entities, then design a per-entity,
gapless, auditable numbering scheme with an explicit gap-reconciliation
procedure.

### 2. `tax` is a single undifferentiated figure — not a VAT breakdown

`EconomicsRow.tax` is one number. A compliant VAT invoice generally needs, at
minimum: the taxable amount per rate, the rate(s) applied, the tax amount per
rate, the total tax, and the supplier's and (often) the customer's VAT
identification numbers.

Detoura currently has **none** of that structure. The document therefore
prints the single `tax` figure as-is and never derives a rate from it. The
model permits `tax` to be `None` (UNKNOWN) and **never** synthesises a value
— inventing a tax figure or back-computing a rate from a total is the single
most damaging thing this code could do.

**Open:** model tax properly (rate, jurisdiction, taxable base per rate) in
the commercial engine — upstream of this phase — before any document is
presented as a tax invoice.

### 3. Travel-specific VAT treatment is entirely unaddressed

Travel is a hard VAT case and Detoura has made no determination on any of it:

- **Margin schemes** (e.g. the EU Tour Operators' Margin Scheme, TOMS, and
  its equivalents). Where they apply, VAT is due on the *margin*, not the
  gross, and the invoice must not show input VAT. Whether Detoura is acting
  as a principal or a disclosed agent drives this, and that determination has
  not been made.
- **Agent vs principal.** If Detoura is a disclosed agent, the supplier's fare
  is arguably not Detoura's turnover at all, and only the service fee and
  markup are. The current breakdown presents supplier cost and Detoura's
  revenue on one document to one customer, which suits a principal model and
  may misrepresent an agency one.
- **Place of supply** for the service fee, which varies with customer
  location and status (B2C vs B2B).
- **International passenger transport**, which is zero-rated or exempt in many
  jurisdictions, unlike the service fee attached to it.

**Open:** obtain a tax determination per market before enabling production
issuance in that market.

### 4. Partial-refund apportionment is deliberately not invented

When a credit note credits less than the full captured amount, how that
refund apportions across supplier cost, Detoura markup, service fee, discount
and tax is a **commercial and tax decision that has not been made**. Is the
service fee refundable? Is the markup? Does tax abate proportionally or per
component?

Rather than guess, a partial credit note carries **no** line-item breakdown:
every component field is `None` (UNKNOWN, in the same sense
`persistence/economics.py` uses it — explicitly not zero), and the PDF states
that the apportionment is not determined. A full credit note mirrors the
original's line items exactly, which reconciles by construction.

This is honest but **not sufficient for a compliant credit note**, which
generally must show the tax being reversed.

**Open:** define the refund apportionment policy, then represent it
explicitly — never as a default split applied silently.

### 5. Customer identity is not on the document

A compliant invoice generally names and addresses the customer, and for B2B
carries their VAT number. Detoura holds only a lead name and email
(`persistence/bookings.py`, deliberately minimal PII), and the document model
carries `user_id` but no billing address or customer tax id.

The current documents are therefore closer to a **simplified receipt** than
an invoice. Whether simplified invoicing is available depends on jurisdiction
and on the amount — several regimes cap it (commonly a few hundred euro),
above which a full invoice with customer details is mandatory.

**Open:** decide whether to collect billing identity, and gate `INVOICE`
issuance on having it.

### 6. E-invoicing, clearance and archival mandates

A growing number of jurisdictions mandate structured e-invoicing (e.g.
UBL/CII via Peppol) and/or real-time clearance or reporting to a tax
authority before or at issuance. A PDF is not a structured e-invoice.

Retention rules are also unaddressed: many regimes require invoices to be
retained in their original form for 5–10 years, with integrity and
authenticity guarantees.

Phase 5 stores the exact issued PDF bytes as a BLOB in the same transaction
as the document row, so the artifact cannot go missing and the row cannot be
edited (there is no `update_document`). That is a good foundation for
retention but is **not** an accredited archival solution, and there is no
retention schedule, no legal hold, and no deletion policy.

**Open:** per-market e-invoicing and archival requirements.

### 7. Immutability is enforced, but there is no signature

An issued document is immutable: the model is frozen, the only persistence
write path is `issue_document`, and "superseded" is derived from a later
document's `supersedes_document_id` rather than stored (so no status column
has to be mutated on an issued row).

What is **absent** is cryptographic integrity: no digital signature, no hash
chain across the document sequence, no qualified electronic seal. Several
jurisdictions require one of these to establish authenticity and integrity.
Immutability against Detoura's own application code is not the same as
tamper-evidence against someone with database access.

**Open:** hash-chain the document sequence at minimum; qualified e-signature
where a market requires it.

### 8. Currency, rounding and conversion

Documents are issued in the economics ledger's currency, and mixing
currencies on one document is refused (`CurrencyMismatch`). Money is stored
as integer minor units and figures are asserted to reconcile within half a
cent.

Not addressed: jurisdictions that require the tax amount to also be stated in
the local currency with the exchange rate and its source and date. Detoura's
`FixedExchangeRates` is explicitly illustrative and synthetic
(`models/money.py`) and must not back a real conversion on a tax document.

### 9. Consumer-law disclosures

Not addressed: mandatory consumer disclosures that may need to appear on or
alongside a travel document — package-travel protections and insolvency
protection statements, the operating carrier disclosure, cancellation and
withdrawal rights, and ADR/ODR contact points.

### 10. Which document a customer is entitled to, and when

Phase 5 provides the capability to issue a `RECEIPT`, an `INVOICE` and a
`CREDIT_NOTE`. It does **not** encode policy: who may request which, whether
an invoice is issued automatically or on request, the deadline for issuing
one after the taxable event, or who is authorised to trigger a credit note.
Wiring and authorisation are Agent 6's integration surface; the *policy* is
an open business decision.

---

## Guard rails currently in force

These are implemented and tested, and should not be relaxed without
revisiting this document:

| Guard | Where |
|---|---|
| Production issuance fails closed on missing company/legal metadata | `financial_document_pdf.resolve_company_metadata` |
| Sandbox documents are visibly marked `TEST DOCUMENT - NOT FOR TAX PURPOSES` and `is_production=False` | `financial_document_pdf.render_document_pdf` |
| No document is issued without an `EconomicsRow` | `financial_document_service.issue_receipt_or_invoice` |
| Line items must reconcile to `customer_total` within half a cent, or construction fails | `FinancialDocument._check_components` |
| A partial breakdown is rejected — all components known, or (credit notes only) none | `FinancialDocument._check_components` |
| Tax is never synthesised; `None` means UNKNOWN, not zero | model + service |
| Full vs partial refund is derived arithmetically, never asserted by a caller | `FinancialDocument.credit_scope` |
| A credit note against a zero/unknown capture is refused rather than defaulted | model validator + service |
| Issued documents have no update path | `persistence/financial_documents.py` |
| A credit note `adjusts` (does not invalidate) the original | model relationship split |
| No provider order ids, offer ids or raw payloads reach the PDF | `ItineraryLeg` conversion boundary |
| Ownership-checked reads return `None` for both "missing" and "not yours" | `get_document_for_user` |
