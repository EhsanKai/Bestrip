"""Detoura Ops console API (V8.5 Phase B).

Admin-only. Every route except ``/status`` and ``/session`` requires an ops
session (see :mod:`detoura.api.ops_auth`). This router exposes read access to
the booking record, the economics ledger, the admin audit trail and the
recovery queue. It exposes **no** write action on a booking yet - ticket
operations (cancel, change, rebook) are Phase C, and until then every such
action is reported as not implemented rather than faked.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from ..persistence import audit as audit_store
from ..persistence import bookings as bookings_store
from ..persistence import economics as economics_store
from ..persistence import get_db
from .ops_auth import (
    create_session,
    ops_enabled,
    require_ops,
    revoke_session,
    verify_shared_token,
)
from .ops_contracts import (
    OpsAuditEventDTO,
    OpsBookingDetailDTO,
    OpsBookingItemDTO,
    OpsBookingsPage,
    OpsBookingSummaryDTO,
    OpsEconomicsDTO,
    OpsLoginRequest,
    OpsOverviewDTO,
    OpsRecoveryPage,
    OpsSessionResponse,
)

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])

_PHASE_LABEL = {
    "awaiting_travelers": "Awaiting traveller details",
    "awaiting_confirmation": "Awaiting confirmation",
    "revalidating": "Revalidating",
    "reconfirm_required": "Reconfirmation required (price changed)",
    "issuing": "Issuing tickets",
    "complete": "Complete",
    "partial_failure": "Partial failure",
    "failed": "Failed",
    "guided_booking": "Guided booking (Basic — traveller books each ticket)",
}

# Ticket-level actions the spec enumerates. Only VIEW is real in Phase B; the
# rest are declared unavailable rather than faked (Phase C: ticket operations).
_PENDING = "Ticket operations arrive in a later phase (Phase C)."
_TICKET_ACTIONS = (
    ("VIEW", True, ""),
    ("REVALIDATE", False, _PENDING),
    ("CANCEL", False, _PENDING),
    ("REQUEST_CHANGE", False, _PENDING),
    ("CHANGE_DATE", False, _PENDING),
    ("REBOOK_REPLACE_LEG", False, _PENDING),
    ("RETRY_SAFE_OPERATION", False, _PENDING),
)


def _summary(rec: bookings_store.BookingRecord) -> OpsBookingSummaryDTO:
    confirmed = sum(1 for i in rec.items if i.state == "CONFIRMED")
    return OpsBookingSummaryDTO(
        booking_id=rec.booking_id,
        session_ref=rec.session_ref,
        journey_reference=rec.journey_reference,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
        mode=rec.mode,
        phase=rec.phase,
        phase_label=_PHASE_LABEL.get(rec.phase, rec.phase),
        trip_label=rec.trip_label,
        route_cities=rec.route_cities,
        party_size=rec.party_size,
        lead_name=rec.lead_name,
        lead_email=rec.lead_email,
        currency=rec.currency,
        service_tier=rec.service_tier,
        discovered_total=rec.discovered_total,
        current_total=rec.current_total,
        customer_total=rec.customer_total,
        recovery_state=rec.recovery_state,
        ticket_count=len(rec.items),
        confirmed_count=confirmed,
    )


def _item_dto(it: bookings_store.BookingItemRecord) -> OpsBookingItemDTO:
    from .ops_contracts import OpsActionDTO

    return OpsBookingItemDTO(
        sequence=it.sequence,
        origin_city=it.origin_city, origin_airport=it.origin_airport,
        destination_city=it.destination_city,
        destination_airport=it.destination_airport,
        departure=it.departure, arrival=it.arrival,
        carrier=it.carrier, flight_number=it.flight_number,
        offer_id=it.offer_id, provider=it.provider,
        duffel_order_id=it.provider_order_id,
        quoted_price=it.quoted_price, current_price=it.current_price,
        booked_price=it.booked_price, currency=it.currency,
        cabin_baggage=it.cabin_baggage, checked_baggage=it.checked_baggage,
        required=it.required, state=it.state, detail=it.detail,
        actions=[
            OpsActionDTO(action=a, enabled=e, reason=r)
            for (a, e, r) in _TICKET_ACTIONS
        ],
    )


def _economics_dto(booking_id: str) -> OpsEconomicsDTO | None:
    row = economics_store.get(get_db(), booking_id)
    if row is None:
        return None
    return OpsEconomicsDTO(
        currency=row.currency,
        supplier_cost=row.supplier_cost,
        detoura_service_fee=row.service_fee,
        detoura_markup=row.markup,
        discount=row.discount,
        customer_price=row.customer_price,
        detoura_gross_revenue=row.detoura_gross_revenue,
        markup_policy=f"{row.markup_policy_id}@v{row.markup_policy_version}",
        promo_code=row.promo_code,
        provider_cost_estimate=row.provider_cost_estimate,
        payment_cost=row.payment_cost,
        refund=row.refund,
        recovery_cost=row.recovery_cost,
        contribution_margin=row.contribution_margin,
        has_unknown_costs=row.has_unknown_costs,
    )


def _audit_dto(ev: audit_store.AuditEvent) -> OpsAuditEventDTO:
    return OpsAuditEventDTO(
        id=ev.id, ts=ev.ts, actor=ev.actor, action=ev.action,
        target_type=ev.target_type, target_id=ev.target_id,
        before=ev.before, after=ev.after, note=ev.note,
    )


# --- unauthenticated: is ops even configured here? ---------------------
@router.get("/status")
def ops_status() -> dict:
    return {"enabled": ops_enabled(), "test_mode": True}


@router.post("/session", response_model=OpsSessionResponse)
def ops_login(body: OpsLoginRequest) -> OpsSessionResponse:
    if not ops_enabled():
        raise HTTPException(status_code=503, detail={
            "message": "Detoura Ops is not configured on this deployment.",
        })
    if not verify_shared_token(body.token):
        raise HTTPException(status_code=401, detail={
            "message": "That ops token is not valid.",
        })
    token, ttl = create_session()
    audit_store.record(get_db(), actor="ops", action="OPS_LOGIN",
                       target_type="ops", target_id="session")
    return OpsSessionResponse(session_token=token, expires_in=ttl)


@router.post("/session/logout")
def ops_logout(
    actor: str = Depends(require_ops), authorization: str = Header(default="")
) -> dict:
    if authorization.lower().startswith("bearer "):
        revoke_session(authorization[7:].strip())
    return {"ok": True}


# --- authenticated -------------------------------------------------------
@router.get("/overview", response_model=OpsOverviewDTO)
def ops_overview(actor: str = Depends(require_ops)) -> OpsOverviewDTO:
    db = get_db()
    counts = bookings_store.counts_by_phase(db)
    recovery = len(bookings_store.recovery_queue(db, limit=500))
    audit_n = db.query_one("SELECT COUNT(*) AS n FROM audit_events")
    return OpsOverviewDTO(
        total_bookings=sum(counts.values()),
        counts_by_phase=counts,
        recovery_count=recovery,
        audit_events=int(audit_n["n"]) if audit_n else 0,
    )


@router.get("/bookings", response_model=OpsBookingsPage)
def ops_bookings(
    actor: str = Depends(require_ops),
    limit: int = Query(default=100, ge=1, le=500),
    phase: str | None = None,
    recovery: bool = False,
    search: str | None = Query(default=None, max_length=120),
) -> OpsBookingsPage:
    db = get_db()
    recs = bookings_store.list_bookings(
        db, limit=limit, phase=phase, recovery_only=recovery, search=search,
    )
    return OpsBookingsPage(
        bookings=[_summary(r) for r in recs],
        counts_by_phase=bookings_store.counts_by_phase(db),
        recovery_count=len(bookings_store.recovery_queue(db, limit=500)),
    )


@router.get("/bookings/{booking_id}", response_model=OpsBookingDetailDTO)
def ops_booking_detail(
    booking_id: str, actor: str = Depends(require_ops)
) -> OpsBookingDetailDTO:
    rec = bookings_store.get(get_db(), booking_id)
    if rec is None:
        raise HTTPException(status_code=404, detail={
            "message": "No such booking.",
        })
    base = _summary(rec).model_dump()
    return OpsBookingDetailDTO(
        **base,
        reconfirm_note=rec.reconfirm_note,
        items=[_item_dto(i) for i in rec.items],
        economics=_economics_dto(booking_id),
        audit=[
            _audit_dto(e)
            for e in audit_store.recent(get_db(), limit=100)
            if e.target_id in (booking_id, rec.journey_reference)
        ],
    )


@router.get("/recovery", response_model=OpsRecoveryPage)
def ops_recovery(actor: str = Depends(require_ops)) -> OpsRecoveryPage:
    recs = bookings_store.recovery_queue(get_db(), limit=300)
    by_state: dict[str, int] = {}
    for r in recs:
        by_state[r.recovery_state] = by_state.get(r.recovery_state, 0) + 1
    return OpsRecoveryPage(items=[_summary(r) for r in recs], by_state=by_state)


@router.get("/audit", response_model=list[OpsAuditEventDTO])
def ops_audit(
    actor: str = Depends(require_ops),
    limit: int = Query(default=200, ge=1, le=1000),
    target_type: str | None = None,
    target_id: str | None = None,
) -> list[OpsAuditEventDTO]:
    events = audit_store.recent(
        get_db(), limit=limit, target_type=target_type, target_id=target_id,
    )
    return [_audit_dto(e) for e in events]
