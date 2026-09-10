"""Detoura Ops console API (V8.5 Phase B).

Admin-only. Every route except ``/status`` and ``/session`` requires an ops
session (see :mod:`detoura.api.ops_auth`). This router exposes read access to
the booking record, the economics ledger, the admin audit trail and the
recovery queue. It exposes **no** write action on a booking yet - ticket
operations (cancel, change, rebook) are Phase C, and until then every such
action is reported as not implemented rather than faked.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from ..models.commercial import ServiceTier
from ..models.promo import PromoCode, PromoKind, PromoTarget
from ..persistence import analytics as analytics_store
from ..persistence import audit as audit_store
from ..persistence import bookings as bookings_store
from ..persistence import economics as economics_store
from ..persistence import get_db
from ..persistence import policies as policies_store
from ..persistence import promos as promos_store
from ..services.commercial import CommercialPricingService
from .ops_auth import (
    create_session,
    ops_enabled,
    require_ops,
    revoke_session,
    verify_shared_token,
)
from .ops_contracts import (
    CreateMarkupPolicyRequest,
    MarkupPolicyConfigDTO,
    MarkupPolicyDTO,
    MarkupPreviewDTO,
    MarkupPreviewLineDTO,
    MarkupPreviewRequest,
    OpsAuditEventDTO,
    OpsBookingDetailDTO,
    OpsBookingItemDTO,
    OpsBookingsPage,
    OpsBookingSummaryDTO,
    OpsEconomicsDTO,
    OpsLoginRequest,
    OpsOverviewDTO,
    OpsPromoDetailDTO,
    OpsPromoDTO,
    OpsPromoRedemptionDTO,
    OpsRecoveryPage,
    OpsSessionResponse,
    UpsertPromoRequest,
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


# ======================================================================
# V8.5 Phase C2 — commercial + promo management, finance, analytics
# ======================================================================
def _parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail={"message": f"bad date: {value}"})


# --- markup policy management ---------------------------------------
def _policy_dto(db, policy_id: str, version: int, label: str, active: bool,
                created_at: str) -> MarkupPolicyDTO:
    policy = policies_store.get_policy(db, policy_id, version)
    cfg = None
    if policy is not None:
        cfg = MarkupPolicyConfigDTO(**policies_store.policy_config(policy))
    priced = db.query_one(
        "SELECT COUNT(*) AS n FROM booking_economics "
        "WHERE markup_policy_id = ? AND markup_policy_version = ?",
        (policy_id, version),
    )
    return MarkupPolicyDTO(
        policy_id=policy_id, version=version, label=label, active=active,
        created_at=created_at, config=cfg,
        bookings_priced=int(priced["n"]) if priced else 0,
    )


@router.get("/commercial/policies", response_model=list[MarkupPolicyDTO])
def ops_list_policies(actor: str = Depends(require_ops)) -> list[MarkupPolicyDTO]:
    db = get_db()
    return [
        _policy_dto(db, r["policy_id"], r["version"], r["label"],
                    bool(r["active"]), r["created_at"])
        for r in policies_store.list_policies(db)
    ]


@router.post("/commercial/policies", response_model=MarkupPolicyDTO)
def ops_create_policy(
    body: CreateMarkupPolicyRequest, actor: str = Depends(require_ops)
) -> MarkupPolicyDTO:
    db = get_db()
    pid = policies_store.DEFAULT_POLICY_ID
    version = policies_store.next_version(db, pid)
    try:
        policy = policies_store.build_policy(
            policy_id=pid, version=version, label=body.label,
            basic_percentage=body.basic_percentage,
            basic_fixed_fee=body.basic_fixed_fee,
            all_in_one_percentage=body.all_in_one_percentage,
            all_in_one_fixed_fee=body.all_in_one_fixed_fee,
            max_percentage=body.max_percentage,
            max_fixed_fee=body.max_fixed_fee,
            min_total_fee=body.min_total_fee,
            max_total_fee=body.max_total_fee,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"message": str(e)})
    policies_store.save_policy(db, policy, active=body.activate, actor=actor)
    row = next(
        r for r in policies_store.list_policies(db)
        if r["policy_id"] == pid and r["version"] == version
    )
    return _policy_dto(db, pid, version, row["label"], bool(row["active"]),
                       row["created_at"])


@router.post(
    "/commercial/policies/{policy_id}/{version}/activate",
    response_model=MarkupPolicyDTO,
)
def ops_activate_policy(
    policy_id: str, version: int, actor: str = Depends(require_ops)
) -> MarkupPolicyDTO:
    db = get_db()
    try:
        policies_store.set_active(db, policy_id, version, actor=actor)
    except KeyError:
        raise HTTPException(status_code=404, detail={"message": "No such policy version."})
    row = next(
        r for r in policies_store.list_policies(db)
        if r["policy_id"] == policy_id and r["version"] == version
    )
    return _policy_dto(db, policy_id, version, row["label"], True,
                       row["created_at"])


@router.post("/commercial/preview", response_model=MarkupPreviewDTO)
def ops_preview_policy(
    body: MarkupPreviewRequest, actor: str = Depends(require_ops)
) -> MarkupPreviewDTO:
    db = get_db()
    policy = None
    ref = "active policy"
    if body.draft is not None:
        d = body.draft
        try:
            policy = policies_store.build_policy(
                policy_id="preview", version=1, label="draft preview",
                basic_percentage=d.basic_percentage,
                basic_fixed_fee=d.basic_fixed_fee,
                all_in_one_percentage=d.all_in_one_percentage,
                all_in_one_fixed_fee=d.all_in_one_fixed_fee,
                max_percentage=d.max_percentage, max_fixed_fee=d.max_fixed_fee,
                min_total_fee=d.min_total_fee, max_total_fee=d.max_total_fee,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"message": str(e)})
        ref = "draft"
    elif body.policy_id and body.version:
        policy = policies_store.get_policy(db, body.policy_id, body.version)
        if policy is None:
            raise HTTPException(status_code=404, detail={"message": "No such policy version."})
        ref = f"{body.policy_id}@v{body.version}"

    svc = CommercialPricingService(db)
    lines: list[MarkupPreviewLineDTO] = []
    totals: dict[str, float] = {}
    for tier in (ServiceTier.BASIC, ServiceTier.ALL_IN_ONE):
        res = svc.quote(
            supplier_transport=body.supplier_total, currency=body.currency.upper(),
            ticket_count=body.ticket_count, service_tier=tier,
            markup_policy=policy,
        )
        b = res.quote.breakdown
        totals[tier.value] = b.customer_total
        lines.append(MarkupPreviewLineDTO(
            tier=tier.value, supplier_total=b.supplier_total,
            detoura_service_fee=b.detoura_service_fee,
            detoura_markup=b.detoura_markup,
            detoura_fee_total=b.detoura_revenue_gross,
            customer_total=b.customer_total,
            bounded=res.markup.bounded,
            explanation=list(res.markup.explanation),
        ))
    return MarkupPreviewDTO(
        policy_ref=ref,
        invariant_ok=totals["ALL_IN_ONE"] + 1e-6 >= totals["BASIC"],
        lines=lines,
    )


# --- promo management ---------------------------------------------
def _promo_dto(db, promo: PromoCode, *, detail: bool = False):
    st = promos_store.promo_stats(db, promo.code)
    base = dict(
        code=promo.code, label=promo.label, enabled=promo.enabled,
        kind=promo.kind.value, value=promo.value, currency=promo.currency,
        target=promo.target.value, starts_at=promo.starts_at,
        ends_at=promo.ends_at, global_limit=promo.global_limit,
        per_user_limit=promo.per_user_limit,
        min_order_value=promo.min_order_value, max_discount=promo.max_discount,
        eligible_tiers=[t.value for t in promo.eligible_tiers],
        redemptions=st["redemptions"], discount_total=st["discount_total"],
        revenue_impact=st["revenue_impact"],
        bookings_with_code=st["bookings_with_code"],
    )
    if not detail:
        return OpsPromoDTO(**base)
    return OpsPromoDetailDTO(**base, redemption_log=[
        OpsPromoRedemptionDTO(
            booking_id=r.booking_id, discount_amount=r.discount_amount,
            currency=r.currency, redeemed_at=r.redeemed_at,
        )
        for r in promos_store.redemptions_for(db, promo.code)
    ])


@router.get("/promos", response_model=list[OpsPromoDTO])
def ops_list_promos(actor: str = Depends(require_ops)) -> list[OpsPromoDTO]:
    db = get_db()
    return [_promo_dto(db, p) for p in promos_store.list_promos(db)]


@router.get("/promos/{code}", response_model=OpsPromoDetailDTO)
def ops_promo_detail(code: str, actor: str = Depends(require_ops)) -> OpsPromoDetailDTO:
    db = get_db()
    promo = promos_store.get_promo(db, code)
    if promo is None:
        raise HTTPException(status_code=404, detail={"message": "No such code."})
    return _promo_dto(db, promo, detail=True)


@router.post("/promos", response_model=OpsPromoDetailDTO)
def ops_upsert_promo(
    body: UpsertPromoRequest, actor: str = Depends(require_ops)
) -> OpsPromoDetailDTO:
    db = get_db()
    try:
        promo = PromoCode(
            code=body.code, label=body.label, enabled=body.enabled,
            kind=PromoKind(body.kind), value=body.value,
            currency=body.currency.upper(), target=PromoTarget(body.target),
            starts_at=body.starts_at, ends_at=body.ends_at,
            global_limit=body.global_limit, per_user_limit=body.per_user_limit,
            min_order_value=body.min_order_value, max_discount=body.max_discount,
            eligible_tiers=tuple(
                ServiceTier(t) for t in body.eligible_tiers
                if t in ("BASIC", "ALL_IN_ONE")
            ),
        )
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail={"message": str(e)})
    promos_store.save_promo(db, promo, actor=actor)
    return _promo_dto(db, promo, detail=True)


@router.post("/promos/{code}/enable", response_model=OpsPromoDetailDTO)
def ops_enable_promo(code: str, actor: str = Depends(require_ops)) -> OpsPromoDetailDTO:
    return _set_promo_enabled(code, True, actor)


@router.post("/promos/{code}/disable", response_model=OpsPromoDetailDTO)
def ops_disable_promo(code: str, actor: str = Depends(require_ops)) -> OpsPromoDetailDTO:
    return _set_promo_enabled(code, False, actor)


def _set_promo_enabled(code: str, enabled: bool, actor: str) -> OpsPromoDetailDTO:
    db = get_db()
    try:
        promos_store.set_enabled(db, code, enabled, actor=actor)
    except KeyError:
        raise HTTPException(status_code=404, detail={"message": "No such code."})
    return _promo_dto(db, promos_store.get_promo(db, code), detail=True)


# --- finance ------------------------------------------------------
@router.get("/finance")
def ops_finance(
    actor: str = Depends(require_ops),
    since: str | None = None,
    until: str | None = None,
) -> dict:
    db = get_db()
    s, u = _parse_when(since), _parse_when(until)
    summary = economics_store.finance_summary(db, since=s, until=u)
    tiers = analytics_store.tier_selection(db, since=s, until=u)
    for tier, block in summary["by_tier"].items():
        block["conversion"] = tiers.get(tier, {}).get("conversion")
        block["tier_selected"] = tiers.get(tier, {}).get("selected", 0)
    summary["test_data"] = True
    summary["window"] = {"since": since, "until": until}
    return summary


# --- analytics ---------------------------------------------------
@router.get("/analytics")
def ops_analytics(
    actor: str = Depends(require_ops),
    since: str | None = None,
    until: str | None = None,
) -> dict:
    db = get_db()
    s, u = _parse_when(since), _parse_when(until)
    return {
        "test_data": True,
        "window": {"since": since, "until": until},
        "event_counts": analytics_store.event_counts(db, since=s, until=u),
        "funnel": analytics_store.funnel(db, since=s, until=u),
        "tier_selection": analytics_store.tier_selection(db, since=s, until=u),
        "promo_impact": analytics_store.promo_impact(db, since=s, until=u),
        "repeat_search": analytics_store.repeat_search_rate(db, since=s, until=u),
    }


@router.get("/analytics/events")
def ops_analytics_events(
    actor: str = Depends(require_ops),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict]:
    return analytics_store.recent_events(get_db(), limit=limit)
