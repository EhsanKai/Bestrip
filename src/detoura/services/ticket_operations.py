"""Post-booking ticket operations, orchestrated (V8.5 C3).

Cancellation, change and recovery — each a deliberate multi-step workflow that
an operator drives and that is safe to run against a real (test-mode) provider.

Design rules, all load-bearing:

* **The persisted booking record is the source of truth.** A ``BookingRun`` has
  a one-hour TTL; an operator may cancel a booking days later. Everything here
  reads ``persistence.bookings`` and writes ``persistence.ticket_operations``.

* **Nothing executes without explicit operator approval.** Eligibility/quote
  steps are read-only. ``approve_*`` records a human decision. Only
  ``execute_*`` calls a mutating provider endpoint, and only from an APPROVED
  operation.

* **Idempotent execution.** Every execute carries an ``operation_id`` (and an
  optional client idempotency key). An execute against an operation already in
  a terminal state returns the stored result and makes no provider call.

* **Test mode only.** The Duffel factory refuses a non-test token; every
  provider response is checked for ``live_mode is False``. A DEMO_ONLY booking
  has no ``ord_...`` id, so provider actions return ``NOT_SUPPORTED_IN_DEMO``
  and are recorded as such — never as provider-confirmed truth.

* **CANCELLED ≠ REFUNDED.** The cancellation result carries the order state and
  the refund state separately, and a refund amount is only ever what the
  provider stated.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from ..models.ticket_ops import (
    CANCELLATION_TERMINAL,
    CancellationQuote,
    CancellationResult,
    CancellationState,
    ChangeQuote,
    ChangeResult,
    ChangeState,
    OperationKind,
    RefundStatus,
    TicketOperation,
)
from ..persistence import audit as audit_store
from ..persistence import bookings as bookings_store
from ..persistence import economics as economics_store
from ..persistence import ticket_ops as ops_store
from ..persistence.db import Database
from ..providers.duffel import (
    DuffelChangeError,
    DuffelChangeUnsupported,
    DuffelConfigurationError,
    DuffelOrderNotFound,
    DuffelTransportProvider,
    is_test_token,
)
from ..providers.http import (
    ProviderHttpError,
    RateLimiter,
    RetryingHttpClient,
    UrllibHttpClient,
)

DEMO_STATE = "NOT_SUPPORTED_IN_DEMO"


class TicketOpError(Exception):
    """A workflow-level problem (wrong state, unknown target, provider refusal).
    Carries an HTTP-ish status hint for the API layer."""

    def __init__(self, message: str, *, status: int = 409, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------
def duffel_for_ops() -> DuffelTransportProvider | None:
    """A bounded, test-mode Duffel client for ops mutations, or ``None`` when
    no sandbox token is configured (the caller then reports the action as
    unavailable rather than guessing)."""
    token = os.getenv("DUFFEL_ACCESS_TOKEN", "")
    if not is_test_token(token):
        return None
    http = RetryingHttpClient(
        UrllibHttpClient(), max_retries=2, rate_limiter=RateLimiter(0.25)
    )
    return DuffelTransportProvider(
        access_token=token, http_client=http, max_calls=24, timeout=20.0
    )


def _rec_or_raise(db: Database, booking_id: str) -> bookings_store.BookingRecord:
    rec = bookings_store.get(db, booking_id)
    if rec is None:
        raise TicketOpError("No such booking.", status=404)
    return rec


def _item_or_raise(rec: bookings_store.BookingRecord, sequence: int):
    if sequence == 0:
        return None  # whole-order operation
    for it in rec.items:
        if it.sequence == sequence:
            return it
    raise TicketOpError("No such ticket on this booking.", status=404)


def _is_demo(rec: bookings_store.BookingRecord, item) -> bool:
    if rec.mode != "sandbox_booked":
        return True
    if item is not None:
        return item.provider != "duffel" or not item.provider_order_id
    return not any(i.provider_order_id for i in rec.items)


def _audit(db, *, actor, action, target_id, before=None, after=None, note=""):
    audit_store.record(
        db, actor=actor, action=action, target_type="ticket_operation",
        target_id=target_id, before=before, after=after, note=note,
    )


def _set_recovery(db: Database, booking_id: str, state: str) -> None:
    """Reflect a ticket-op outcome on the booking's recovery flag so it shows
    in the Recovery Center. Best-effort."""
    try:
        rec = bookings_store.get(db, booking_id)
        if rec is None:
            return
        rec.recovery_state = state
        bookings_store.upsert(db, rec)
    except Exception:
        pass


# ======================================================================
# Cancellation
# ======================================================================
def check_cancellation_eligibility(
    db: Database, booking_id: str, sequence: int, *, actor: str,
    duffel_factory=None,
) -> TicketOperation:
    """Step A+B+C: create the operation, ask the provider what cancelling
    returns, store the quote. Read-only at the provider (Duffel's
    order_cancellations create step does not cancel anything)."""
    rec = _rec_or_raise(db, booking_id)
    item = _item_or_raise(rec, sequence)
    order_id = item.provider_order_id if item else next(
        (i.provider_order_id for i in rec.items if i.provider_order_id), None
    )
    # What was actually paid to the provider for what is being cancelled - a
    # per-person fare times the party size, summed across the affected legs.
    # Used only to tell a full refund from a partial one; never to *derive* a
    # refund amount.
    party = max(rec.party_size, 1)
    affected = [item] if item is not None else list(rec.items)
    paid = [
        i for i in affected
        if (i.booked_price or i.current_price or i.quoted_price)
    ]
    order_paid = round(
        sum((i.booked_price or i.current_price or i.quoted_price or 0.0) * party
            for i in paid),
        2,
    ) or None

    op = ops_store.create(
        db, booking_id=booking_id, sequence=sequence,
        kind=OperationKind.CANCELLATION,
        state=CancellationState.PENDING_ELIGIBILITY.value,
        provider_order_id=order_id, actor=actor,
        reason="eligibility check",
    )
    _audit(db, actor=actor, action="CANCELLATION_ELIGIBILITY_CHECKED",
           target_id=op.operation_id,
           note=f"booking {booking_id} ticket {sequence}")

    if _is_demo(rec, item):
        return ops_store.update(
            db, op.operation_id, state=DEMO_STATE,
            quote={"note": "This booking created no provider order; a real "
                           "cancellation cannot be performed."},
        )

    duffel = (duffel_factory or duffel_for_ops)()
    if duffel is None:
        return ops_store.update(
            db, op.operation_id, state=CancellationState.INELIGIBLE.value,
            quote={"note": "No Duffel sandbox token is configured; cannot "
                           "quote a cancellation."},
        )
    try:
        raw = duffel.create_order_cancellation(order_id)
    except DuffelOrderNotFound:
        return ops_store.update(
            db, op.operation_id, state=CancellationState.INELIGIBLE.value,
            quote={"note": "The provider no longer recognises this order."},
        )
    except (DuffelChangeUnsupported,) as e:
        return ops_store.update(
            db, op.operation_id, state=CancellationState.INELIGIBLE.value,
            quote={"note": f"The provider will not cancel this order: {e}"},
        )
    except (DuffelChangeError, DuffelConfigurationError, ProviderHttpError,
            TimeoutError, OSError) as e:
        raise TicketOpError(f"Could not reach the provider to quote a "
                            f"cancellation ({type(e).__name__}).", status=502)

    quote = _cancellation_quote_from_duffel(raw, order_paid=order_paid)
    return ops_store.update(
        db, op.operation_id, state=CancellationState.ELIGIBLE.value,
        provider_order_id=order_id,
        quote=quote.model_dump(mode="json"),
    )


def approve_cancellation(
    db: Database, operation_id: str, *, actor: str, reason: str = "",
) -> TicketOperation:
    """Step D: an operator explicitly authorises executing the cancellation."""
    op = _op_or_raise(db, operation_id, OperationKind.CANCELLATION)
    if op.state != CancellationState.ELIGIBLE.value:
        raise TicketOpError(
            f"Cannot approve a cancellation from state {op.state}."
        )
    updated = ops_store.update(
        db, operation_id, state=CancellationState.APPROVED.value,
        reason=reason or "operator approved cancellation",
    )
    _audit(db, actor=actor, action="CANCELLATION_APPROVED",
           target_id=operation_id, before={"state": op.state},
           after={"state": updated.state}, note=reason)
    return updated


def execute_cancellation(
    db: Database, operation_id: str, *, actor: str, idempotency_key: str = "",
    duffel_factory=None,
) -> TicketOperation:
    """Step E+F: confirm the cancellation at the provider and persist the
    truthful resulting state. Idempotent — a repeat returns the stored
    result."""
    op = _op_or_raise(db, operation_id, OperationKind.CANCELLATION)
    if op.state in CANCELLATION_TERMINAL or op.state == DEMO_STATE:
        return op  # already done; do not call the provider again
    if op.state == CancellationState.EXECUTING.value:
        return op  # a concurrent execute holds the claim; in flight
    if op.state != CancellationState.APPROVED.value:
        raise TicketOpError(
            f"A cancellation must be approved before it is executed "
            f"(state is {op.state})."
        )
    if idempotency_key:
        prior = ops_store.by_idempotency_key(db, idempotency_key)
        if prior is not None and prior.operation_id != operation_id:
            return prior

    # Atomic claim: exactly one execute moves APPROVED -> EXECUTING and goes on
    # to call the provider. A racing execute loses the claim and returns the
    # in-flight op - the provider confirm is never issued twice, and a good
    # terminal result is never overwritten by the loser's "already confirmed"
    # error.
    if not ops_store.claim(
        db, operation_id,
        from_state=CancellationState.APPROVED.value,
        to_state=CancellationState.EXECUTING.value,
    ):
        return ops_store.get(db, operation_id)  # type: ignore[return-value]

    quote = CancellationQuote.model_validate(op.quote_json or {})
    duffel = (duffel_factory or duffel_for_ops)()
    if duffel is None:
        # Release the claim so an operator can retry once a token is configured.
        ops_store.update(db, operation_id, state=CancellationState.APPROVED.value)
        raise TicketOpError("No Duffel sandbox token is configured.", status=503)

    cancellation_id = quote.provider_cancellation_id
    if not cancellation_id:
        ops_store.update(db, operation_id, state=CancellationState.APPROVED.value)
        raise TicketOpError("The stored cancellation quote has no provider id; "
                            "re-run the eligibility check.")

    _audit(db, actor=actor, action="CANCELLATION_EXECUTION_ATTEMPTED",
           target_id=operation_id,
           note=f"order {op.provider_order_id}")
    try:
        raw = duffel.confirm_order_cancellation(cancellation_id)
    except DuffelOrderNotFound:
        result = CancellationResult(
            state=CancellationState.CANCELLATION_FAILED,
            detail="The provider no longer recognises this order. It may have "
                   "been cancelled already, or purged - an operator must check.",
        )
        updated = _persist_cancellation_result(db, op, result, idempotency_key)
        _set_recovery(db, op.booking_id, "CANCELLATION_FAILED")
        _audit(db, actor=actor, action="CANCELLATION_EXECUTION_FAILED",
               target_id=operation_id, after={"state": updated.state},
               note=result.detail)
        return updated
    except (DuffelChangeError, DuffelConfigurationError, ProviderHttpError,
            TimeoutError, OSError) as e:
        result = CancellationResult(
            state=CancellationState.CANCELLATION_FAILED,
            detail=f"The provider did not complete the cancellation "
                   f"({type(e).__name__}). The order may still be live.",
        )
        updated = _persist_cancellation_result(db, op, result, idempotency_key)
        _set_recovery(db, op.booking_id, "CANCELLATION_FAILED")
        _audit(db, actor=actor, action="CANCELLATION_EXECUTION_FAILED",
               target_id=operation_id, after={"state": updated.state},
               note=result.detail)
        return updated

    result = _cancellation_result_from_duffel(raw, quote)
    updated = _persist_cancellation_result(db, op, result, idempotency_key)

    # Economics: record the refund the provider actually stated (0 for
    # non-refundable, the amount for refunded/partial, left UNKNOWN for pending).
    if result.refund_status in (RefundStatus.FULL, RefundStatus.PARTIAL,
                                RefundStatus.NONE):
        try:
            economics_store.update_costs(
                db, op.booking_id, actor=actor,
                refund=result.refund_amount or 0.0,
            )
        except KeyError:
            pass

    _set_recovery(
        db, op.booking_id,
        "" if result.state in CANCELLATION_TERMINAL
        and result.state is not CancellationState.CANCELLATION_FAILED
        else "CANCELLATION_FAILED",
    )
    _audit(db, actor=actor, action="CANCELLATION_EXECUTED",
           target_id=operation_id,
           before={"state": op.state},
           after={"state": updated.state,
                  "refund_status": result.refund_status.value},
           note=result.detail)
    return updated


def _persist_cancellation_result(
    db, op, result: CancellationResult, idempotency_key: str,
) -> TicketOperation:
    updated = ops_store.update(
        db, op.operation_id, state=result.state.value,
        result=result.model_dump(mode="json"),
    )
    if idempotency_key and not op.idempotency_key:
        with db.write() as conn:
            conn.execute(
                "UPDATE ticket_operations SET idempotency_key = ? "
                "WHERE operation_id = ? AND idempotency_key = ''",
                (idempotency_key, op.operation_id),
            )
    return updated


def _cancellation_quote_from_duffel(
    raw: dict, *, order_paid: float | None = None,
) -> CancellationQuote:
    return CancellationQuote(
        provider="duffel",
        provider_cancellation_id=raw.get("id"),
        currency=raw.get("refund_currency") or raw.get("total_currency") or "EUR",
        refund_amount=_f(raw.get("refund_amount")),
        order_paid_amount=order_paid,
        penalty_amount=_f(raw.get("penalty_amount")) if raw.get("penalty_amount") else None,
        refund_to=raw.get("refund_to") or "",
        expires_at=_dt(raw.get("expires_at")),
        live_mode=raw.get("live_mode"),
        conditions=tuple(
            c.get("description", "") for c in (raw.get("conditions") or [])
            if isinstance(c, dict)
        ),
    )


def _cancellation_result_from_duffel(
    raw: dict, quote: CancellationQuote,
) -> CancellationResult:
    refund_amount = _f(raw.get("refund_amount"))
    if refund_amount is None:
        refund_amount = quote.refund_amount
    currency = raw.get("refund_currency") or quote.currency
    confirmed_at = raw.get("confirmed_at")

    if not confirmed_at:
        return CancellationResult(
            state=CancellationState.CANCELLATION_FAILED,
            detail="The provider did not confirm the cancellation.",
            live_mode=raw.get("live_mode"),
        )

    # Cancelled for sure. Now the refund, kept as a separate fact.
    if refund_amount is None:
        return CancellationResult(
            state=CancellationState.REFUND_PENDING,
            refund_status=RefundStatus.UNKNOWN, currency=currency,
            provider_cancellation_id=raw.get("id"),
            provider_status="confirmed",
            detail="Order cancelled. The provider has not yet stated the refund.",
            live_mode=raw.get("live_mode"),
        )
    if refund_amount <= 0.0:
        return CancellationResult(
            state=CancellationState.NON_REFUNDABLE,
            refund_status=RefundStatus.NONE, currency=currency,
            refund_amount=0.0, provider_cancellation_id=raw.get("id"),
            provider_status="confirmed",
            detail="Order cancelled. The provider stated no refund is due.",
            live_mode=raw.get("live_mode"),
        )
    paid = quote.order_paid_amount
    penalty = _f(raw.get("penalty_amount")) or quote.penalty_amount or 0.0
    partial = bool(
        (paid and refund_amount + 0.01 < paid) or penalty > 0.01
    )
    return CancellationResult(
        state=(CancellationState.PARTIALLY_REFUNDED if partial
               else CancellationState.REFUNDED),
        refund_status=(RefundStatus.PARTIAL if partial else RefundStatus.FULL),
        currency=currency, refund_amount=round(refund_amount, 2),
        provider_cancellation_id=raw.get("id"), provider_status="confirmed",
        detail=(f"Order cancelled. {currency} {refund_amount:.2f} refunded"
                + (" (partial — a fee was retained)." if partial else ".")),
        live_mode=raw.get("live_mode"),
    )


# ======================================================================
# Change / rebooking
# ======================================================================
def check_change_capability(
    db: Database, booking_id: str, sequence: int, *, actor: str,
    duffel_factory=None,
) -> TicketOperation:
    """Inspect whether this order can be changed at all, before any change
    action is offered. Records a CHANGE operation with the capability."""
    rec = _rec_or_raise(db, booking_id)
    item = _item_or_raise(rec, sequence)
    order_id = item.provider_order_id if item else next(
        (i.provider_order_id for i in rec.items if i.provider_order_id), None
    )
    op = ops_store.create(
        db, booking_id=booking_id, sequence=sequence,
        kind=OperationKind.CHANGE, state=ChangeState.PENDING_CAPABILITY.value,
        provider_order_id=order_id, actor=actor, reason="capability check",
    )
    _audit(db, actor=actor, action="CHANGE_CAPABILITY_CHECKED",
           target_id=op.operation_id, note=f"booking {booking_id} ticket {sequence}")

    if _is_demo(rec, item):
        return ops_store.update(
            db, op.operation_id, state=DEMO_STATE,
            quote={"capability": "NOT_SUPPORTED",
                   "note": "This booking created no provider order; a real "
                           "change cannot be performed."},
        )
    duffel = (duffel_factory or duffel_for_ops)()
    if duffel is None:
        return ops_store.update(
            db, op.operation_id, state=ChangeState.CAPABILITY_UNKNOWN.value,
            quote={"capability": "UNKNOWN",
                   "note": "No Duffel sandbox token is configured — cannot "
                           "determine whether this order can be changed."},
        )
    try:
        order = duffel.get_order(order_id)
    except (DuffelOrderNotFound,):
        return ops_store.update(
            db, op.operation_id, state=ChangeState.NOT_SUPPORTED.value,
            quote={"capability": "NOT_SUPPORTED",
                   "note": "The provider no longer recognises this order."},
        )
    except (DuffelChangeError, ProviderHttpError, TimeoutError, OSError) as e:
        raise TicketOpError(f"Could not reach the provider ({type(e).__name__}).",
                            status=502)

    available = order.get("available_actions") or []
    can_change = "change" in available or bool(order.get("changeable"))
    cap = "ORDER_CHANGE" if can_change else "NOT_SUPPORTED"
    return ops_store.update(
        db, op.operation_id,
        state=(ChangeState.QUOTED.value if can_change
               else ChangeState.NOT_SUPPORTED.value),
        quote={"capability": cap,
               "available_actions": available,
               "note": ("This order can be changed via the provider."
                        if can_change else
                        "The provider does not offer a change for this order.")},
    )


def approve_change(db: Database, operation_id: str, *, actor: str,
                   reason: str = "") -> TicketOperation:
    op = _op_or_raise(db, operation_id, OperationKind.CHANGE)
    if op.state not in (ChangeState.QUOTED.value,):
        raise TicketOpError(f"Cannot approve a change from state {op.state}.")
    updated = ops_store.update(db, operation_id, state=ChangeState.APPROVED.value,
                               reason=reason or "operator approved change")
    _audit(db, actor=actor, action="CHANGE_APPROVED", target_id=operation_id,
           before={"state": op.state}, after={"state": updated.state}, note=reason)
    return updated


def execute_change(db: Database, operation_id: str, *, actor: str,
                   idempotency_key: str = "",
                   duffel_factory=None) -> TicketOperation:
    """Execute an approved change. In this milestone the change offer flow is
    not driven end to end from the console (it needs new-slice construction);
    an approved change without a stored ``change_offer_id`` is recorded as
    requiring a manual provider step rather than faking success."""
    op = _op_or_raise(db, operation_id, OperationKind.CHANGE)
    if op.state in (ChangeState.APPLIED.value, ChangeState.FAILED.value,
                    ChangeState.NOT_SUPPORTED.value, DEMO_STATE):
        return op
    if op.state != ChangeState.APPROVED.value:
        raise TicketOpError(f"A change must be approved first (state {op.state}).")

    offer_id = (op.quote_json or {}).get("change_offer_id")
    if not offer_id:
        result = ChangeResult(
            state=ChangeState.FAILED,
            detail="No provider change offer is attached. Construct the "
                   "replacement itinerary and obtain a change offer before "
                   "executing.",
        )
        _audit(db, actor=actor, action="CHANGE_EXECUTION_BLOCKED",
               target_id=operation_id, note=result.detail)
        return ops_store.update(db, operation_id, state=ChangeState.FAILED.value,
                                result=result.model_dump(mode="json"))
    duffel = (duffel_factory or duffel_for_ops)()
    if duffel is None:
        raise TicketOpError("No Duffel sandbox token is configured.", status=503)
    _audit(db, actor=actor, action="CHANGE_EXECUTION_ATTEMPTED",
           target_id=operation_id, note=f"offer {offer_id}")
    try:
        raw = duffel.create_and_confirm_order_change(offer_id)
    except (DuffelChangeUnsupported, DuffelChangeError, ProviderHttpError,
            DuffelConfigurationError, TimeoutError, OSError) as e:
        result = ChangeResult(state=ChangeState.FAILED,
                              detail=f"The provider did not apply the change "
                                     f"({type(e).__name__}).")
        _set_recovery(db, op.booking_id, "CHANGE_REQUIRES_ACTION")
        _audit(db, actor=actor, action="CHANGE_EXECUTION_FAILED",
               target_id=operation_id, note=result.detail)
        return ops_store.update(db, operation_id, state=ChangeState.FAILED.value,
                                result=result.model_dump(mode="json"))
    result = ChangeResult(
        state=ChangeState.APPLIED,
        new_provider_order_id=raw.get("order_id") or raw.get("id"),
        charged_amount=_f(raw.get("change_total_amount")),
        currency=raw.get("change_total_currency") or "EUR",
        provider_status="confirmed",
        detail="The provider applied the order change.",
        live_mode=raw.get("live_mode"),
    )
    _audit(db, actor=actor, action="CHANGE_EXECUTED", target_id=operation_id,
           after={"state": result.state.value}, note=result.detail)
    return ops_store.update(db, operation_id, state=ChangeState.APPLIED.value,
                            result=result.model_dump(mode="json"))


# ======================================================================
# Recovery workflow
# ======================================================================
_RECOVERY_STEPS = ("INSPECTED", "REVALIDATED", "REPLACEMENT_FOUND",
                   "COMPARED", "APPROVED", "EXECUTED", "ABANDONED", "FAILED")


def start_recovery(db: Database, booking_id: str, sequence: int, *, actor: str,
                   reason: str) -> TicketOperation:
    rec = _rec_or_raise(db, booking_id)
    _item_or_raise(rec, sequence)
    op = ops_store.create(
        db, booking_id=booking_id, sequence=sequence,
        kind=OperationKind.RECOVERY, state="INSPECTED", actor=actor,
        reason=reason,
    )
    _audit(db, actor=actor, action="RECOVERY_STARTED", target_id=op.operation_id,
           note=f"{booking_id} ticket {sequence}: {reason}")
    return op


def record_recovery_candidate(
    db: Database, operation_id: str, *, actor: str, candidate: dict,
) -> TicketOperation:
    """Attach a proposed replacement (route, dates, carrier, fare, baggage,
    connections) for operator comparison. No provider write."""
    op = _op_or_raise(db, operation_id, OperationKind.RECOVERY)
    if op.state in ("EXECUTED", "ABANDONED"):
        raise TicketOpError(f"This recovery is closed ({op.state}).")
    quote = dict(op.quote_json or {})
    quote["candidate"] = candidate
    quote["compared_at"] = datetime.now(timezone.utc).isoformat()
    updated = ops_store.update(db, operation_id, state="COMPARED", quote=quote)
    _audit(db, actor=actor, action="RECOVERY_REPLACEMENT_FOUND",
           target_id=operation_id,
           note=candidate.get("summary", "candidate recorded"))
    return updated


def approve_recovery(db: Database, operation_id: str, *, actor: str,
                     reason: str = "") -> TicketOperation:
    op = _op_or_raise(db, operation_id, OperationKind.RECOVERY)
    if op.state != "COMPARED":
        raise TicketOpError("Record and compare a replacement before approving.")
    updated = ops_store.update(db, operation_id, state="APPROVED",
                               reason=reason or "operator approved replacement")
    _audit(db, actor=actor, action="RECOVERY_APPROVED", target_id=operation_id,
           before={"state": op.state}, after={"state": "APPROVED"}, note=reason)
    return updated


def execute_recovery(db: Database, operation_id: str, *, actor: str,
                     idempotency_key: str = "") -> TicketOperation:
    """Execute an approved recovery. This milestone does not auto-book a
    replacement leg from the console (that needs a fresh acquisition +
    order flow); it records the operator's executed decision and the manual
    provider action required, and never silently books."""
    op = _op_or_raise(db, operation_id, OperationKind.RECOVERY)
    if op.state in ("EXECUTED", "ABANDONED", "FAILED"):
        return op
    if op.state != "APPROVED":
        raise TicketOpError(f"A recovery must be approved first (state {op.state}).")
    if idempotency_key:
        prior = ops_store.by_idempotency_key(db, idempotency_key)
        if prior is not None and prior.operation_id != operation_id:
            return prior
    result = {
        "executed_by": actor,
        "at": datetime.now(timezone.utc).isoformat(),
        "note": "Operator-approved replacement recorded. Book the replacement "
                "leg through the normal acquisition + order flow; this action "
                "does not auto-book.",
        "candidate": (op.quote_json or {}).get("candidate"),
    }
    updated = ops_store.update(db, operation_id, state="EXECUTED", result=result)
    if idempotency_key and not op.idempotency_key:
        with db.write() as conn:
            conn.execute(
                "UPDATE ticket_operations SET idempotency_key = ? "
                "WHERE operation_id = ? AND idempotency_key = ''",
                (idempotency_key, operation_id),
            )
    _set_recovery(db, op.booking_id, "")
    _audit(db, actor=actor, action="RECOVERY_EXECUTED", target_id=operation_id,
           before={"state": op.state}, after={"state": "EXECUTED"})
    return updated


def abandon_recovery(db: Database, operation_id: str, *, actor: str,
                     reason: str) -> TicketOperation:
    op = _op_or_raise(db, operation_id, OperationKind.RECOVERY)
    updated = ops_store.update(db, operation_id, state="ABANDONED", reason=reason)
    _audit(db, actor=actor, action="RECOVERY_ABANDONED", target_id=operation_id,
           note=reason)
    return updated


# ---------------------------------------------------------------------------
def _op_or_raise(db, operation_id, kind: OperationKind) -> TicketOperation:
    op = ops_store.get(db, operation_id)
    if op is None or op.kind is not kind:
        raise TicketOpError("No such operation.", status=404)
    return op


def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dt(v):
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
