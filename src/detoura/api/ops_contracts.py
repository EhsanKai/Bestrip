"""DTOs for the Detoura Ops console (V8.5 Phase B).

Kept separate from the product contract (``contracts.py``): ops shows more
(Duffel order ids, internal states, the economics ledger) and is admin-only.
Traveller PII beyond lead name + email never appears in these shapes.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OpsLoginRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    token: str = Field(min_length=1, max_length=200)


class OpsSessionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    session_token: str
    expires_in: float
    ops_actor: str = "ops"


class OpsActionDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: str
    enabled: bool
    reason: str = ""


class OpsBookingItemDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    sequence: int
    origin_city: str
    origin_airport: str
    destination_city: str
    destination_airport: str
    departure: datetime | None = None
    arrival: datetime | None = None
    carrier: str = ""
    flight_number: str = ""
    offer_id: str = ""
    provider: str = ""
    duffel_order_id: str | None = None
    quoted_price: float = 0.0
    current_price: float | None = None
    booked_price: float | None = None
    currency: str = "EUR"
    cabin_baggage: str = "unknown"
    checked_baggage: str = "unknown"
    required: bool = True
    state: str
    detail: str = ""
    actions: list[OpsActionDTO] = Field(default_factory=list)


class OpsBookingSummaryDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    booking_id: str
    session_ref: str
    journey_reference: str
    created_at: datetime
    updated_at: datetime
    mode: str
    phase: str
    phase_label: str
    trip_label: str
    route_cities: list[str]
    party_size: int
    lead_name: str
    lead_email: str
    currency: str
    service_tier: str
    discovered_total: float
    current_total: float | None = None
    customer_total: float | None = None
    recovery_state: str = ""
    ticket_count: int = 0
    confirmed_count: int = 0


class OpsEconomicsDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    currency: str
    supplier_cost: float
    detoura_service_fee: float
    detoura_markup: float
    discount: float
    customer_price: float
    detoura_gross_revenue: float
    markup_policy: str
    promo_code: str | None = None
    provider_cost_estimate: float | None = None
    payment_cost: float | None = None
    refund: float | None = None
    recovery_cost: float | None = None
    contribution_margin: float | None = None
    has_unknown_costs: bool = True


class OpsBookingDetailDTO(OpsBookingSummaryDTO):
    reconfirm_note: str = ""
    items: list[OpsBookingItemDTO] = Field(default_factory=list)
    economics: OpsEconomicsDTO | None = None
    audit: list["OpsAuditEventDTO"] = Field(default_factory=list)


class OpsAuditEventDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: int
    ts: datetime
    actor: str
    action: str
    target_type: str = ""
    target_id: str = ""
    before: dict | None = None
    after: dict | None = None
    note: str = ""


class OpsBookingsPage(BaseModel):
    model_config = ConfigDict(frozen=True)
    bookings: list[OpsBookingSummaryDTO]
    counts_by_phase: dict[str, int] = Field(default_factory=dict)
    recovery_count: int = 0


class OpsRecoveryPage(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: list[OpsBookingSummaryDTO]
    by_state: dict[str, int] = Field(default_factory=dict)


class OpsOverviewDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    test_mode: bool = True
    total_bookings: int = 0
    counts_by_phase: dict[str, int] = Field(default_factory=dict)
    recovery_count: int = 0
    audit_events: int = 0


OpsBookingDetailDTO.model_rebuild()


# --- V8.5 Phase C2: commercial + promo management, finance, analytics ------

class MarkupPolicyConfigDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    basic_percentage: float
    basic_fixed_fee: float
    all_in_one_percentage: float
    all_in_one_fixed_fee: float
    max_percentage: float
    max_fixed_fee: float
    min_total_fee: float
    max_total_fee: float


class MarkupPolicyDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    policy_id: str
    version: int
    label: str
    active: bool
    created_at: str = ""
    config: MarkupPolicyConfigDTO | None = None
    bookings_priced: int = 0


class CreateMarkupPolicyRequest(BaseModel):
    """The small set of numbers an operator tunes. The server builds the full
    policy and assigns the next version; no computed fee is accepted here."""

    model_config = ConfigDict(frozen=True)
    label: str = Field(default="", max_length=120)
    basic_percentage: float = Field(ge=0.0, le=1.0)
    basic_fixed_fee: float = Field(ge=0.0, le=1_000.0)
    all_in_one_percentage: float = Field(ge=0.0, le=1.0)
    all_in_one_fixed_fee: float = Field(ge=0.0, le=1_000.0)
    max_percentage: float = Field(default=0.15, ge=0.0, le=1.0)
    max_fixed_fee: float = Field(default=25.0, ge=0.0, le=1_000.0)
    min_total_fee: float = Field(default=0.0, ge=0.0, le=1_000.0)
    max_total_fee: float = Field(default=120.0, ge=0.0, le=5_000.0)
    activate: bool = True


class MarkupPreviewRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    supplier_total: float = Field(gt=0.0, le=1_000_000.0)
    ticket_count: int = Field(default=3, ge=1, le=12)
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    # preview a stored version, or an unsaved draft
    policy_id: str | None = None
    version: int | None = None
    draft: CreateMarkupPolicyRequest | None = None


class MarkupPreviewLineDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    tier: str
    supplier_total: float
    detoura_service_fee: float
    detoura_markup: float
    detoura_fee_total: float
    customer_total: float
    bounded: bool
    explanation: list[str]


class MarkupPreviewDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    policy_ref: str
    invariant_ok: bool
    lines: list[MarkupPreviewLineDTO]


class OpsPromoDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str
    label: str = ""
    enabled: bool
    kind: str
    value: float
    currency: str
    target: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    global_limit: int | None = None
    per_user_limit: int | None = None
    min_order_value: float = 0.0
    max_discount: float | None = None
    eligible_tiers: list[str] = Field(default_factory=list)
    # stats
    redemptions: int = 0
    discount_total: float = 0.0
    revenue_impact: float = 0.0
    bookings_with_code: int = 0


class UpsertPromoRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str = Field(min_length=3, max_length=40)
    label: str = Field(default="", max_length=120)
    enabled: bool = True
    kind: str = Field(default="PERCENTAGE", pattern="^(PERCENTAGE|FIXED)$")
    value: float = Field(gt=0.0, le=100_000.0)
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    target: str = Field(default="DETOURA_FEE", pattern="^(DETOURA_FEE|ORDER_TOTAL)$")
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    global_limit: int | None = Field(default=None, ge=1)
    per_user_limit: int | None = Field(default=None, ge=1)
    min_order_value: float = Field(default=0.0, ge=0.0)
    max_discount: float | None = Field(default=None, gt=0.0)
    eligible_tiers: list[str] = Field(default_factory=list)


class OpsPromoRedemptionDTO(BaseModel):
    model_config = ConfigDict(frozen=True)
    booking_id: str
    discount_amount: float
    currency: str
    redeemed_at: datetime


class OpsPromoDetailDTO(OpsPromoDTO):
    redemption_log: list[OpsPromoRedemptionDTO] = Field(default_factory=list)
