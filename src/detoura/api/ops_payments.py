"""Ops-authenticated visibility + safe recovery actions for payments (V9
Phase 4 §U).

No generic "set payment status" endpoint exists, and none ever will (§U):
every action here goes through the same domain functions
(``services.payment_service``) that a normal request would, so Ops cannot
fabricate a CAPTURED or REFUNDED state - it can only trigger a REAL
provider-truth reconciliation, or a domain-validated capture/cancel/refund
against a payment already in a state that legitimately allows it (the same
compare-and-swap, the same state-machine validation, the same ledger write).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..models.payment import PaymentStatus
from ..payment_config import resolve_provider
from ..persistence import get_db
from ..persistence import payments as store
from ..services import payment_service as ps
from .ops_auth import require_ops

router = APIRouter(prefix="/api/v1/ops/payments", tags=["ops", "payments"])


def _payment_dto(payment) -> dict:
    return {
        "payment_id": payment.payment_id, "booking_id": payment.booking_id,
        "journey_reference": payment.journey_reference, "user_id": payment.user_id,
        "currency": payment.currency, "customer_total": payment.customer_total,
        "status": payment.status.value, "provider": payment.provider,
        "provider_payment_reference": payment.provider_payment_reference,
        "authorized_amount": payment.authorized_amount,
        "captured_amount": payment.captured_amount,
        "refunded_amount": payment.refunded_amount,
        "created_at": payment.created_at.isoformat(), "updated_at": payment.updated_at.isoformat(),
        "version": payment.version,
    }


def _event_dto(e) -> dict:
    return {
        "event_id": e.event_id, "event_type": e.event_type,
        "occurred_at": e.occurred_at.isoformat(), "amount": e.amount,
        "detail": e.detail, "data": e.data,
    }


@router.get("/{payment_id}")
def get_payment(payment_id: str, actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    payment = store.get_payment(db, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail={"message": "No such payment."})
    return {
        **_payment_dto(payment),
        "events": [_event_dto(e) for e in store.list_events(db, payment_id)],
        "refunds": [
            {"refund_id": r.refund_id, "amount": r.amount, "status": r.status.value,
             "reason": r.reason, "created_at": r.created_at.isoformat()}
            for r in store.list_refunds_for_payment(db, payment_id)
        ],
        "allocations": [
            {"component": a.component.value, "label": a.label, "amount": a.amount}
            for a in store.list_allocations(db, payment_id)
        ],
    }


@router.get("/by-booking/{booking_id}")
def list_payments_for_booking(booking_id: str, actor: str = Depends(require_ops)) -> dict:
    db = get_db()
    payments = store.list_payments_for_booking(db, booking_id)
    return {"booking_id": booking_id, "payments": [_payment_dto(p) for p in payments]}


@router.get("/reconciliation/findings")
def list_findings(
    resolved: bool | None = None, limit: int = 200, actor: str = Depends(require_ops),
) -> dict:
    db = get_db()
    findings = store.list_findings(db, resolved=resolved, limit=limit)
    return {
        "findings": [
            {
                "finding_id": f.finding_id, "payment_id": f.payment_id,
                "local_status": f.local_status, "provider_status": f.provider_status,
                "classification": f.classification.value, "detail": f.detail,
                "created_at": f.created_at.isoformat(), "resolved": f.resolved,
                "resolved_at": f.resolved_at.isoformat() if f.resolved_at else None,
            }
            for f in findings
        ],
    }


@router.post("/reconciliation/findings/{finding_id}/resolve")
def resolve_finding(finding_id: str, actor: str = Depends(require_ops)) -> dict:
    """Administrative closure of the FINDING record only - never changes a
    payment's status (§U). A finding is resolved once a human has confirmed
    (elsewhere, against real provider truth via :func:`reconcile_payment`,
    or via the provider's own dashboard) that the discrepancy is
    understood/handled."""
    ok = store.resolve_finding(get_db(), finding_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"message": "No such open finding."})
    return {"finding_id": finding_id, "resolved": True}


@router.post("/{payment_id}/reconcile")
def trigger_reconciliation(payment_id: str, actor: str = Depends(require_ops)) -> dict:
    """The only Ops action that can move a payment out of UNKNOWN/
    RECONCILIATION_REQUIRED - and it does so by asking the PROVIDER for
    real truth, never by accepting an Ops-typed status (§P, §U)."""
    db = get_db()
    payment = store.get_payment(db, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail={"message": "No such payment."})
    provider = resolve_provider()
    try:
        updated, finding = ps.reconcile_payment(db, payment=payment, provider=provider)
    except store.StaleVersion:
        raise HTTPException(status_code=409, detail={"message": "This payment changed concurrently; re-fetch and retry."})
    return {
        "payment": _payment_dto(updated),
        "finding": (
            {"finding_id": finding.finding_id, "classification": finding.classification.value,
             "detail": finding.detail}
            if finding else None
        ),
    }


@router.post("/{payment_id}/capture")
def ops_capture(payment_id: str, actor: str = Depends(require_ops)) -> dict:
    """Explicit Ops-triggered capture - for resolving a
    ``RECONCILIATION_REQUIRED`` partial-booking-failure case where a human
    has decided the authorized amount should, in fact, be captured (e.g.
    the failed leg was itself non-essential, or a manual supplier fix
    landed). Goes through the exact same ``payment_service.request_capture``
    a normal flow would, from an ``AUTHORIZED`` payment only - it cannot
    capture a payment sitting in ``RECONCILIATION_REQUIRED`` without first
    being moved back to ``AUTHORIZED`` via a real reconciliation
    (``/reconcile``), so this can never be used to fabricate a capture out
    of an unresolved state."""
    db = get_db()
    payment = store.get_payment(db, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail={"message": "No such payment."})
    if payment.status is not PaymentStatus.AUTHORIZED:
        raise HTTPException(
            status_code=409,
            detail={"message": f"cannot capture from status {payment.status.value}; reconcile first if needed"},
        )
    provider = resolve_provider()
    try:
        updated = ps.request_capture(db, payment=payment, provider=provider)
    except store.StaleVersion:
        raise HTTPException(status_code=409, detail={"message": "This payment changed concurrently; re-fetch and retry."})
    except ValueError as error:
        # V9 Phase 6 Payment Security: the status check above is a
        # convenience pre-check, not a guard against a genuine race -
        # between that read and this call, a concurrent capture attempt for
        # the SAME payment (e.g. an Ops user double-clicking, or a retried
        # request) can have already moved the payment to CAPTURE_PENDING (or
        # any other non-AUTHORIZED status), and `request_capture`'s own
        # leading guard then raises a plain `ValueError`, not
        # `StaleVersion`. Found via real multi-threaded adversarial testing
        # (tests/test_v9_phase6_payment_security.py) - the financial
        # invariant itself was never violated (capture is never duplicated
        # either way), but an unhandled `ValueError` here was an unhandled
        # 500 instead of the same clean, retryable 409 every other race in
        # this file already returns.
        raise HTTPException(status_code=409, detail={"message": str(error)})
    # V9 Phase 5: a payment can settle AFTER its booking already reached a
    # terminal phase (today's only live capture path is this Ops action -
    # see post_booking_finalizer.py's module docstring). Re-running the
    # finalizer here lets a confirmation held at PENDING_VERIFICATION for
    # want of a settled payment promote to CONFIRMED once this capture
    # succeeds. Never lets a finalizer failure affect the capture response.
    try:
        from ..services.post_booking_finalizer import try_finalize

        try_finalize(db, booking_id=updated.booking_id)
    except Exception:
        pass
    return _payment_dto(updated)


@router.post("/{payment_id}/cancel")
def ops_cancel(payment_id: str, actor: str = Depends(require_ops)) -> dict:
    """Explicit Ops-triggered release of an authorization - the recovery
    action for a partial-booking-failure case where a human decides the
    trip cannot proceed and the hold should be released instead."""
    db = get_db()
    payment = store.get_payment(db, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail={"message": "No such payment."})
    provider = resolve_provider()
    try:
        updated = ps.cancel_authorization(db, payment=payment, provider=provider)
    except store.StaleVersion:
        raise HTTPException(status_code=409, detail={"message": "This payment changed concurrently; re-fetch and retry."})
    return _payment_dto(updated)


@router.post("/{payment_id}/refund")
def ops_refund(payment_id: str, body: dict, actor: str = Depends(require_ops)) -> dict:
    """Ops-triggered refund - the only path that may specify a PARTIAL
    amount (cancellation/recovery workflows, §I). Still bounded by the same
    0 <= refunded <= captured invariant, still idempotent on
    ``idempotency_key``, still produces the same ledger events - Ops has no
    separate, weaker code path (§U)."""
    amount = body.get("amount")
    reason = str(body.get("reason", "ops recovery"))[:200]
    idempotency_key = str(body.get("idempotency_key", "")).strip()
    if len(idempotency_key) < 8:
        raise HTTPException(status_code=400, detail={"message": "idempotency_key (>=8 chars) is required"})
    db = get_db()
    payment = store.get_payment(db, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail={"message": "No such payment."})
    refund_amount = float(amount) if amount is not None else round(
        payment.captured_amount - payment.refunded_amount, 2
    )
    provider = resolve_provider()
    try:
        updated_payment, refund = ps.request_refund(
            db, payment=payment, provider=provider, amount=refund_amount, reason=reason,
            idempotency_key=idempotency_key,
        )
    except store.StaleVersion:
        raise HTTPException(status_code=409, detail={"message": "This payment changed concurrently; re-fetch and retry."})
    except (ValueError, ps.InvalidRefundAmount) as error:
        raise HTTPException(status_code=409, detail={"message": str(error)})
    return {
        **_payment_dto(updated_payment),
        "refund_id": refund.refund_id, "refund_status": refund.status.value,
        "refund_amount": refund.amount,
    }
