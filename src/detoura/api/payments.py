"""Consumer-safe payment API (V9 Phase 4 §T).

Backend/API seam only - no checkout/payment-form visual design here
(that's ChatGPT-owned). Every amount, currency and quote is server-owned:
nothing in a request body may set a payable amount (§T, §Y). A payment is
always created from a booking's already-priced
:class:`~detoura.models.commercial.CommercialQuote` - never a client
number.

Anonymous requests remain supported (the legacy anonymous flow, §S) -
``user_id`` is simply ``None`` on the resulting payment. An authenticated
session ties ownership: a different signed-in user can never read or act on
someone else's payment, and the 404 shape for "does not exist" vs "belongs
to someone else" is identical (the same anti-enumeration pattern Phase 2.6's
My Trips API uses).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ..models.payment import PaymentStatus
from ..observability import log_event
from ..payment_config import payment_config, resolve_provider
from ..persistence import get_db
from ..persistence import payments as store
from ..services import payment_service as ps
from ..services.booking_flow import booking_store
from .auth import get_optional_session, require_csrf

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])

_logger = logging.getLogger(__name__)


class PaymentNotAllowed(HTTPException):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(status_code=status_code, detail={"message": message})


def _get_owned_payment(db, payment_id: str, *, user_id: str | None):
    """Ownership-checked lookup (§S). ``None`` (-> 404) for both "does not
    exist" and "belongs to a different signed-in user" - never distinguishable."""
    payment = store.get_payment(db, payment_id)
    if payment is None:
        return None
    if payment.user_id is not None and payment.user_id != user_id:
        return None
    return payment


def _payment_dto(payment) -> dict:
    return {
        "payment_id": payment.payment_id,
        "booking_id": payment.booking_id,
        "journey_reference": payment.journey_reference,
        "currency": payment.currency,
        "customer_total": payment.customer_total,
        "status": payment.status.value,
        "provider": payment.provider,
        "authorized_amount": payment.authorized_amount,
        "captured_amount": payment.captured_amount,
        "refunded_amount": payment.refunded_amount,
        "created_at": payment.created_at.isoformat(),
        "updated_at": payment.updated_at.isoformat(),
        "requires_customer_action": payment.status is PaymentStatus.REQUIRES_CUSTOMER_ACTION,
    }


@router.post("")
def create_payment(body: dict, request: Request) -> dict:
    """Body: ``{"booking_id": "...", "idempotency_key": "..."}`` only.
    ``idempotency_key`` is the CLIENT's retry-safety token (so a dropped
    HTTP response and a resubmit never create two payments, §J) - never an
    amount, never a currency.
    """
    booking_id = str(body.get("booking_id", "")).strip()
    idempotency_key = str(body.get("idempotency_key", "")).strip()
    if not booking_id:
        raise PaymentNotAllowed("booking_id is required.")
    if len(idempotency_key) < 8:
        raise PaymentNotAllowed("a client idempotency_key of at least 8 characters is required.")

    run = booking_store().get(booking_id)
    if run is None:
        raise PaymentNotAllowed("No such booking, or it has expired.", status_code=404)
    if run.quote is None:
        raise PaymentNotAllowed("This booking has not been priced yet.")

    session = get_optional_session(request)
    user_id = session.user_id if session else None
    # A booking already tied to a signed-in user cannot be paid for by a
    # different one (§S) - checked the same anti-enumeration way as reads.
    if run.user_key not in ("anonymous", "") and run.user_key != user_id:
        raise PaymentNotAllowed("No such booking, or it has expired.", status_code=404)

    cfg = payment_config()
    snapshot = ps.freeze_checkout_snapshot(
        get_db(), booking_id=run.booking_id, journey_reference=run.journey_reference,
        user_id=user_id, service_tier=run.service_tier.value, quote=run.quote, cfg=cfg,
    )
    provider = resolve_provider(cfg)
    payment, created = ps.create_payment(
        get_db(), snapshot=snapshot, provider_name=provider.name,
        idempotency_key=idempotency_key,
    )
    return {**_payment_dto(payment), "created": created, "checkout_snapshot_id": snapshot.snapshot_id}


@router.get("/{payment_id}")
def get_payment(payment_id: str, request: Request) -> dict:
    session = get_optional_session(request)
    user_id = session.user_id if session else None
    payment = _get_owned_payment(get_db(), payment_id, user_id=user_id)
    if payment is None:
        raise PaymentNotAllowed("No such payment.", status_code=404)
    return _payment_dto(payment)


@router.post("/{payment_id}/confirm")
def confirm_payment(payment_id: str, request: Request) -> dict:
    """Authorize the payment against its frozen checkout snapshot (§D/§N).
    Idempotent - a duplicate/concurrent confirm returns the same resulting
    transaction, never a second authorization (§J, §X.7)."""
    session = get_optional_session(request)
    user_id = session.user_id if session else None
    db = get_db()
    payment = _get_owned_payment(db, payment_id, user_id=user_id)
    if payment is None:
        raise PaymentNotAllowed("No such payment.", status_code=404)
    if session is not None:
        require_csrf(request, session)

    snapshot = store.get_snapshot(db, payment.checkout_snapshot_id)
    if snapshot is not None and snapshot.is_expired():
        raise PaymentNotAllowed(
            "The priced quote behind this payment has expired; start a new checkout to reconfirm.",
            status_code=409,
        )
    provider = resolve_provider()
    try:
        updated = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snapshot)
    except store.StaleVersion:
        # A genuinely concurrent write already moved this payment on -
        # never surface as an unhandled 500; the caller re-fetches and
        # retries against current state (never a double authorization,
        # §X.7 - the loser here simply lost the race, it never executed).
        raise PaymentNotAllowed(
            "This payment changed concurrently; refresh and try again.", status_code=409,
        )
    return _payment_dto(updated)


@router.post("/{payment_id}/refund")
def refund_payment(payment_id: str, request: Request, body: dict | None = None) -> dict:
    """Consumer-initiated refund is FULL-remaining-amount only - never a
    client-chosen amount (§T, §Y). A partial refund is an Ops/cancellation
    workflow action (``api/ops_payments.py``), which alone may pass an
    explicit amount, still bounded by the same domain invariant
    (0 <= refunded <= captured, §I)."""
    body = body or {}
    session = get_optional_session(request)
    if session is None:
        raise PaymentNotAllowed("Sign in required to request a refund.", status_code=401)
    require_csrf(request, session)
    db = get_db()
    payment = _get_owned_payment(db, payment_id, user_id=session.user_id)
    if payment is None:
        raise PaymentNotAllowed("No such payment.", status_code=404)

    remaining = round(payment.captured_amount - payment.refunded_amount, 2)
    if remaining <= 0:
        raise PaymentNotAllowed("Nothing left to refund on this payment.", status_code=409)

    idempotency_key = str(body.get("idempotency_key", "")).strip()
    if len(idempotency_key) < 8:
        raise PaymentNotAllowed("a client idempotency_key of at least 8 characters is required.")
    reason = str(body.get("reason", "customer requested"))[:200]

    provider = resolve_provider()
    try:
        updated_payment, refund = ps.request_refund(
            db, payment=payment, provider=provider, amount=remaining, reason=reason,
            idempotency_key=idempotency_key,
        )
    except store.StaleVersion:
        raise PaymentNotAllowed(
            "This payment changed concurrently; refresh and try again.", status_code=409,
        )
    except (ValueError, ps.InvalidRefundAmount) as error:
        raise PaymentNotAllowed(str(error), status_code=409)
    return {
        **_payment_dto(updated_payment),
        "refund_id": refund.refund_id, "refund_status": refund.status.value,
        "refund_amount": refund.amount,
    }


@router.post("/webhook/{provider_name}")
async def provider_webhook(provider_name: str, request: Request) -> dict:
    """Provider event ingestion (§O). Signature-verified, deduplicated by
    ``(provider, provider_event_id)`` (an atomic INSERT claim, §J), and a
    provider's claim is never trusted to overwrite a more-advanced local
    terminal state."""
    payload = await request.body()
    signature = request.headers.get("Stripe-Signature") or request.headers.get("X-Signature", "")
    # Observability only (V9 Limited Beta observability contract §9): never
    # the payload or signature - those may embed a client_secret/webhook
    # secret or a raw provider event body.
    log_event(_logger, "payment_webhook_received", provider=provider_name)
    provider = resolve_provider()
    if provider.name != provider_name:
        # A webhook arriving for a provider this deployment is not
        # currently configured to trust - reject rather than guess.
        raise HTTPException(status_code=400, detail={"message": "unrecognised provider"})
    try:
        event = provider.verify_event(payload=payload, signature=signature)
    except Exception:
        log_event(_logger, "payment_webhook_rejected", level=logging.WARNING, provider=provider_name, reason="unverifiable")
        raise HTTPException(status_code=400, detail={"message": "invalid or unverifiable event"})

    db = get_db()
    claimed = store.claim_provider_event(
        db, provider=provider_name, provider_event_id=event.provider_event_id,
        payment_id=None, event_type=event.event_type, payload=event.payload,
    )
    if not claimed:
        log_event(_logger, "payment_webhook_duplicate_ignored", provider=provider_name, event_type=event.event_type)
        return {"status": "duplicate_ignored"}  # already processed - safe replay (§O)

    if event.provider_reference:
        rows = db.query(
            "SELECT payment_id FROM payment_transactions WHERE provider_payment_reference=?",
            (event.provider_reference,),
        )
        if rows:
            payment = store.get_payment(db, rows[0]["payment_id"])
            if payment is not None and payment.status in (
                PaymentStatus.UNKNOWN, PaymentStatus.RECONCILIATION_REQUIRED,
                PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURE_PENDING, PaymentStatus.CANCEL_PENDING,
            ):
                # Only ever reconciled through the provider's own retrieve
                # (never the webhook payload's claim taken at face value) -
                # a webhook's role is "something happened, go check", not
                # "trust this status" (§O: "no trust in client-provided
                # payment status", extended here to the provider's own
                # push notification for defence in depth).
                ps.reconcile_payment(db, payment=payment, provider=provider)
    store.mark_provider_event_processed(db, provider=provider_name, provider_event_id=event.provider_event_id)
    log_event(_logger, "payment_webhook_processed", provider=provider_name, event_type=event.event_type)
    return {"status": "processed"}
