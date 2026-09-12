"""Deterministic PDF rendering for financial documents (V9 Phase 5).

Why a hand-rolled writer
------------------------
This project has no PDF dependency: ``pyproject.toml`` lists pydantic,
certifi and argon2-cffi at runtime, and pytest/httpx/fastapi/uvicorn/Pillow
for dev. ``reportlab``, ``fpdf2`` and ``weasyprint`` are all absent, so
"use whichever is already a dependency" resolves to none of them, and
weasyprint in particular would drag in a Cairo/Pango stack for what is a
one-page text document.

A single-page, text-only PDF using the base-14 fonts (which need no
embedding) is a small, bounded, well-specified problem: a handful of
objects, a byte-offset xref table, and a content stream of positioned text.
That is what :func:`_build_pdf` does, correctly, in ~120 lines and with no
new dependency. If Detoura later needs logos, tables or non-Latin scripts,
the right move is a real library behind :func:`render_document_pdf` - not
to grow this writer.

Determinism
-----------
The same :class:`FinancialDocument` and the same company metadata always
produce byte-identical output. Nothing observes the clock (``issued_at``
comes off the document, never ``now()``), nothing generates a random id,
there is no ``/Info`` dictionary with a ``CreationDate``, and no trailer
``/ID``. Amounts are formatted with a fixed 2-decimal format that does not
consult locale. Byte-identity is asserted in the tests, not assumed.

What never goes in
------------------
Raw provider payloads, secrets, internal Ops notes, and provider order/offer
ids. The renderer cannot leak them because it is never given them: it takes
:class:`ItineraryLeg`, a deliberately narrow value type carrying only what
a customer already knows about their own flights. Dropping the provider
fields happens at that conversion boundary, in
``financial_document_service``, not by remembering to omit them here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from ..models.financial_document import (
    CreditScope,
    FinancialDocument,
    FinancialDocumentType,
)

# ======================================================================
# Company metadata + the production fail-closed guard
# ======================================================================
SANDBOX_WATERMARK = "TEST DOCUMENT - NOT FOR TAX PURPOSES"


class IncompleteCompanyMetadata(Exception):
    """Production issuance was requested but required legal metadata is
    missing. Raised, never worked around: a document that looks official
    and omits the issuing entity's legal identity is worse than no document
    - a customer may file it, and it will not stand up."""


@dataclass(frozen=True, slots=True)
class CompanyMetadata:
    """Who is issuing the document. Read from the environment.

    Deliberately not cached in a module global: these are six ``os.getenv``
    calls, and a cached config object is exactly the thing that makes one
    test's ``monkeypatch.setenv`` leak into the next.
    """

    name: str = ""
    legal_entity: str = ""
    """The registered legal name and company/registration number, as a
    single configured string - Detoura does not model company registration
    structure, and must not invent one."""
    address: str = ""
    tax_id: str = ""
    """VAT/tax identifier, if the entity has one configured."""
    support_contact: str = ""

    #: What a production document may not go out without.
    REQUIRED_FOR_PRODUCTION = (
        "name",
        "legal_entity",
        "address",
        "tax_id",
        "support_contact",
    )

    @classmethod
    def from_env(cls) -> "CompanyMetadata":
        def _s(key: str) -> str:
            return os.getenv(key, "").strip()

        return cls(
            name=_s("FINANCIAL_DOCUMENT_COMPANY_NAME"),
            legal_entity=_s("FINANCIAL_DOCUMENT_LEGAL_ENTITY"),
            address=_s("FINANCIAL_DOCUMENT_ADDRESS"),
            tax_id=_s("FINANCIAL_DOCUMENT_TAX_ID"),
            support_contact=_s("FINANCIAL_DOCUMENT_SUPPORT_CONTACT"),
        )

    def missing_for_production(self) -> list[str]:
        return [f for f in self.REQUIRED_FOR_PRODUCTION if not getattr(self, f)]

    def validate_for_production(self) -> None:
        missing = self.missing_for_production()
        if missing:
            raise IncompleteCompanyMetadata(
                "refusing to issue a production financial document without "
                f"required company/legal metadata: {', '.join(missing)}. Set the "
                "corresponding FINANCIAL_DOCUMENT_* environment variables, or "
                "unset FINANCIAL_DOCUMENTS_PRODUCTION_MODE to issue clearly "
                "marked sandbox documents instead."
            )


def production_mode_enabled() -> bool:
    raw = os.getenv("FINANCIAL_DOCUMENTS_PRODUCTION_MODE", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def resolve_company_metadata() -> tuple[CompanyMetadata, bool]:
    """``(metadata, is_production)``, failing closed.

    Sandbox (the default) tolerates missing metadata - but the document that
    results is marked ``is_production=False`` and its PDF carries
    :data:`SANDBOX_WATERMARK`, so it can never be mistaken for the real
    thing. Production tolerates nothing missing.
    """
    metadata = CompanyMetadata.from_env()
    is_production = production_mode_enabled()
    if is_production:
        metadata.validate_for_production()
    return metadata, is_production


# ======================================================================
# The narrow itinerary view the renderer is allowed to see
# ======================================================================
@dataclass(frozen=True, slots=True)
class ItineraryLeg:
    """One leg, as the customer already knows it.

    There is no field here for a provider order id, an offer id, or a raw
    payload, so no amount of forgetfulness in the renderer can put one on a
    customer's receipt.
    """

    sequence: int
    origin: str = ""
    destination: str = ""
    departure: datetime | None = None
    arrival: datetime | None = None
    carrier: str = ""
    flight_number: str = ""


# ======================================================================
# Document layout
# ======================================================================
_PAGE_W = 595
_PAGE_H = 842
_MARGIN_L = 56
_MARGIN_R = 56
_MARGIN_T = 56
_MARGIN_B = 56
_USABLE_W = _PAGE_W - _MARGIN_L - _MARGIN_R

_TYPE_TITLES = {
    FinancialDocumentType.RECEIPT: "RECEIPT",
    FinancialDocumentType.INVOICE: "INVOICE",
    FinancialDocumentType.CREDIT_NOTE: "CREDIT NOTE",
}


def _money(currency: str, amount: float) -> str:
    return f"{currency} {amount:.2f}"


def _date(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def render_document_pdf(
    document: FinancialDocument,
    *,
    company: CompanyMetadata,
    itinerary: list[ItineraryLeg] | None = None,
    adjusts_document_number: str | None = None,
) -> bytes:
    """Render ``document`` to PDF bytes. Pure and deterministic.

    ``adjusts_document_number`` is the human-facing number of the document a
    credit note is against - passed in rather than looked up, so this stays
    a pure function of its arguments.
    """
    lines: list[tuple] = []

    def text(s: str, *, bold: bool = False, size: float = 9.5, gap: float = 0.0) -> None:
        lines.append(("text", s, bold, size, gap))

    def rule(gap: float = 4.0) -> None:
        lines.append(("rule", "", False, 0.0, gap))

    def row(label: str, amount: str, *, bold: bool = False, gap: float = 0.0) -> None:
        lines.append(("row", (label, amount), bold, 9.5, gap))

    # -- issuer -------------------------------------------------------
    if not document.is_production:
        text(SANDBOX_WATERMARK, bold=True, size=11)
        rule()
    text(company.name or "Detoura (sandbox - no company configured)",
         bold=True, size=15, gap=2)
    for detail in (company.legal_entity, company.address, company.tax_id,
                   company.support_contact):
        if detail:
            text(detail, size=8)
    rule(gap=8)

    # -- document identity --------------------------------------------
    text(_TYPE_TITLES[document.document_type], bold=True, size=13, gap=2)
    row("Document number", document.document_number)
    row("Issue date", document.issued_at.strftime("%Y-%m-%d"))
    row("Journey reference", document.journey_reference)
    if document.document_type is FinancialDocumentType.CREDIT_NOTE:
        row("Credit against", adjusts_document_number or "(see original document)")
        scope = document.credit_scope
        row("Refund scope",
            "Full refund" if scope is CreditScope.FULL else "Partial refund")
    rule(gap=8)

    # -- itinerary -----------------------------------------------------
    if itinerary:
        text("Itinerary", bold=True, size=10.5, gap=2)
        for leg in itinerary:
            route = f"{leg.origin or '-'} to {leg.destination or '-'}"
            text(f"{leg.sequence}.  {route}", size=9.5)
            flight = " ".join(p for p in (leg.carrier, leg.flight_number) if p)
            detail = f"     Departs {_date(leg.departure)}   Arrives {_date(leg.arrival)}"
            if flight:
                detail += f"   {flight}"
            text(detail, size=8)
        rule(gap=8)

    # -- financial breakdown -------------------------------------------
    text("Breakdown", bold=True, size=10.5, gap=2)
    ccy = document.currency
    if document.has_line_item_breakdown:
        row("Transport", _money(ccy, document.supplier_transport))
        row("Baggage", _money(ccy, document.supplier_baggage))
        row("Supplier fees", _money(ccy, document.supplier_fees))
        row("Detoura service fee", _money(ccy, document.detoura_service_fee))
        row("Detoura markup", _money(ccy, document.detoura_markup))
        if document.discount:
            row("Discount", f"-{_money(ccy, document.discount)}")
        row("Tax", _money(ccy, document.tax))
    else:
        # A partial credit note. Stating a fabricated apportionment here
        # would be the single most damaging thing this renderer could do.
        text("The apportionment of this partial refund across the original "
             "line items is not determined.", size=8)
    rule(gap=2)
    total_label = (
        "Total credited"
        if document.document_type is FinancialDocumentType.CREDIT_NOTE
        else "Total"
    )
    row(total_label, _money(ccy, document.customer_total), bold=True)
    rule(gap=8)

    # -- money actually moved -------------------------------------------
    text("Payment", bold=True, size=10.5, gap=2)
    row("Captured", _money(ccy, document.captured_amount))
    row("Refunded", _money(ccy, document.refunded_amount))
    row("Net retained", _money(ccy, document.net_amount_retained))

    if not document.is_production:
        rule(gap=10)
        text(SANDBOX_WATERMARK, bold=True, size=9)
        text("Issued by a Detoura sandbox/development environment. This is not "
             "a valid tax document and must not be used for accounting or tax "
             "purposes.", size=8)

    return _build_pdf(lines)


# ======================================================================
# Minimal PDF writer
# ======================================================================
#: Helvetica advance widths per 1000 units, for the characters that appear
#: in these documents. Used only to right-align amounts; anything not listed
#: falls back to 556, which is the digit width and a fair average.
_WIDTHS = {
    " ": 278, ".": 278, ",": 278, "-": 333, "(": 333, ")": 333, "/": 278,
    ":": 278, "'": 191, "%": 889,
    "i": 222, "j": 222, "l": 222, "t": 278, "f": 278, "r": 333,
    "I": 278, "J": 500,
    "m": 833, "w": 722, "M": 833, "W": 944,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "K": 667, "L": 556, "N": 722, "O": 778, "P": 667, "Q": 778,
    "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "X": 667, "Y": 667,
    "Z": 611,
}


def _text_width(s: str, size: float, bold: bool) -> float:
    total = sum(_WIDTHS.get(ch, 556) for ch in s)
    # Helvetica-Bold runs a little wider than Helvetica at the same size.
    return total / 1000.0 * size * (1.06 if bold else 1.0)


def _wrap(s: str, size: float, bold: bool, width: float) -> list[str]:
    """Greedy word wrap. Deterministic: no locale, no hyphenation dictionary."""
    if _text_width(s, size, bold) <= width:
        return [s]
    out: list[str] = []
    current = ""
    for word in s.split(" "):
        candidate = f"{current} {word}" if current else word
        if current and _text_width(candidate, size, bold) > width:
            out.append(current)
            current = word
        else:
            current = candidate
    if current:
        out.append(current)
    return out or [""]


def _escape(s: str) -> bytes:
    """PDF literal-string escaping, then Latin-1 (the encoding the base-14
    fonts' default WinAnsi behaviour matches). Characters outside it become
    '?' rather than raising - a receipt must still render."""
    escaped = s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return escaped.encode("latin-1", errors="replace")


def _f(value: float) -> str:
    """Fixed-format number for the content stream. Never scientific
    notation, never locale-dependent."""
    return f"{value:.2f}"


def _build_pdf(lines: list[tuple]) -> bytes:
    """Lay the line list out into pages and emit a valid PDF."""
    top_y = _PAGE_H - _MARGIN_T
    pages: list[list[tuple]] = [[]]
    y = top_y

    def ensure_room(needed: float) -> None:
        nonlocal y
        if y - needed < _MARGIN_B:
            pages.append([])
            y = top_y

    for kind, payload, bold, size, gap in lines:
        y -= gap
        if kind == "rule":
            ensure_room(6)
            y -= 4
            pages[-1].append(("rect", _MARGIN_L, y, _USABLE_W, 0.5))
            y -= 4
            continue

        if kind == "row":
            label, amount = payload
            leading = size * 1.45
            ensure_room(leading)
            y -= leading
            pages[-1].append(("text", _MARGIN_L, y, label, bold, size))
            x = _PAGE_W - _MARGIN_R - _text_width(amount, size, bold)
            pages[-1].append(("text", x, y, amount, bold, size))
            continue

        leading = size * 1.45
        for chunk in _wrap(payload, size, bold, _USABLE_W):
            ensure_room(leading)
            y -= leading
            pages[-1].append(("text", _MARGIN_L, y, chunk, bold, size))

    total_pages = len(pages)
    for index, ops in enumerate(pages, start=1):
        label = f"Page {index} of {total_pages}"
        ops.append((
            "text",
            _PAGE_W - _MARGIN_R - _text_width(label, 7.5, False),
            _MARGIN_B - 18,
            label, False, 7.5,
        ))

    return _assemble(pages)


def _content_stream(ops: list[tuple]) -> bytes:
    out = bytearray()
    for op in ops:
        if op[0] == "rect":
            _, x, y_pos, w, h = op
            out += f"{_f(x)} {_f(y_pos)} {_f(w)} {_f(h)} re f\n".encode("ascii")
        else:
            _, x, y_pos, s, bold, size = op
            font = b"/F2" if bold else b"/F1"
            out += b"BT " + font + f" {_f(size)} Tf 1 0 0 1 {_f(x)} {_f(y_pos)} Tm (".encode("ascii")
            out += _escape(s)
            out += b") Tj ET\n"
    return bytes(out)


def _assemble(pages: list[list[tuple]]) -> bytes:
    """Emit the object graph, xref table and trailer.

    Object numbering: 1 catalog, 2 page tree, 3 Helvetica, 4 Helvetica-Bold,
    then a (page, content) pair per page. Nothing here varies with time or
    randomness, so equal input gives byte-equal output.
    """
    page_count = len(pages)
    first_page_obj = 5
    page_obj_numbers = [first_page_obj + 2 * i for i in range(page_count)]

    objects: list[bytes] = []
    kids = " ".join(f"{n} 0 R" for n in page_obj_numbers)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("ascii")
    )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding >>"
    )

    for index, ops in enumerate(pages):
        content_obj = page_obj_numbers[index] + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_W} {_PAGE_H}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {content_obj} 0 R >>".encode("ascii")
        )
        stream = _content_stream(ops)
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"endstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    # A binary comment marks the file as containing binary data, per the
    # spec's recommendation for anything a naive tool might treat as text.
    out += b"%\xe2\xe3\xcf\xd3\n"
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_offset = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    # No /ID and no /Info: both are the usual sources of non-determinism
    # (a random file id, a generation timestamp) and neither is required.
    out += f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    return bytes(out)
