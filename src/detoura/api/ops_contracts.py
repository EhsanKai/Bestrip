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
