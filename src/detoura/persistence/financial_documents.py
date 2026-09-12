"""Persistence for financial documents (V9 Phase 5).

Two tables: the documents themselves, and a counter table backing document
numbering.

**There is deliberately no ``update_document``.** Immutability of an issued
document is enforced structurally - the only write path is
:func:`issue_document`, which inserts. A refund does not edit a receipt; it
issues a credit note that ``adjusts`` it. A mistake on an invoice is not
edited; a replacement invoice ``supersedes`` it. Neither ever rewrites a row
that has already been handed to a customer.

Whether a document has been superseded is therefore *derived* from the
relationship graph (:func:`document_status`) rather than stored - a status
column would have to be mutated on an issued document to stay truthful,
which is the exact thing this module refuses to allow.

Idempotency mirrors ``persistence/payments.py::create_payment``: a UNIQUE
constraint on ``idempotency_key``, an INSERT attempt, and on
``IntegrityError`` a re-SELECT returning the row that already exists. A
retried finalizer run therefore never issues two originals for one logical
event.

The rendered PDF is stored as a BLOB alongside the row rather than as a
path into a data directory. Reasons, in order: it commits in the same
transaction as the document (a row can never exist with its artifact
missing, and a rolled-back issue leaves no orphan file), it needs no
filesystem/permissions/backup story separate from the database that already
holds the economics ledger, and these are single-page text PDFs of a few
kilobytes. If document volume ever makes that untrue, moving to object
storage is a schema change behind :func:`get_document_pdf`, which is the
only reader.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..models.financial_document import (
    FinancialDocument,
    FinancialDocumentStatus,
    FinancialDocumentType,
)
from ..models.money import from_minor_units, to_minor_units
from .db import Database

# ======================================================================
# --- Phase 5 schema addition (for Agent 6 to add to persistence/db.py's
# --- central migration - do NOT edit db.py yourself) ---
#
# Paste SCHEMA_DDL verbatim into db.py's `_DDL` string and bump
# SCHEMA_VERSION. Both tables are `IF NOT EXISTS`, so applying it to an
# existing deployment is a no-op for anything already present. No column is
# added to an existing table, so no `_ADD_COLUMNS` entry is needed.
# ======================================================================
SCHEMA_DDL = """
-- ==================================================================
-- V9 Phase 5: Financial documents (receipt / invoice / credit note).
--
-- An issued row is NEVER updated - there is no UPDATE statement against
-- this table anywhere in the codebase. A refund adds a CREDIT_NOTE row
-- pointing at the original via adjusts_document_id (which does NOT
-- invalidate it). A correction adds a row pointing via
-- supersedes_document_id (which does). "Superseded" is derived from those
-- pointers, never stored, so no status column can drift.
--
-- Money is integer minor units, as everywhere else. A NULL component means
-- UNKNOWN, never zero: the only legitimate case is a partial credit note,
-- whose apportionment across supplier cost/markup/fee/tax is a commercial
-- decision Detoura has not made and must not invent.
--
-- pdf_blob holds the exact bytes handed to the customer, committed in the
-- same transaction as the row so an artifact can never go missing.
-- ==================================================================
CREATE TABLE IF NOT EXISTS financial_documents (
    document_id               TEXT PRIMARY KEY,
    document_type             TEXT NOT NULL,
    document_number           TEXT NOT NULL UNIQUE,
    booking_id                TEXT NOT NULL,
    journey_reference         TEXT NOT NULL,
    user_id                   TEXT,
    currency                  TEXT NOT NULL,
    supplier_transport_minor  INTEGER,
    supplier_baggage_minor    INTEGER,
    supplier_fees_minor       INTEGER,
    detoura_markup_minor      INTEGER,
    detoura_service_fee_minor INTEGER,
    discount_minor            INTEGER,
    tax_minor                 INTEGER,
    customer_total_minor      INTEGER NOT NULL,
    captured_amount_minor     INTEGER NOT NULL,
    refunded_amount_minor     INTEGER NOT NULL DEFAULT 0,
    issued_at                 TEXT NOT NULL,
    adjusts_document_id       TEXT,
    supersedes_document_id    TEXT,
    is_production             INTEGER NOT NULL DEFAULT 0,
    idempotency_key           TEXT NOT NULL UNIQUE,
    pdf_blob                  BLOB
);
CREATE INDEX IF NOT EXISTS ix_findoc_booking ON financial_documents (booking_id);
CREATE INDEX IF NOT EXISTS ix_findoc_user ON financial_documents (user_id);
CREATE INDEX IF NOT EXISTS ix_findoc_adjusts ON financial_documents (adjusts_document_id);
CREATE INDEX IF NOT EXISTS ix_findoc_supersedes ON financial_documents (supersedes_document_id);

-- One row per numbering series (e.g. RCPT-2026). next_value is the last
-- number handed out. A number is allocated by incrementing it inside a write
-- transaction, never by reading it and writing back. See
-- allocate_document_number for the concurrency argument.
CREATE TABLE IF NOT EXISTS document_numbering (
    series      TEXT PRIMARY KEY,
    next_value  INTEGER NOT NULL DEFAULT 0
);
"""
# ======================================================================
# --- end Phase 5 schema addition ---
# ======================================================================


def apply_schema(db: Database) -> None:
    """Create the Phase 5 tables on ``db``.

    For tests and for a deployment running ahead of the central migration.
    Once Agent 6 folds :data:`SCHEMA_DDL` into ``db.py``, this is a no-op on
    any database that has been through it.

    Statements are executed one at a time rather than via ``executescript``:
    ``executescript`` issues an implicit COMMIT before it runs, which would
    end the ``db.write()`` transaction underneath us and leave its own
    COMMIT with nothing to commit.

    Splitting uses :func:`sqlite3.complete_statement` rather than a naive
    ``split(";")``, because SQLite's own parser is the only thing that
    correctly ignores a semicolon inside a comment or a string literal.
    """
    with db.write() as conn:
        for statement in _split_statements(SCHEMA_DDL):
            conn.execute(statement)


def _split_statements(script: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    return statements


#: Prefix per document type. Short, uppercase, and legally neutral - these
#: are Detoura-internal references, not a tax authority's series codes.
NUMBER_PREFIXES: dict[FinancialDocumentType, str] = {
    FinancialDocumentType.RECEIPT: "RCPT",
    FinancialDocumentType.INVOICE: "INV",
    FinancialDocumentType.CREDIT_NOTE: "CN",
}


class DocumentNumberingError(RuntimeError):
    """The counter could not be advanced. Never swallowed: issuing a
    document with a guessed or reused number is worse than not issuing."""


def series_for(document_type: FinancialDocumentType, issued_at: datetime) -> str:
    return f"{NUMBER_PREFIXES[document_type]}-{issued_at.year:04d}"


def allocate_document_number(
    db: Database, *, document_type: FinancialDocumentType, issued_at: datetime
) -> str:
    """Hand out the next number in this type+year series.

    Returns e.g. ``RCPT-2026-000123``.

    **LEGAL NEUTRALITY - read this before relying on the format.** This is a
    Detoura-internal reference and nothing more. It does NOT claim to
    satisfy any jurisdiction's requirements for sequential invoice
    numbering, and it demonstrably does not meet the common ones: the
    sequence can contain gaps (a number is allocated before the insert, so a
    lost race or a crash between the two consumes one), it resets each
    calendar year without any registered series identifier, it is not
    registered with or reported to any tax authority, and it is assigned per
    document *type* rather than per legal entity or establishment. Making
    these documents legally compliant invoices is an open item tracked in
    ``docs/V9_PHASE5_FINANCIAL_DOCUMENT_LEGAL_NOTES.md``, not something this
    function quietly delivers.

    **Concurrency.** No read-then-write, and therefore no TOCTOU window.
    The allocation is a single ``UPDATE ... SET next_value = next_value + 1``
    - the increment happens inside SQLite, against whatever the row holds at
    the moment of the write, so two concurrent allocators cannot compute the
    same successor from the same stale read. Both the seeding INSERT and the
    incrementing UPDATE run inside one :meth:`Database.write` transaction,
    which is serialised twice over: a process-wide ``threading.RLock`` (so
    threads in this process queue), and ``BEGIN IMMEDIATE`` (so a second OS
    process contends for SQLite's write lock up front, where
    ``busy_timeout`` retries correctly - see ``db.py``). Reading the new
    value back is safe for the same reason: it happens inside that same
    exclusive transaction, before any other writer can enter.

    ``RETURNING`` (SQLite >= 3.35) is used where available purely to save a
    statement; the fallback ``SELECT`` inside the same transaction is
    equally race-free, because the transaction - not the statement - is what
    provides the isolation.
    """
    series = series_for(document_type, issued_at)
    with db.write() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO document_numbering (series, next_value)"
            " VALUES (?, 0)",
            (series,),
        )
        value: int | None = None
        if sqlite3.sqlite_version_info >= (3, 35, 0):
            row = conn.execute(
                "UPDATE document_numbering SET next_value = next_value + 1"
                " WHERE series = ? RETURNING next_value",
                (series,),
            ).fetchone()
            if row is not None:
                value = int(row[0])
        else:  # pragma: no cover - modern SQLite everywhere this runs
            conn.execute(
                "UPDATE document_numbering SET next_value = next_value + 1"
                " WHERE series = ?",
                (series,),
            )
            row = conn.execute(
                "SELECT next_value FROM document_numbering WHERE series = ?",
                (series,),
            ).fetchone()
            if row is not None:
                value = int(row[0])
        if value is None:  # pragma: no cover - defensive
            raise DocumentNumberingError(f"could not advance series {series!r}")
    return f"{series}-{value:06d}"


# ======================================================================
# Reads
# ======================================================================
def _minor(value: float | None) -> int | None:
    return None if value is None else to_minor_units(value)


def _major(value: int | None) -> float | None:
    return None if value is None else from_minor_units(value)


def _row_to_document(row: sqlite3.Row) -> FinancialDocument:
    return FinancialDocument(
        document_id=row["document_id"],
        document_type=FinancialDocumentType(row["document_type"]),
        document_number=row["document_number"],
        booking_id=row["booking_id"],
        journey_reference=row["journey_reference"],
        user_id=row["user_id"],
        currency=row["currency"],
        supplier_transport=_major(row["supplier_transport_minor"]),
        supplier_baggage=_major(row["supplier_baggage_minor"]),
        supplier_fees=_major(row["supplier_fees_minor"]),
        detoura_markup=_major(row["detoura_markup_minor"]),
        detoura_service_fee=_major(row["detoura_service_fee_minor"]),
        discount=_major(row["discount_minor"]),
        tax=_major(row["tax_minor"]),
        customer_total=from_minor_units(row["customer_total_minor"]),
        captured_amount=from_minor_units(row["captured_amount_minor"]),
        refunded_amount=from_minor_units(row["refunded_amount_minor"]),
        issued_at=datetime.fromisoformat(row["issued_at"]),
        adjusts_document_id=row["adjusts_document_id"],
        supersedes_document_id=row["supersedes_document_id"],
        is_production=bool(row["is_production"]),
    )


def issue_document(
    db: Database,
    *,
    document: FinancialDocument,
    idempotency_key: str,
    pdf_bytes: bytes | None = None,
) -> tuple[FinancialDocument, bool]:
    """Insert an issued document, or return the one this key already made.

    Returns ``(document, created)``. ``created`` is ``False`` when an
    earlier attempt with the same ``idempotency_key`` already issued it - a
    safe retry, never a second original for one logical event. The stored
    row is returned in that case, not the caller's freshly-built one, so a
    retry can never quietly hand back a different number or a different set
    of figures than the customer already has.

    This is the ONLY write path into ``financial_documents``. There is no
    update counterpart, by design.
    """
    if not idempotency_key or len(idempotency_key) < 8:
        raise ValueError("idempotency_key must be at least 8 characters")
    with db.write() as conn:
        try:
            conn.execute(
                "INSERT INTO financial_documents ("
                " document_id, document_type, document_number, booking_id,"
                " journey_reference, user_id, currency,"
                " supplier_transport_minor, supplier_baggage_minor,"
                " supplier_fees_minor, detoura_markup_minor,"
                " detoura_service_fee_minor, discount_minor, tax_minor,"
                " customer_total_minor, captured_amount_minor,"
                " refunded_amount_minor, issued_at, adjusts_document_id,"
                " supersedes_document_id, is_production, idempotency_key, pdf_blob"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    document.document_id,
                    document.document_type.value,
                    document.document_number,
                    document.booking_id,
                    document.journey_reference,
                    document.user_id,
                    document.currency,
                    _minor(document.supplier_transport),
                    _minor(document.supplier_baggage),
                    _minor(document.supplier_fees),
                    _minor(document.detoura_markup),
                    _minor(document.detoura_service_fee),
                    _minor(document.discount),
                    _minor(document.tax),
                    to_minor_units(document.customer_total),
                    to_minor_units(document.captured_amount),
                    to_minor_units(document.refunded_amount),
                    document.issued_at.isoformat(),
                    document.adjusts_document_id,
                    document.supersedes_document_id,
                    int(document.is_production),
                    idempotency_key,
                    pdf_bytes,
                ),
            )
        except sqlite3.IntegrityError:
            existing = conn.execute(
                "SELECT * FROM financial_documents WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is None:
                # Not the idempotency key - a genuinely conflicting
                # document_id or document_number. Never paper over that.
                raise
            return _row_to_document(existing), False
    return document, True


def get_document(db: Database, document_id: str) -> FinancialDocument | None:
    row = db.query_one(
        "SELECT * FROM financial_documents WHERE document_id=?", (document_id,)
    )
    return _row_to_document(row) if row else None


def get_document_by_idempotency_key(
    db: Database, idempotency_key: str
) -> FinancialDocument | None:
    row = db.query_one(
        "SELECT * FROM financial_documents WHERE idempotency_key=?",
        (idempotency_key,),
    )
    return _row_to_document(row) if row else None


def get_document_for_user(
    db: Database, document_id: str, *, user_id: str
) -> FinancialDocument | None:
    """Ownership-checked read. Returns ``None`` both for "no such document"
    and "not yours" - the same anti-enumeration shape as
    ``payments.get_payment_for_user``, so a caller cannot distinguish the
    two and probe for which document ids exist."""
    row = db.query_one(
        "SELECT * FROM financial_documents WHERE document_id=? AND user_id=?",
        (document_id, user_id),
    )
    return _row_to_document(row) if row else None


def list_documents_for_booking(
    db: Database, booking_id: str
) -> list[FinancialDocument]:
    rows = db.query(
        # `rowid` as the tiebreak, not `document_id` (a random token): two
        # documents issued in the same microsecond share an `issued_at`, and
        # ordering that tie randomly makes the list not actually
        # chronological. Same reasoning as `payments.list_events`.
        "SELECT *, rowid FROM financial_documents WHERE booking_id=?"
        " ORDER BY issued_at, rowid",
        (booking_id,),
    )
    return [_row_to_document(r) for r in rows]


def get_document_pdf(db: Database, document_id: str) -> bytes | None:
    """The stored PDF bytes, or ``None`` if the document has none.

    The only reader of the blob column - see the module docstring on why
    the bytes live in the row.
    """
    row = db.query_one(
        "SELECT pdf_blob FROM financial_documents WHERE document_id=?",
        (document_id,),
    )
    if row is None or row["pdf_blob"] is None:
        return None
    return bytes(row["pdf_blob"])


def get_document_pdf_for_user(
    db: Database, document_id: str, *, user_id: str
) -> bytes | None:
    """Ownership-checked PDF read. ``None`` for "no such document", "not
    yours", and "no PDF stored" alike."""
    row = db.query_one(
        "SELECT pdf_blob FROM financial_documents"
        " WHERE document_id=? AND user_id=?",
        (document_id, user_id),
    )
    if row is None or row["pdf_blob"] is None:
        return None
    return bytes(row["pdf_blob"])


def document_status(db: Database, document_id: str) -> FinancialDocumentStatus | None:
    """Derived status - see the module docstring on why it is not a column.

    ``SUPERSEDED`` iff some later document declares it replaces this one.
    A credit note adjusting a receipt leaves the receipt ``ISSUED``: the
    capture it records still happened.
    """
    if get_document(db, document_id) is None:
        return None
    row = db.query_one(
        "SELECT 1 FROM financial_documents WHERE supersedes_document_id=? LIMIT 1",
        (document_id,),
    )
    return (
        FinancialDocumentStatus.SUPERSEDED
        if row is not None
        else FinancialDocumentStatus.ISSUED
    )


def list_credit_notes_against(
    db: Database, document_id: str
) -> list[FinancialDocument]:
    """Every credit note issued against this document, oldest first."""
    rows = db.query(
        "SELECT *, rowid FROM financial_documents WHERE adjusts_document_id=?"
        " ORDER BY issued_at, rowid",
        (document_id,),
    )
    return [_row_to_document(r) for r in rows]
