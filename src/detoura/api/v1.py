"""The Detoura product API (V5.9).

`/api/v1` is what the frontend talks to. The engine's own routes stay mounted
at the root for development and for anyone who wants the full trace, but no
screen uses them: this router is the contract, and it is deliberately narrower
than what the engine can say.

The most important thing in this file is not an endpoint, it is the
distinction the search endpoint refuses to blur. Empty recommendations plus a
provider outage returns issues and no guidance; empty recommendations with
every provider healthy returns guidance and no issues. The UI renders those as
completely different screens, and it can only do that because the backend
decided which one is true.
"""

from __future__ import annotations

import copy
import os
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from ..models.itinerary import (
    Itinerary,
    PlannerMetadata,
    PlanResult,
    StaySummary,
    TravelValueBreakdown,
)
from ..models.transport import TransportOption, TransportType
from ..models.trip import TravelPreferences, TripRequest
from ..profiles import PROFILES, ProfileName
from ..providers.failures import FailureLog, ProviderFailureKind
from ..search_modes import MODE_SETTINGS, SearchMode, apply_mode
from ..services.budget_sensitivity import analyze_budget_sensitivity
from ..services.feedback import record_feedback
from ..models.booking import BookingState, PriceTolerance
from ..models.traveler import Traveler, TravelerGender, TravelerParty, TravelerTitle
from ..providers.duffel import DuffelTransportProvider, is_test_token
from ..providers.http import RateLimiter, RetryingHttpClient, UrllibHttpClient
from ..services.booking_flow import (
    attach_travelers,
    booking_store,
    build_travel_pass,
    create_run_demo,
    create_run_from_selection,
    start_confirmation,
)
from ..services.booking_commercial import finalize_economics, price_run
from ..services.booking_persistence import persist_run
from ..services.booking_orchestrator import BookingPhase
from ..models.commercial import ServiceTier
from ..persistence import analytics as analytics_store
from ..persistence import get_db
from ..models.travel_pass import PassMode, PassStatus
from ..services.planner import TravelPlanner
from ..services.confidence import SearchQuality
from ..services.recheck import recheck_trip
from ..services.reoptimizer import EditConflict, reoptimize
from ..services.revalidation import RevalidationLimitExceeded, revalidate_selection
from ..services.selection_store import selection_store
from .assembler import build_response, recommendation_dto
from .contracts import (
    BookingIntentResponse,
    BookingItemStateDTO,
    ChangeDiffDTO,
    CommercialPreviewRequest,
    CommercialPreviewResponse,
    CommercialSummaryDTO,
    ConfirmBookingRequest,
    CreateBookingIntentRequest,
    GuidedMarkRequest,
    OfferChangeDTO,
    PriceBreakdownDTO,
    SelfServiceItineraryDTO,
    SelfServiceTicketDTO,
    ServiceTierOptionDTO,
    SetCommercialOptionsRequest,
    RevalidateRequest,
    RevalidateResponse,
    RevalidatedOfferDTO,
    REVALIDATION_MESSAGES,
    SubmitTravelersRequest,
    TrackEventsRequest,
    TravelPassResponse,
    TravelPassTicketDTO,
    SimilarityDTO,
    TripRecommendation,
    TripReoptimizeRequest,
    TripReoptimizeResponse,
    RECHECK_MESSAGES,
    ProviderIssueDTO,
    RecheckComponentDTO,
    SessionProfileDTO,
    TripFeedbackRequest,
    TripFeedbackResponse,
    TripRecheckRequest,
    TripRecheckResponse,
    TripSearchRequest,
    TripSearchResponse,
)

router = APIRouter(prefix="/api/v1", tags=["detoura"])

_planner = TravelPlanner()


def get_planner() -> TravelPlanner:
    """Dependency hook so tests can inject a planner over synthetic fixtures."""
    return _planner


def to_trip_request(body: TripSearchRequest) -> TripRequest:
    """Product request -> engine request.

    The interest chips become preference weights. A chip is a statement of
    interest, not a measurement, so it maps to a single high weight rather than
    a fabricated distribution: pretending the traveler said "culture 0.83"
    would be inventing precision from a click.
    """
    interests = body.validated_interests()
    dislikes = body.validated_dislikes()
    weights = {name: 0.85 for name in interests}
    for name in dislikes:
        weights[name] = 0.0

    try:
        start = date.fromisoformat(body.date_from)
        end = date.fromisoformat(body.date_to)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    if end <= start:
        end = start + timedelta(days=body.duration_days)

    return TripRequest(
        origin=body.origin,
        # The engine searches up to preferred + flexibility; the response
        # labels anything above the preferred budget. The committed budget the
        # traveller set is never silently raised.
        budget=body.budget + max(0.0, body.budget_flex),
        travelers=body.travelers,
        duration_days=body.duration_days,
        date_from=start,
        date_to=end,
        date_flexible=body.date_flexible,
        transport_preferences=body.transport,
        preferred_destinations=body.preferred_destinations,
        avoid_destinations=body.avoided_destinations,
        previously_visited=body.previously_visited,
        preferred_experiences=interests,
        disliked_experiences=dislikes,
        preferred_city_count=body.preferred_city_count,
        accommodation_preference=body.accommodation_preference,
        baggage=body.baggage,
        preferences=TravelPreferences(**weights) if weights else TravelPreferences(),
        profile=body.profile,
    )


def _closest_price(planner: TravelPlanner, request: TripRequest) -> float | None:
    """The cheapest trip that exists if the budget were not binding.

    Only called when nothing matched, and only to answer the one question a
    traveler actually has at that moment: *how far off was I?* Re-planning at a
    much larger budget is the honest way to find out; guessing would not be.
    """
    try:
        probe = request.model_copy(update={"budget": request.budget * 3})
        result = planner.plan(probe)
    except ValueError:
        return None
    if not result.recommendations:
        return None
    return min(item.total_cost for item in result.recommendations)


@router.post("/search", response_model=TripSearchResponse)
def search(
    body: TripSearchRequest,
    planner: TravelPlanner = Depends(get_planner),
) -> TripSearchResponse:
    """Find trips worth taking.

    The search mode decides how hard we look; it is the only search control the
    product exposes, and it maps to engine configuration here rather than
    leaking a beam width into the client.
    """
    request = to_trip_request(body)
    mode = body.search_mode
    failures = FailureLog()

    # A per-request planner only when the mode actually needs different
    # configuration, so the default SMART path keeps the shared warm caches.
    settings = apply_mode(planner.config, mode)
    if settings == planner.config:
        active = planner
    else:
        active = TravelPlanner(
            transport_provider=planner.transport.inner,
            destination_provider=planner.destinations,
            config=settings,
            accommodation_provider=planner.accommodation.inner,
            ground_transfer_provider=planner.ground_transfer.inner,
        )

    try:
        # The log travels with the request, not with the planner: the planner
        # is shared across concurrent requests and a log on the instance would
        # report one caller's outage inside another caller's answer.
        result = active.plan(request, profile=body.profile, failures=failures)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:  # noqa: BLE001 - see below
        # Defence in depth, and deliberately broad.
        #
        # With the resilience wrappers in place a provider fault is caught at
        # the provider and recorded, so this should not fire. It exists for
        # the fault that is not a provider's - a bug in scoring, an unexpected
        # shape from a new integration - because the alternative is a bare 500
        # that tells the client nothing it can act on. A 503 with a retryable
        # issue is honest: something upstream of the answer broke, and trying
        # again is reasonable advice.
        failures.record(
            ProviderFailureKind.UNAVAILABLE,
            "search",
            detail=str(error),
            context="planning",
        )
        raise HTTPException(
            status_code=503,
            detail={
                "message": (
                    "We could not complete this search. That is a problem on "
                    "our side, not a sign that no trips exist."
                ),
                "issue": {
                    "kind": ProviderFailureKind.UNAVAILABLE.value,
                    "provider": "search",
                    "retryable": True,
                },
            },
        ) from error

    closest = None
    if not result.recommendations and not failures.degraded:
        closest = _closest_price(active, request)

    return build_response(
        result,
        request,
        body,
        mode=mode,
        failures=failures,
        closest_price=closest,
    )


def _component_dto(result) -> RecheckComponentDTO:
    return RecheckComponentDTO(
        label=result.label,
        state=result.state,
        saved_price=result.saved_price,
        current_price=result.current_price,
        change=result.change,
        detail=result.detail,
    )


@router.post("/trips/recheck", response_model=TripRecheckResponse)
def recheck(
    body: TripRecheckRequest,
    planner: TravelPlanner = Depends(get_planner),
) -> TripRecheckResponse:
    """Re-price a saved trip against the providers.

    Stateless: the trip arrives in the request because saved trips live in the
    traveler's browser, so this works with no account, no database and no
    session. It also means the endpoint re-checks exactly the trip the client
    is showing, rather than one it looked up and hoped was the same.

    The status distinction that matters is ``UNVERIFIABLE`` versus
    ``UNAVAILABLE``: the first is a failure of ours, the second is a fact about
    the trip, and reporting the first as the second would talk someone out of a
    trip that is still bookable.
    """
    failures = FailureLog()
    result = recheck_trip(
        planner,
        legs=[
            {
                "origin": leg.origin,
                "destination": leg.destination,
                "departure": leg.departure,
                "operator": leg.operator,
                "price_per_person": leg.price_per_person,
            }
            for leg in body.legs
        ],
        stays=[
            {
                "city": stay.city,
                "arrival": stay.arrival,
                "departure": stay.departure,
                "name": stay.name,
                "cost": stay.cost,
            }
            for stay in body.stays
        ],
        transfers=[
            {"label": transfer.label, "cost": transfer.cost}
            for transfer in body.transfers
        ],
        travelers=body.travelers,
        saved_price=body.saved_price,
        failures=failures,
    )

    return TripRecheckResponse(
        trip_id=body.trip_id,
        status=result.status,
        message=RECHECK_MESSAGES[result.status],
        checked_at=result.checked_at,
        saved_price=result.saved_price,
        current_price=result.current_price,
        price_change=result.change,
        price_change_pct=result.change_pct,
        price_freshness=result.freshness,
        legs=[_component_dto(c) for c in result.legs],
        stays=[_component_dto(c) for c in result.stays],
        transfers=[_component_dto(c) for c in result.transfers],
        issues=[
            ProviderIssueDTO(
                kind=str(entry["kind"]),
                provider=str(entry["provider"]),
                message=str(entry["message"]),
                retryable=bool(entry["retryable"]),
                occurrences=int(entry["occurrences"]),
            )
            for entry in failures.summary()
        ],
    )


def _revalidation_duffel() -> DuffelTransportProvider:
    """A Duffel provider for the revalidation read path, or a 503.

    The token is read here from the environment and never from the request.
    Bounded transport: a rate limiter and the client's own retry ceiling, plus
    the provider's hard ``max_calls``. If no sandbox token is configured the
    endpoint is unavailable rather than silently trusting snapshot prices.
    """
    token = os.getenv("DUFFEL_ACCESS_TOKEN", "")
    if not is_test_token(token):
        raise HTTPException(
            status_code=503,
            detail={
                "message": (
                    "Revalidation needs a Duffel sandbox token and none is "
                    "configured. A trip cannot be confirmed without it."
                ),
                "issue": {"kind": "PROVIDER_UNCONFIGURED", "retryable": False},
            },
        )
    http = RetryingHttpClient(
        UrllibHttpClient(), max_retries=2, rate_limiter=RateLimiter(0.2)
    )
    return DuffelTransportProvider(
        access_token=token, http_client=http, max_calls=16, timeout=12.0
    )


@router.post("/trips/revalidate", response_model=RevalidateResponse)
def revalidate(body: RevalidateRequest) -> RevalidateResponse:
    """Re-check a selected itinerary against the provider before booking.

    The request carries only a `selection_id` the server issued at search time.
    The server re-fetches each offer behind it from Duffel and compares against
    its own record of what was shown - the client never asserts a price, a
    baggage state, or a booking status. The response leads with the
    server-verified current values and every change.

    A selection the server does not recognise, or one whose 20-minute window
    has passed, is a 404: the offers behind it are dead too.
    """
    selection = selection_store().get(body.selection_id)
    if selection is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": (
                    "That selection is unknown or has expired. Run the search "
                    "again and re-select the trip."
                ),
            },
        )

    duffel = _revalidation_duffel()
    tolerance = PriceTolerance(
        absolute=body.tolerance_absolute, percentage=body.tolerance_percentage
    )
    try:
        result = revalidate_selection(selection, duffel=duffel, tolerance=tolerance)
    except RevalidationLimitExceeded as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return RevalidateResponse(
        selection_id=result.selection_id,
        status=result.status,
        bookable=result.bookable,
        may_proceed=result.status.may_proceed_to_confirmation,
        message=REVALIDATION_MESSAGES[result.status],
        discovered_total=result.discovered_total,
        current_total=result.current_total,
        delta=result.total_delta,
        delta_pct=result.total_delta_pct,
        currency=result.currency,
        tolerance_absolute=tolerance.absolute,
        tolerance_percentage=tolerance.percentage,
        offers=[
            RevalidatedOfferDTO(
                offer_id=offer.offer_id,
                leg=offer.leg_label or f"{offer.origin} → {offer.destination}",
                status=offer.status,
                discovered_amount=offer.discovered_amount,
                current_amount=offer.current_amount,
                delta=offer.amount_delta,
                discovered_currency=offer.discovered_currency,
                current_currency=offer.current_currency,
                changes=[
                    OfferChangeDTO(
                        field=c.field, discovered=c.discovered, current=c.current,
                        severity=c.severity.value, detail=c.detail,
                    )
                    for c in offer.changes
                ],
                detail=offer.detail,
            )
            for offer in result.offers
        ],
        checked_at=result.checked_at,
    )


# ---------------------------------------------------------------------------
# Booking flow (V8 Phase 4) — one confirmation, several tickets, a test pass.
# The server owns every price, status and the journey reference.
# ---------------------------------------------------------------------------
_TERMINAL_PHASES = {
    BookingPhase.COMPLETE, BookingPhase.PARTIAL_FAILURE, BookingPhase.FAILED,
    BookingPhase.GUIDED_BOOKING,
}
_PHASE_MESSAGES = {
    BookingPhase.AWAITING_TRAVELERS: "Enter who is travelling.",
    BookingPhase.AWAITING_CONFIRMATION: "Review your journey and confirm once.",
    BookingPhase.REVALIDATING: "Checking your trip…",
    BookingPhase.RECONFIRM_REQUIRED: "Something changed — please confirm again.",
    BookingPhase.ISSUING: "Preparing your journey…",
    BookingPhase.COMPLETE: "Your test journey has been prepared.",
    BookingPhase.PARTIAL_FAILURE: "We couldn't complete your entire journey.",
    BookingPhase.FAILED: "This journey could not be prepared.",
    BookingPhase.GUIDED_BOOKING: "Your journey is ready — book each ticket with Detoura's guidance.",
}


def _booking_or_404(booking_id: str):
    run = booking_store().get(booking_id)
    if run is None:
        raise HTTPException(
            status_code=404,
            detail={"message": "That booking is unknown or has expired. Start again from the trip."},
        )
    return run


def _breakdown_dto(bd, *, bookable: float = 0.0, reconciled: bool = True) -> PriceBreakdownDTO:
    return PriceBreakdownDTO(
        currency=bd.currency,
        supplier_transport=bd.supplier_transport,
        supplier_baggage=bd.supplier_baggage,
        supplier_fees=bd.supplier_fees,
        supplier_total=bd.supplier_total,
        detoura_service_fee=bd.detoura_service_fee,
        detoura_markup=bd.detoura_markup,
        detoura_revenue_gross=bd.detoura_revenue_gross,
        discount=bd.discount,
        tax=bd.tax,
        customer_total=bd.customer_total,
        bookable_ticket_subtotal=bookable or bd.supplier_transport,
        reconciled=reconciled,
        explanation=list(bd.explanation),
    )


_TIER_CONTENT = {
    ServiceTier.BASIC: {
        "tagline": "Book with guidance",
        "summary": "You confirm each ticket yourself, in one organised place.",
        "flow": "self_service",
        "included": [
            "Optimised journey",
            "One organised itinerary",
            "Live fare checks",
            "Guided ticket-by-ticket booking",
        ],
        "not_included": [
            "Detoura books the tickets for you",
            "Booking monitoring",
            "Recovery assistance",
            "Unified travel pass",
        ],
    },
    ServiceTier.ALL_IN_ONE: {
        "tagline": "Detoura handles it",
        "summary": "One confirmation — Detoura books and manages the journey.",
        "flow": "managed",
        "included": [
            "Everything in Basic",
            "One confirmation",
            "Detoura books all tickets",
            "Booking monitoring",
            "Recovery assistance",
            "Unified travel pass",
        ],
        "not_included": [],
    },
}
_RECOMMENDED_TIER = ServiceTier.ALL_IN_ONE


def _tier_option_dto(
    service, *, supplier_total: float, currency: str, ticket_count: int,
    promo_code: str | None, user_key: str, selected_tier: ServiceTier | None,
) -> list[ServiceTierOptionDTO]:
    options: list[ServiceTierOptionDTO] = []
    for tier in (ServiceTier.BASIC, ServiceTier.ALL_IN_ONE):
        res = service.quote(
            supplier_transport=supplier_total, currency=currency,
            ticket_count=ticket_count, service_tier=tier,
            promo_code=promo_code, user_key=user_key,
        )
        b = res.quote.breakdown
        c = _TIER_CONTENT[tier]
        options.append(ServiceTierOptionDTO(
            tier=tier, label=tier.label, summary=c["summary"],
            tagline=c["tagline"], flow=c["flow"],
            detoura_fee=b.detoura_revenue_gross, customer_total=b.customer_total,
            selected=tier is selected_tier,
            recommended=tier is _RECOMMENDED_TIER,
            included=c["included"], not_included=c["not_included"],
        ))
    return options


def _commercial_dto(run) -> CommercialSummaryDTO | None:
    """The transparent price for ``run``, plus each service tier priced with
    the current promo so the customer compares real numbers."""
    if run.quote is None:
        return None
    from ..services.commercial import CommercialPricingService

    db = get_db()
    from ..services.booking_commercial import _supplier_transport, reconcile_run_price

    supplier = _supplier_transport(run)
    service = CommercialPricingService(db)
    options = _tier_option_dto(
        service, supplier_total=supplier, currency=run.currency,
        ticket_count=len(run.items), promo_code=run.requested_promo,
        user_key=run.user_key, selected_tier=run.service_tier,
    )

    q = run.quote
    promo_msg = ""
    promo_ok = bool(q.promo_code)
    if run.requested_promo and not q.promo_code:
        # requested but not applied - say why, from a fresh evaluation
        res = service.quote(
            supplier_transport=supplier, currency=run.currency,
            ticket_count=len(run.items), service_tier=run.service_tier,
            promo_code=run.requested_promo, user_key=run.user_key,
        )
        promo_msg = res.promo.reason if res.promo else "code could not be applied"
    elif q.promo_code:
        promo_msg = f"{q.promo_code} applied: -{q.promo_discount:.2f} {run.currency}"

    rec = reconcile_run_price(run)
    return CommercialSummaryDTO(
        service_tier=run.service_tier,
        service_tier_label=run.service_tier.label,
        breakdown=_breakdown_dto(
            q.breakdown, bookable=rec.bookable_ticket_subtotal, reconciled=rec.ok
        ),
        markup_policy=str(q.markup_policy),
        promo_code=q.promo_code or (run.requested_promo or None),
        promo_accepted=promo_ok,
        promo_message=promo_msg,
        tier_options=options,
        test_mode=True,
    )


def _itinerary_dto(run) -> SelfServiceItineraryDTO:
    from ..services.guided_booking import guidance, progress

    _finalize_if_terminal(run)
    persist_run(run, get_db())
    booked, total = progress(run)
    tickets = [
        SelfServiceTicketDTO(
            sequence=idx,
            origin_city=i.origin_city, origin_airport=i.origin_airport,
            destination_city=i.destination_city,
            destination_airport=i.destination_airport,
            departure=i.departure, arrival=i.arrival,
            carrier=i.carrier, flight_number=i.flight_number,
            fare=i.quoted_price, rechecked_fare=i.current_price, currency=i.currency,
            cabin_baggage=i.cabin_baggage, checked_baggage=i.checked_baggage,
            available=i.state is not BookingState.UNAVAILABLE,
            guided_state=(i.guided_state.value if i.guided_state else "READY_TO_BOOK"),
            reported_by=i.guided_reported_by or "detoura",
            detoura_verified=False,
            note=i.detail,
            booking_guidance=guidance(i),
        )
        for idx, i in enumerate(run.items, start=1)
    ]
    dates = sorted({
        i.departure.date().isoformat() for i in run.items if i.departure
    })
    return SelfServiceItineraryDTO(
        journey_reference=run.journey_reference,
        booking_id=run.booking_id,
        trip_label=run.trip_label,
        route_cities=list(run.route_cities),
        travel_dates=dates,
        party_size=run.party.size if run.party else 1,
        traveller_name=(run.party.lead.full_name if run.party else ""),
        traveller_details_saved=run.party is not None,
        tickets=tickets,
        booked_count=booked,
        ticket_count=total,
        fares_rechecked=True,
        recheck_note=run.reconfirm_note,
        commercial=_commercial_dto(run),
        test_mode=True,
        generated_at=datetime.now(timezone.utc),
    )


def _finalize_if_terminal(run) -> None:
    if run.phase in _TERMINAL_PHASES and not run.economics_written:
        try:
            finalize_economics(run, get_db())
        except Exception:  # a ledger write must never break a poll
            pass


def _intent_dto(run) -> BookingIntentResponse:
    _finalize_if_terminal(run)
    persist_run(run, get_db())
    from ..services.booking_commercial import reconcile_run_price

    rec = reconcile_run_price(run)
    requested = max((i.travelers for i in run.items), default=1)
    return BookingIntentResponse(
        booking_id=run.booking_id,
        journey_reference=run.journey_reference,
        mode=run.mode,
        phase=run.phase,
        trip_label=run.trip_label,
        route_cities=list(run.route_cities),
        currency=run.currency,
        discovered_total=run.discovered_total,
        current_total=run.current_total,
        trip_estimate=dict(run.trip_estimate or {}),
        price_reconciled=rec.ok,
        price_issue=rec.reason,
        reconfirm_note=run.reconfirm_note,
        party_size=run.party.size if run.party else requested,
        requested_travelers=requested,
        travelers_submitted=run.party is not None,
        service_flow=(
            "self_service" if run.service_tier is ServiceTier.BASIC else "managed"
        ),
        commercial=_commercial_dto(run),
        items=[
            BookingItemStateDTO(
                sequence=idx,
                origin_city=i.origin_city, origin_airport=i.origin_airport,
                destination_city=i.destination_city, destination_airport=i.destination_airport,
                departure=i.departure, arrival=i.arrival,
                carrier=i.carrier, flight_number=i.flight_number,
                cabin_baggage=i.cabin_baggage, checked_baggage=i.checked_baggage,
                price_per_person=i.quoted_price, current_price=i.current_price,
                currency=i.currency, state=i.state, detail=i.detail,
                provider_order_id=i.provider_order_id,
            )
            for idx, i in enumerate(run.items, start=1)
        ],
        pass_available=run.phase in (
            BookingPhase.COMPLETE, BookingPhase.PARTIAL_FAILURE, BookingPhase.FAILED,
        ),
        itinerary_available=run.phase is BookingPhase.GUIDED_BOOKING,
    )


@router.post("/events")
def track_events(body: TrackEventsRequest) -> dict:
    """Anonymous product-funnel events from the web client. No auth (it is the
    public site) and no PII - the store keeps only whitelisted event names and
    prop keys and drops anything that looks like personal data."""
    kept = analytics_store.record_many(
        get_db(),
        [{"event": e.event, "tier": e.tier, "props": e.props} for e in body.events],
        session_key=body.session_key,
        visitor_key=body.visitor_key,
    )
    return {"kept": kept}


@router.post("/commercial/preview", response_model=CommercialPreviewResponse)
def commercial_preview(body: CommercialPreviewRequest) -> CommercialPreviewResponse:
    """Price both service tiers for a trip without starting a booking. Used on
    the results and trip-detail screens so the customer sees real numbers, and
    no unavoidable fee ever appears only at final confirmation."""
    from ..services.commercial import CommercialPricingService

    service = CommercialPricingService(get_db())
    options = _tier_option_dto(
        service, supplier_total=body.supplier_total, currency=body.currency.upper(),
        ticket_count=body.ticket_count, promo_code=body.promo_code,
        user_key="anonymous", selected_tier=None,
    )
    return CommercialPreviewResponse(
        currency=body.currency.upper(), supplier_total=body.supplier_total,
        tiers=options, test_mode=True,
    )


@router.post("/booking-intents", response_model=BookingIntentResponse, status_code=201)
def create_booking_intent(body: CreateBookingIntentRequest) -> BookingIntentResponse:
    """Begin a booking. From a server-issued `selection_id` (real Duffel offers,
    `SANDBOX_BOOKED`) or from a synthetic trip's legs (`DEMO_ONLY` — no Duffel
    Order is ever created and the pass says so)."""
    if body.selection_id:
        selection = selection_store().get(body.selection_id)
        if selection is None:
            raise HTTPException(status_code=404, detail={
                "message": "That selection is unknown or has expired. Search again.",
            })
        run = create_run_from_selection(selection)
    elif body.demo_legs:
        est = body.demo_trip_estimate
        run = create_run_demo(
            trip_label=body.demo_trip_label or "Detoura journey",
            currency=body.demo_currency,
            trip_estimate=(
                {
                    "total": est.total, "transport": est.transport,
                    "accommodation": est.accommodation, "transfer": est.transfer,
                }
                if est is not None
                else {}
            ),
            legs=[
                {
                    "origin": leg.origin, "destination": leg.destination,
                    "departure": leg.departure, "arrival": leg.arrival,
                    "carrier": leg.carrier, "flight_number": leg.flight_number,
                    "price_per_person": leg.price_per_person,
                    "travelers": body.demo_travelers,
                    "cabin": leg.cabin, "checked": leg.checked,
                }
                for leg in body.demo_legs
            ],
        )
    else:
        raise HTTPException(status_code=422, detail="provide selection_id or demo_legs")

    # Price it now, so the review screen shows the transparent breakdown from
    # the first render. The tier is the customer's choice; the server owns
    # every amount.
    try:
        price_run(
            run, get_db(),
            service_tier=body.service_tier,
            promo_code=body.promo_code,
        )
    except Exception:  # pricing must not block starting a booking
        pass
    booking_store().put(run)
    return _intent_dto(run)


@router.post(
    "/booking-intents/{booking_id}/commercial",
    response_model=BookingIntentResponse,
)
def set_commercial_options(
    booking_id: str, body: SetCommercialOptionsRequest
) -> BookingIntentResponse:
    """Change the service tier and/or promo code before confirmation, and get
    the re-priced journey back. Rejected after the journey is confirmed."""
    run = _booking_or_404(booking_id)
    if run.phase not in (
        BookingPhase.AWAITING_TRAVELERS,
        BookingPhase.AWAITING_CONFIRMATION,
    ):
        raise HTTPException(status_code=409, detail={
            "message": "This journey is already being booked; the price is locked.",
        })
    promo = None if body.clear_promo else body.promo_code
    if body.clear_promo:
        run.requested_promo = None
    price_run(run, get_db(), service_tier=body.service_tier, promo_code=promo)
    return _intent_dto(run)


_TITLES = {t.value: t for t in TravelerTitle}
_GENDERS = {g.value: g for g in TravelerGender}


@router.post("/booking-intents/{booking_id}/travelers", response_model=BookingIntentResponse)
def submit_travelers(booking_id: str, body: SubmitTravelersRequest) -> BookingIntentResponse:
    """The traveller enters their details once; they are reused for every leg.

    Validated here. Never logged, never echoed into a URL, never stored beyond
    this booking's in-memory run.
    """
    run = _booking_or_404(booking_id)
    if run.phase not in (BookingPhase.AWAITING_TRAVELERS, BookingPhase.AWAITING_CONFIRMATION):
        raise HTTPException(status_code=409, detail={"message": "This booking is past the traveller step."})

    needed = max((i.travelers for i in run.items), default=1)
    if len(body.travelers) != needed:
        raise HTTPException(status_code=422, detail={
            "message": (
                f"This journey is for {needed} traveller"
                f"{'s' if needed != 1 else ''}; {len(body.travelers)} "
                f"provided. Enter each traveller once."
            ),
            "requested_travelers": needed,
            "provided": len(body.travelers),
        })
    try:
        travelers = tuple(
            Traveler(
                given_name=t.given_name, family_name=t.family_name,
                born_on=t.born_on, email=t.email, phone=t.phone,
                title=_TITLES.get((t.title or "").lower()),
                gender=_GENDERS.get((t.gender or "").lower()),
                nationality=t.nationality,
                passport_number=t.passport_number,
                passport_issuing_country=t.passport_issuing_country,
                passport_expiry=t.passport_expiry,
                document_type=t.document_type,
            )
            for t in body.travelers
        )
        # No two passengers may share a document number - never one identity
        # copied across the party.
        docs = [d.passport_number for d in travelers if d.passport_number]
        if len(docs) != len(set(docs)):
            raise ValueError("two travellers cannot share a document number")
        party = TravelerParty(travelers=travelers)
        # Assert the authoritative count: the party must not be short a
        # passenger, and a missing one is never manufactured by copying another.
        if party.size != needed:
            raise ValueError(
                f"this journey needs {needed} distinct travellers, got {party.size}"
            )
        attach_travelers(run, party)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail={"message": str(error)}) from error
    return _intent_dto(run)


@router.post("/booking-intents/{booking_id}/confirm", response_model=BookingIntentResponse)
def confirm_booking(booking_id: str, body: ConfirmBookingRequest) -> BookingIntentResponse:
    """The single journey confirmation.

    * All-in-One: kicks off revalidation then Detoura's per-leg issuance. Poll
      `GET /booking-intents/{id}` for progress, then fetch the travel pass.
    * Basic: re-checks the fares and opens the guided booking workflow. No
      orchestration, no Duffel Order. Fetch `.../itinerary` for the tickets.
    """
    run = _booking_or_404(booking_id)
    run.tolerance = PriceTolerance(
        absolute=body.tolerance_absolute, percentage=body.tolerance_percentage
    )
    # Re-price at the moment of confirmation: a promo may have lapsed or been
    # fully redeemed since the review screen was rendered.
    try:
        price_run(run, get_db())
    except Exception:
        pass

    # Price provenance: the priced supplier transport must reconcile with the
    # bookable ticket fares. If it does not, refuse - never charge against a
    # number that cannot be explained.
    from ..services.booking_commercial import reconcile_run_price

    rec = reconcile_run_price(run)
    if not rec.ok:
        with run._lock:
            run.phase = BookingPhase.PRICE_INCONSISTENT
        raise HTTPException(status_code=409, detail={
            "message": (
                "We can't confirm this journey: the ticket prices don't add up "
                "to what we were about to charge. Nothing has been booked."
            ),
            "phase": BookingPhase.PRICE_INCONSISTENT.value,
            "reason": rec.reason,
            "bookable_ticket_subtotal": rec.bookable_ticket_subtotal,
            "priced_supplier_transport": rec.priced_supplier_transport,
        })

    try:
        if run.service_tier is ServiceTier.BASIC:
            from ..services.guided_booking import prepare_journey

            prepare_journey(run)
        else:
            start_confirmation(run)
    except ValueError as error:
        raise HTTPException(status_code=409, detail={"message": str(error)}) from error
    return _intent_dto(run)


@router.post(
    "/booking-intents/{booking_id}/tickets/{sequence}/start-booking",
    response_model=SelfServiceItineraryDTO,
)
def guided_start_ticket(booking_id: str, sequence: int) -> SelfServiceItineraryDTO:
    """The traveller is going to book this ticket externally now."""
    run = _booking_or_404(booking_id)
    from ..services.guided_booking import start_ticket

    try:
        start_ticket(run, sequence)
    except KeyError:
        raise HTTPException(status_code=404, detail={"message": "No such ticket."})
    except ValueError as error:
        raise HTTPException(status_code=409, detail={"message": str(error)})
    persist_run(run, get_db())
    return _itinerary_dto(run)


@router.post(
    "/booking-intents/{booking_id}/tickets/{sequence}/mark",
    response_model=SelfServiceItineraryDTO,
)
def guided_mark_ticket(
    booking_id: str, sequence: int, body: GuidedMarkRequest
) -> SelfServiceItineraryDTO:
    """The traveller reports where this ticket stands. Recorded as
    traveller-reported; Detoura never upgrades it to verified."""
    run = _booking_or_404(booking_id)
    from ..models.booking import GuidedBookingState
    from ..services.guided_booking import mark_ticket

    try:
        mark_ticket(run, sequence, GuidedBookingState(body.state),
                    reference=body.reference)
    except KeyError:
        raise HTTPException(status_code=404, detail={"message": "No such ticket."})
    except ValueError as error:
        raise HTTPException(status_code=409, detail={"message": str(error)})
    persist_run(run, get_db())
    return _itinerary_dto(run)


@router.get(
    "/booking-intents/{booking_id}/itinerary",
    response_model=SelfServiceItineraryDTO,
)
def get_itinerary(booking_id: str) -> SelfServiceItineraryDTO:
    run = _booking_or_404(booking_id)
    if run.phase is not BookingPhase.GUIDED_BOOKING:
        raise HTTPException(status_code=409, detail={
            "message": "This journey has no guided itinerary.",
            "phase": run.phase.value,
        })
    _finalize_if_terminal(run)
    return _itinerary_dto(run)


@router.get("/booking-intents/{booking_id}", response_model=BookingIntentResponse)
def get_booking_intent(booking_id: str) -> BookingIntentResponse:
    return _intent_dto(_booking_or_404(booking_id))


_PASS_HEADLINES = {
    PassStatus.READY: "Your journey is ready",
    PassStatus.RECOVERY_REQUIRED: "Your journey needs attention",
    PassStatus.FAILED: "This journey could not be prepared",
}
_MODE_NOTES = {
    PassMode.SANDBOX_BOOKED: "Duffel Test Mode Orders were created. Not real tickets.",
    PassMode.DEMO_ONLY: "DEMO ONLY — no Duffel Order was created. No payment collected.",
}


@router.get("/booking-intents/{booking_id}/travel-pass", response_model=TravelPassResponse)
def get_travel_pass(booking_id: str) -> TravelPassResponse:
    """The Detoura Test Travel Pass — server-generated from the actual journey.

    Available only once the run reached a terminal phase. A journey with any
    failed required leg produces a `recovery_required` pass, never a success.
    """
    run = _booking_or_404(booking_id)
    if run.phase not in _TERMINAL_PHASES:
        raise HTTPException(status_code=409, detail={
            "message": _PHASE_MESSAGES.get(run.phase, "The booking is still in progress."),
            "phase": run.phase.value,
        })
    _finalize_if_terminal(run)
    tp = build_travel_pass(run)
    return TravelPassResponse(
        journey_reference=tp.journey_reference,
        booking_id=tp.booking_id,
        mode=tp.mode,
        status=tp.status,
        traveler_name=tp.traveler_name,
        party_size=tp.party_size,
        route_cities=list(tp.route_cities),
        travel_dates=list(tp.travel_dates),
        tickets=[
            TravelPassTicketDTO(
                sequence=t.sequence,
                origin_city=t.origin_city, origin_airport=t.origin_airport,
                destination_city=t.destination_city, destination_airport=t.destination_airport,
                departure=t.departure, arrival=t.arrival,
                carrier=t.carrier, flight_number=t.flight_number,
                cabin_baggage=t.cabin_baggage, checked_baggage=t.checked_baggage,
                price_per_person=t.price_per_person, currency=t.currency,
                booking_state=t.booking_state, confirmed=t.confirmed,
                provider_order_id=t.provider_order_id,
            )
            for t in tp.tickets
        ],
        tickets_prepared=tp.tickets_prepared,
        trip_total=tp.trip_total,
        currency=tp.currency,
        baggage_complete=tp.baggage_complete,
        unknowns=list(tp.unknowns),
        provider_order_ids=list(tp.provider_order_ids),
        disclaimer=tp.disclaimer.model_dump(),
        headline=_PASS_HEADLINES[tp.status],
        mode_note=_MODE_NOTES[tp.mode],
        generated_at=tp.generated_at,
    )


def _session_profile_dto(weights) -> SessionProfileDTO:
    return SessionProfileDTO(**weights.as_dict())


@router.post("/feedback/{trip_id}", response_model=TripFeedbackResponse)
def feedback(trip_id: str, body: TripFeedbackRequest) -> TripFeedbackResponse:
    """React, in real time, to one explicit signal about one trip (V6).

    Stateless like ``/trips/recheck``: nothing about ``trip_id`` is looked up
    or stored, it is only echoed back, because Detoura keeps no trip database.
    What actually changes is session state - see
    :mod:`detoura.services.feedback` for the declared/observed split this
    endpoint exists to maintain, and why the heuristic here is a fast,
    session-scoped complement to :mod:`detoura.learning`'s batch fit rather
    than a replacement for it.

    A missing or empty ``session_id`` is not an error: it gets a fresh,
    randomly-identified session, because refusing a first-time visitor would
    defeat the point of a personalization endpoint.
    """
    session_id = body.session_id or f"anon-{uuid.uuid4().hex}"
    try:
        session = record_feedback(
            session_id,
            body.action,
            body.value_breakdown.as_dict(),
            declared_profile=body.declared_profile,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return TripFeedbackResponse(
        session_id=session.session_id,
        trip_id=trip_id,
        declared_profile=session.declared_profile,
        declared=_session_profile_dto(session.declared),
        observed=_session_profile_dto(session.observed),
        confidence=session.confidence,
        signal_count=session.signal_count,
        explanation=session.explanation(),
    )


@router.post("/budget-sensitivity")
def budget_sensitivity(
    body: TripSearchRequest,
    steps: int = Query(default=6, ge=2, le=10),
    planner: TravelPlanner = Depends(get_planner),
) -> dict:
    """What would another fifty euros unlock? (Part 16)"""
    request = to_trip_request(body)
    try:
        analysis = analyze_budget_sensitivity(
            planner, request, steps=steps, profile=body.profile
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    thresholds = {step.budget for step in analysis.thresholds}
    return {
        "currency": analysis.currency,
        "profile": analysis.profile.value,
        "minimum_feasible_budget": analysis.minimum_feasible_budget,
        "steps": [
            {
                "budget": step.budget,
                "feasible": step.feasible,
                "trips_found": len(step.city_sets),
                "best_price": step.best_cost if step.feasible else None,
                "best_route": step.best_route if step.feasible else None,
                "unlocks": [sorted(cities) for cities in step.unlocked],
                "is_threshold": step.budget in thresholds,
            }
            for step in analysis.steps
        ],
    }


@router.get("/origins/{query}")
def origins(query: str, planner: TravelPlanner = Depends(get_planner)) -> dict:
    """Departure airports for a place, with what it costs to reach each.

    The Discover screen shows these under the origin field, so the traveler
    learns early that Detoura counts the ride to the airport - which is one of
    the things that makes its answers differ from a flight search.
    """
    try:
        candidates = planner.origin_resolver.resolve(query, planner.config)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    airports = []
    for candidate in candidates:
        options = planner.ground_transfer.search(query, candidate.code)
        cheapest = min(options, key=lambda o: o.price_per_person, default=None)
        airports.append(
            {
                "code": candidate.code,
                "name": candidate.name,
                "city": candidate.city,
                "distance_km": candidate.distance_km,
                "transfer_price": cheapest.price_per_person if cheapest else None,
                "transfer_minutes": cheapest.duration_minutes if cheapest else None,
            }
        )
    return {"origin": query, "airports": airports}


@router.get("/destinations")
def destinations(planner: TravelPlanner = Depends(get_planner)) -> list[dict]:
    """The catalog, described in interests rather than raw attributes (V5.7)."""
    from ..models.destination import EXPERIENCE_ATTRIBUTES

    catalog = []
    for destination in planner.destinations.all():
        scores = {name: getattr(destination, name) for name in EXPERIENCE_ATTRIBUTES}
        strengths = sorted(
            (name for name, value in scores.items() if value >= 0.75),
            key=lambda name: -scores[name],
        )[:4]
        catalog.append(
            {
                "id": destination.id,
                "name": destination.name,
                "country": destination.country,
                "recommended_min_days": destination.recommended_min_days,
                "recommended_max_days": destination.recommended_max_days,
                "strengths": strengths,
            }
        )
    return catalog


@router.get("/profiles")
def profiles() -> list[dict]:
    """The three ways of answering "what is the best trip?"."""
    labels = {
        ProfileName.CHEAPEST: ("Cheapest", "Spend as little as the trip allows."),
        ProfileName.BEST_VALUE: (
            "Best value",
            "The best trip your money and time can buy.",
        ),
        ProfileName.ADVENTURE: ("Adventure", "See more places, without the slog."),
    }
    return [
        {
            "name": name.value,
            "label": labels[name][0],
            "description": labels[name][1],
        }
        for name in PROFILES
    ]


@router.get("/search-modes")
def search_modes() -> list[dict]:
    """How hard Detoura can look, and roughly what each costs in seconds."""
    return [
        {
            "name": mode.value,
            "label": mode.label,
            "description": mode.blurb,
            "estimated_seconds": list(mode.estimated_seconds),
            "is_default": mode is SearchMode.SMART,
        }
        for mode in MODE_SETTINGS
    ]




def _selected_itinerary(trip, currency: str) -> Itinerary:
    """Rebuild the client's trip as an :class:`Itinerary`.

    Only what preservation and the change diff measure is reconstructed. The
    legs carry no provider id - ids are internal and never reach a client - so
    similarity matches them on (origin, destination, departure), the same
    natural key `recheck` uses to re-find a saved leg.
    """
    legs = [
        TransportOption(
            id=f"saved-{index}",
            origin=leg.origin,
            destination=leg.destination,
            departure=leg.departure,
            # Placeholder: the wire format carries no per-leg duration, so a
            # saved leg has no honest arrival time. Nothing downstream reads
            # it - transit comes from the DTO's own intercity/transfer minutes,
            # never from these timestamps - but anything that later computes
            # from `leg.arrival` must not trust this value.
            arrival=leg.departure,
            price_per_person=leg.price_per_person,
            transport_type=TransportType.FLIGHT,
            duration_minutes=0,
            operator=leg.operator or "saved",
        )
        for index, leg in enumerate(trip.legs)
    ]
    return Itinerary(
        rank=0,
        score=0.0,
        total_cost=trip.total_price,
        currency=currency,
        duration_days=trip.duration_days,
        origin_airport=trip.origin_airport,
        return_airport=trip.return_airport,
        cities=list(trip.cities),
        legs=legs,
        total_travel_minutes=trip.intercity_minutes,
        ground_transfer_minutes=trip.transfer_minutes,
        usable_destination_minutes=trip.usable_minutes,
        departure=trip.departure,
        arrival=trip.arrival,
        stays=[
            StaySummary(
                city=stay.city,
                arrival=stay.arrival,
                departure=stay.departure,
                nights=max((stay.departure.date() - stay.arrival.date()).days, 0),
                accommodation_cost=stay.cost,
                accommodation_name=stay.name,
            )
            for stay in trip.stays
        ],
        value_breakdown=TravelValueBreakdown(
            profile=ProfileName.BEST_VALUE,
            cost=0.0,
            experience=trip.experience_score,
            preferences=trip.preference_match,
            time=0.0,
            diversity=0.0,
            accommodation=trip.accommodation_score,
            total=0.0,
        ),
    )


def _moded_planner(planner: TravelPlanner, mode: SearchMode) -> TravelPlanner:
    """A view of the shared planner configured for one search mode.

    A shallow copy, so the providers - and therefore the warm caches - stay the
    shared instance's and only the config differs. Constructing a fresh
    TravelPlanner here would discard exactly the cache an edit benefits from
    most, since it is re-searching a space the original search just paid to
    fetch.
    """
    scoped = copy.copy(planner)
    scoped.config = apply_mode(planner.config, mode)
    return scoped


@router.post("/trips/reoptimize", response_model=TripReoptimizeResponse)
def reoptimize_trip(
    body: TripReoptimizeRequest, planner: TravelPlanner = Depends(get_planner)
) -> TripReoptimizeResponse:
    """Edit a chosen trip: keep what the traveler liked, re-optimize the rest.

    Stateless like every other endpoint here - the trip travels with the
    request, because Detoura stores nothing server-side.

    Three outcomes, deliberately distinguishable. A contradictory edit, or one
    that cannot be expressed as a valid trip, is a **422**: the traveler asked
    for something that cannot exist. A coherent edit that nothing satisfied
    returns **200 with a null trip** and a warning: the question was fair, the
    answer is empty. Conflating the two would tell somebody their request was
    malformed when it was merely unlucky.
    """
    request = to_trip_request(body.search)
    mode = body.search.search_mode
    original = _selected_itinerary(body.trip, request.currency)
    failures = FailureLog()

    try:
        result = reoptimize(
            _moded_planner(planner, mode),
            request,
            original,
            body.patch,
            failures=failures,
            profile=body.search.profile,
        )
    except EditConflict as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    trip_dto = None
    alternatives: list[TripRecommendation] = []
    if result.trip is not None:
        # `recommendation_dto` reads `result.baseline` for the your-idea
        # comparison. An edit has no such baseline - the thing being compared
        # against is the previous trip, and that comparison is the diff - so it
        # is passed as None and the recommendation simply carries no
        # `comparison`, rather than one built against the wrong reference.
        plan_like = PlanResult(
            profile=result.trip.profile or body.search.profile,
            baseline=None,
            recommendations=[result.trip, *result.alternatives],
            metadata=PlannerMetadata(origin=request.origin),
        )
        quality = SearchQuality(
            rounds=1,
            winner_stable=True,
            frontier_size=result.considered,
            completed=result.considered,
            alternatives_returned=1 + len(result.alternatives),
            degraded=failures.degraded,
            deep=mode is SearchMode.DEEP,
        )
        moment = datetime.now()
        trip_dto = recommendation_dto(
            result.trip, plan_like, quality, travelers=request.travelers, now=moment
        )
        alternatives = [
            recommendation_dto(
                alternative, plan_like, quality,
                travelers=request.travelers, now=moment,
            )
            for alternative in result.alternatives
        ]

    return TripReoptimizeResponse(
        trip_id=body.trip_id,
        trip=trip_dto,
        alternatives=alternatives,
        similarity=(
            SimilarityDTO(**result.similarity.as_dict())
            if result.similarity is not None
            else None
        ),
        diff=(
            ChangeDiffDTO.model_validate(result.diff.model_dump())
            if result.diff is not None
            else None
        ),
        locked=list(result.derived.locked),
        excluded=list(result.derived.excluded),
        unsupported_operations=list(result.derived.unsupported),
        considered=result.considered,
        issues=[
            ProviderIssueDTO(
                kind=str(entry["kind"]),
                provider=str(entry["provider"]),
                message=str(entry["message"]),
                retryable=bool(entry["retryable"]),
                occurrences=int(entry["occurrences"]),
            )
            for entry in failures.summary()
        ],
        warnings=list(result.warnings),
    )


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "product": "Detoura", "data_source": "synthetic"}
