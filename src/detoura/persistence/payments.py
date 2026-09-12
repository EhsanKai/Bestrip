"""Persistence for the payment domain (V9 Phase 4).

Every write that changes a payment/refund's state goes through a
compare-and-swap on ``version`` (never a read-then-write race, §J) and, for
irreversible steps, an accompanying insert into the append-only
``payment_events``/``payment_provider_events`` tables in the SAME
transaction - so the ledger and the current-state row can never disagree
about whether a step happened.

Idempotent creation (payments, refunds) relies on the UNIQUE constraint on
``idempotency_key``: a retried create is a single INSERT attempt that either
succeeds once or fails with ``IntegrityError``, at which point the existing
row - not a fresh one - is returned. This is the same pattern
``persistence/accounts.py`` uses for duplicate-email registration.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone

from ..models.commercial import CommercialQuote
from ..models.money import from_minor_units, to_minor_units
from ..models.payment import (
    AllocationComponent,
    CheckoutSnapshot,
    PaymentAllocation,
    PaymentEvent,
    PaymentStatus,
    PaymentTransaction,
    ReconciliationClassification,
    ReconciliationFinding,
    Refund,
    RefundStatus,
)
from .db import Database


class DuplicateIdempotencyKey(Exception):
    """Raised internally, caught by the creator, never propagated - a
    duplicate create is not an error to the caller, it is the same row."""


class StaleVersion(Exception):
    """A compare-and-swap write lost the race: the row has moved since the
    caller last read it. The caller must re-read, never blindly retry the
    same write."""


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(16)}"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


# ======================================================================
# Checkout snapshots
# ======================================================================
def create_snapshot(db: Database, snapshot: CheckoutSnapshot) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO checkout_snapshots ("
            " snapshot_id, booking_id, journey_reference, user_id, service_tier,"
            " currency, customer_total_minor, markup_policy_id,"
            " markup_policy_version, price_provenance, quote_json,"
            " revalidation_json, created_at, expires_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot.snapshot_id, snapshot.booking_id, snapshot.journey_reference,
                snapshot.user_id, snapshot.service_tier, snapshot.currency,
                to_minor_units(snapshot.customer_total),
                snapshot.quote.markup_policy.policy_id,
                snapshot.quote.markup_policy.version,
                snapshot.revalidation_state.get("provenance", ""),
                snapshot.quote.model_dump_json(),
                json.dumps(snapshot.revalidation_state),
                snapshot.created_at.isoformat(), snapshot.expires_at.isoformat(),
            ),
        )


def get_snapshot(db: Database, snapshot_id: str) -> CheckoutSnapshot | None:
    row = db.query_one("SELECT * FROM checkout_snapshots WHERE snapshot_id=?", (snapshot_id,))
    if row is None:
        return None
    return CheckoutSnapshot(
        snapshot_id=row["snapshot_id"], booking_id=row["booking_id"],
        journey_reference=row["journey_reference"], user_id=row["user_id"],
        service_tier=row["service_tier"],
        quote=CommercialQuote.model_validate_json(row["quote_json"]),
        revalidation_state=json.loads(row["revalidation_json"] or "{}"),
        created_at=_dt(row["created_at"]), expires_at=_dt(row["expires_at"]),
    )


# ======================================================================
# Payment transactions
# ======================================================================
def _row_to_payment(row: sqlite3.Row) -> PaymentTransaction:
    return PaymentTransaction(
        payment_id=row["payment_id"], journey_reference=row["journey_reference"],
        booking_id=row["booking_id"], user_id=row["user_id"],
        checkout_snapshot_id=row["checkout_snapshot_id"], currency=row["currency"],
        customer_total=from_minor_units(row["customer_total_minor"]),
        status=PaymentStatus(row["status"]), provider=row["provider"],
        provider_payment_reference=row["provider_payment_reference"],
        idempotency_key=row["idempotency_key"],
        authorized_amount=from_minor_units(row["authorized_amount_minor"]),
        captured_amount=from_minor_units(row["captured_amount_minor"]),
        refunded_amount=from_minor_units(row["refunded_amount_minor"]),
        created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        version=row["version"],
    )


def create_payment(
    db: Database, *, payment: PaymentTransaction,
) -> tuple[PaymentTransaction, bool]:
    """Insert a new payment, or return the existing row for the same
    ``idempotency_key``. Returns ``(payment, created)`` - ``created`` is
    ``False`` when an earlier attempt already made this row (a safe retry,
    never a duplicate charge, §J)."""
    with db.write() as conn:
        try:
            conn.execute(
                "INSERT INTO payment_transactions ("
                " payment_id, journey_reference, booking_id, user_id,"
                " checkout_snapshot_id, currency, customer_total_minor, status,"
                " provider, provider_payment_reference, idempotency_key,"
                " authorized_amount_minor, captured_amount_minor,"
                " refunded_amount_minor, created_at, updated_at, version"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payment.payment_id, payment.journey_reference, payment.booking_id,
                    payment.user_id, payment.checkout_snapshot_id, payment.currency,
                    to_minor_units(payment.customer_total), payment.status.value,
                    payment.provider, payment.provider_payment_reference,
                    payment.idempotency_key,
                    to_minor_units(payment.authorized_amount),
                    to_minor_units(payment.captured_amount),
                    to_minor_units(payment.refunded_amount),
                    payment.created_at.isoformat(), payment.updated_at.isoformat(),
                    payment.version,
                ),
            )
        except sqlite3.IntegrityError:
            existing = conn.execute(
                "SELECT * FROM payment_transactions WHERE idempotency_key=?",
                (payment.idempotency_key,),
            ).fetchone()
            if existing is None:  # pragma: no cover - defensive
                raise
            return _row_to_payment(existing), False
    return payment, True


def get_payment(db: Database, payment_id: str) -> PaymentTransaction | None:
    row = db.query_one("SELECT * FROM payment_transactions WHERE payment_id=?", (payment_id,))
    return _row_to_payment(row) if row else None


def get_payment_by_idempotency_key(db: Database, idempotency_key: str) -> PaymentTransaction | None:
    row = db.query_one(
        "SELECT * FROM payment_transactions WHERE idempotency_key=?", (idempotency_key,),
    )
    return _row_to_payment(row) if row else None


def list_payments_for_booking(db: Database, booking_id: str) -> list[PaymentTransaction]:
    rows = db.query(
        "SELECT * FROM payment_transactions WHERE booking_id=? ORDER BY created_at",
        (booking_id,),
    )
    return [_row_to_payment(r) for r in rows]


def get_payment_for_user(db: Database, payment_id: str, *, user_id: str) -> PaymentTransaction | None:
    """Ownership-checked read (§S) - returns ``None`` for both "does not
    exist" and "belongs to someone else", the same anti-enumeration shape
    Phase 2.6's My Trips API uses."""
    row = db.query_one(
        "SELECT * FROM payment_transactions WHERE payment_id=? AND user_id=?",
        (payment_id, user_id),
    )
    return _row_to_payment(row) if row else None


def compare_and_swap_payment(
    db: Database, *, payment: PaymentTransaction, expected_version: int,
) -> PaymentTransaction:
    """Write ``payment`` (already advanced via ``.with_status``/field update)
    only if the stored row is still at ``expected_version``. Raises
    :class:`StaleVersion` otherwise - the caller must re-read and decide
    fresh, never blindly overwrite (§J, §K)."""
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE payment_transactions SET"
            " status=?, provider_payment_reference=?, authorized_amount_minor=?,"
            " captured_amount_minor=?, refunded_amount_minor=?, updated_at=?, version=?"
            " WHERE payment_id=? AND version=?",
            (
                payment.status.value, payment.provider_payment_reference,
                to_minor_units(payment.authorized_amount),
                to_minor_units(payment.captured_amount),
                to_minor_units(payment.refunded_amount),
                payment.updated_at.isoformat(), payment.version,
                payment.payment_id, expected_version,
            ),
        )
        if cur.rowcount == 0:
            raise StaleVersion(
                f"{payment.payment_id}: expected version {expected_version}, row has moved"
            )
    return payment


# ======================================================================
# Payment events (append-only ledger)
# ======================================================================
def record_event(db: Database, event: PaymentEvent, *, conn: sqlite3.Connection | None = None) -> None:
    """Append one ledger row. Pass ``conn`` to fold this into an
    already-open ``db.write()`` transaction (the common case: a state
    change and its ledger row must commit together or not at all)."""
    sql = (
        "INSERT INTO payment_events ("
        " event_id, payment_id, event_type, occurred_at, amount_minor, detail, data_json"
        ") VALUES (?,?,?,?,?,?,?)"
    )
    params = (
        event.event_id, event.payment_id, event.event_type,
        event.occurred_at.isoformat(),
        to_minor_units(event.amount) if event.amount is not None else None,
        event.detail, json.dumps(event.data),
    )
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.write() as c:
            c.execute(sql, params)


def list_events(db: Database, payment_id: str) -> list[PaymentEvent]:
    rows = db.query(
        # `rowid` (SQLite's implicit, monotonically-increasing insert order),
        # not `event_id` (a random token), as the tiebreak: two events
        # written microseconds apart can share an identical `occurred_at`
        # timestamp, and sorting the tie by a random id makes the ledger's
        # displayed order not actually chronological - found via a direct
        # smoke test of the happy-path flow, where AUTHORIZATION_REQUESTED
        # and AUTHORIZED (written back-to-back with the same `now`) came out
        # in the wrong order. `rowid` is exact insertion order, always.
        "SELECT *, rowid FROM payment_events WHERE payment_id=? ORDER BY occurred_at, rowid",
        (payment_id,),
    )
    return [
        PaymentEvent(
            event_id=r["event_id"], payment_id=r["payment_id"], event_type=r["event_type"],
            occurred_at=_dt(r["occurred_at"]),
            amount=from_minor_units(r["amount_minor"]) if r["amount_minor"] is not None else None,
            detail=r["detail"], data=json.loads(r["data_json"] or "{}"),
        )
        for r in rows
    ]


# ======================================================================
# Provider (webhook) event dedup
# ======================================================================
def claim_provider_event(
    db: Database, *, provider: str, provider_event_id: str, payment_id: str | None,
    event_type: str, payload: dict, now: datetime | None = None,
) -> bool:
    """Atomically claim one inbound provider event. Returns ``True`` the
    first time this (provider, provider_event_id) is seen, ``False`` for a
    duplicate/replay - the caller must skip processing on ``False``. The
    primary key IS the idempotency mechanism (§J, §O): no read happens
    before the insert."""
    now = now or datetime.now(timezone.utc)
    with db.write() as conn:
        try:
            conn.execute(
                "INSERT INTO payment_provider_events ("
                " provider, provider_event_id, payment_id, event_type,"
                " received_at, payload_json, processed"
                ") VALUES (?,?,?,?,?,?,0)",
                (provider, provider_event_id, payment_id, event_type,
                 now.isoformat(), json.dumps(payload)),
            )
        except sqlite3.IntegrityError:
            return False
    return True


def mark_provider_event_processed(db: Database, *, provider: str, provider_event_id: str) -> None:
    with db.write() as conn:
        conn.execute(
            "UPDATE payment_provider_events SET processed=1"
            " WHERE provider=? AND provider_event_id=?",
            (provider, provider_event_id),
        )


# ======================================================================
# Refunds
# ======================================================================
def _row_to_refund(row: sqlite3.Row) -> Refund:
    return Refund(
        refund_id=row["refund_id"], payment_id=row["payment_id"],
        amount=from_minor_units(row["amount_minor"]), currency=row["currency"],
        status=RefundStatus(row["status"]), reason=row["reason"],
        idempotency_key=row["idempotency_key"],
        provider_refund_reference=row["provider_refund_reference"],
        created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        version=row["version"],
    )


def create_refund(db: Database, *, refund: Refund) -> tuple[Refund, bool]:
    with db.write() as conn:
        try:
            conn.execute(
                "INSERT INTO refunds ("
                " refund_id, payment_id, amount_minor, currency, status, reason,"
                " idempotency_key, provider_refund_reference, created_at, updated_at, version"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    refund.refund_id, refund.payment_id, to_minor_units(refund.amount),
                    refund.currency, refund.status.value, refund.reason,
                    refund.idempotency_key, refund.provider_refund_reference,
                    refund.created_at.isoformat(), refund.updated_at.isoformat(),
                    refund.version,
                ),
            )
        except sqlite3.IntegrityError:
            existing = conn.execute(
                "SELECT * FROM refunds WHERE idempotency_key=?", (refund.idempotency_key,),
            ).fetchone()
            if existing is None:  # pragma: no cover - defensive
                raise
            return _row_to_refund(existing), False
    return refund, True


def get_refund(db: Database, refund_id: str) -> Refund | None:
    row = db.query_one("SELECT * FROM refunds WHERE refund_id=?", (refund_id,))
    return _row_to_refund(row) if row else None


def list_refunds_for_payment(db: Database, payment_id: str) -> list[Refund]:
    rows = db.query("SELECT * FROM refunds WHERE payment_id=? ORDER BY created_at", (payment_id,))
    return [_row_to_refund(r) for r in rows]


def compare_and_swap_refund(db: Database, *, refund: Refund, expected_version: int) -> Refund:
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE refunds SET status=?, provider_refund_reference=?, updated_at=?, version=?"
            " WHERE refund_id=? AND version=?",
            (
                refund.status.value, refund.provider_refund_reference,
                refund.updated_at.isoformat(), refund.version,
                refund.refund_id, expected_version,
            ),
        )
        if cur.rowcount == 0:
            raise StaleVersion(f"{refund.refund_id}: expected version {expected_version}, row has moved")
    return refund


# ======================================================================
# Payment allocations
# ======================================================================
def write_allocations(db: Database, allocations: list[PaymentAllocation]) -> None:
    if not allocations:
        return
    with db.write() as conn:
        for a in allocations:
            conn.execute(
                "INSERT INTO payment_allocations ("
                " allocation_id, payment_id, component, label, amount_minor,"
                " currency, created_at"
                ") VALUES (?,?,?,?,?,?,?)",
                (
                    a.allocation_id, a.payment_id, a.component.value, a.label,
                    to_minor_units(a.amount), a.currency, a.created_at.isoformat(),
                ),
            )


def list_allocations(db: Database, payment_id: str) -> list[PaymentAllocation]:
    rows = db.query(
        "SELECT * FROM payment_allocations WHERE payment_id=? ORDER BY created_at",
        (payment_id,),
    )
    return [
        PaymentAllocation(
            allocation_id=r["allocation_id"], payment_id=r["payment_id"],
            component=AllocationComponent(r["component"]), label=r["label"],
            amount=from_minor_units(r["amount_minor"]), currency=r["currency"],
            created_at=_dt(r["created_at"]),
        )
        for r in rows
    ]


# ======================================================================
# Reconciliation findings
# ======================================================================
def create_finding(db: Database, finding: ReconciliationFinding) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO reconciliation_findings ("
            " finding_id, payment_id, local_status, provider_status, classification,"
            " detail, created_at, resolved, resolved_at"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            (
                finding.finding_id, finding.payment_id, finding.local_status,
                finding.provider_status, finding.classification.value, finding.detail,
                finding.created_at.isoformat(), int(finding.resolved),
                _iso(finding.resolved_at),
            ),
        )


def _row_to_finding(row: sqlite3.Row) -> ReconciliationFinding:
    return ReconciliationFinding(
        finding_id=row["finding_id"], payment_id=row["payment_id"],
        local_status=row["local_status"], provider_status=row["provider_status"],
        classification=ReconciliationClassification(row["classification"]),
        detail=row["detail"], created_at=_dt(row["created_at"]),
        resolved=bool(row["resolved"]), resolved_at=_dt(row["resolved_at"]),
    )


def list_findings(db: Database, *, resolved: bool | None = None, limit: int = 200) -> list[ReconciliationFinding]:
    if resolved is None:
        rows = db.query(
            "SELECT * FROM reconciliation_findings ORDER BY created_at DESC LIMIT ?", (limit,),
        )
    else:
        rows = db.query(
            "SELECT * FROM reconciliation_findings WHERE resolved=? ORDER BY created_at DESC LIMIT ?",
            (int(resolved), limit),
        )
    return [_row_to_finding(r) for r in rows]


def resolve_finding(db: Database, finding_id: str, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE reconciliation_findings SET resolved=1, resolved_at=?"
            " WHERE finding_id=? AND resolved=0",
            (now.isoformat(), finding_id),
        )
        return cur.rowcount > 0
