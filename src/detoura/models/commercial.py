"""Detoura's commercial layer (V8.5).

The provider fare and Detoura's own revenue must never be conflated. A search
result shows a supplier price; the customer pays that supplier price **plus** an
explicit, disclosed Detoura component (a service fee and/or a markup), minus any
promotion. This module is the vocabulary for that split.

Three rules hold everywhere:

* The supplier price is never mutated to hide Detoura's margin. It appears in
  the breakdown exactly as the provider quoted it.
* Every amount is >= 0 on input; the only thing that subtracts is ``discount``,
  and it can never drive ``customer_total`` below zero.
* A booking keeps the *breakdown it was priced with*. Historical economics are
  never recomputed from today's markup rules - see
  :mod:`detoura.persistence.economics`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .money import round_half_up


class ServiceTier(str, Enum):
    """What the customer is buying from Detoura, over and above the tickets.

    ``BASIC`` is the default and the cheaper option: the traveller receives the
    itinerary and handles more of the process themselves. ``ALL_IN_ONE`` is an
    explicit, opt-in upgrade - Detoura orchestrates the multi-ticket booking,
    revalidation, one traveller-data flow, issuance, monitoring and recovery
    support.

    This is a Detoura *service product*. It is **not** an EU legal "package
    holiday" - that classification has not been established and the UI/API must
    not claim it.
    """

    BASIC = "BASIC"
    ALL_IN_ONE = "ALL_IN_ONE"

    @property
    def label(self) -> str:
        return {"BASIC": "Basic", "ALL_IN_ONE": "All-in-One"}[self.value]


class PricingPolicyRef(BaseModel):
    """Identifies the exact policy version a quote was produced with.

    Stored alongside every booking so "why was this the price?" is always
    answerable, even years later after the live rules have moved on.
    """

    model_config = ConfigDict(frozen=True)

    policy_id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)
    label: str = Field(default="", max_length=120)

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"{self.policy_id}@v{self.version}"


class PriceBreakdown(BaseModel):
    """A transparent, itemised price. The customer-facing number and every
    component behind it.

    Construct through :class:`detoura.services.commercial.CommercialPricingService`;
    the invariants here are a backstop, not the primary guard.
    """

    model_config = ConfigDict(frozen=True)

    currency: str = Field(min_length=3, max_length=3)

    # --- supplier side: exactly as the provider quoted, never adjusted -------
    supplier_transport: float = Field(ge=0.0)
    supplier_baggage: float = Field(default=0.0, ge=0.0)
    supplier_fees: float = Field(default=0.0, ge=0.0)

    # --- Detoura's own commercial component ---------------------------------
    detoura_service_fee: float = Field(default=0.0, ge=0.0)
    detoura_markup: float = Field(default=0.0, ge=0.0)

    # --- reductions and tax ------------------------------------------------
    discount: float = Field(default=0.0, ge=0.0)
    """Amount subtracted from the customer total. Applied to the Detoura
    component unless a promotion explicitly targets the order total."""
    tax: float = Field(default=0.0, ge=0.0)
    """Detoura-side tax/VAT on the service component, where a market is
    configured for it. Supplier-side taxes are already inside the fare."""

    explanation: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _check(self) -> "PriceBreakdown":
        gross = self.gross_before_discount
        if self.discount - gross > 0.005:
            raise ValueError(
                "discount exceeds the total it could apply to "
                f"({self.discount} > {gross})"
            )
        if self.customer_total < -0.005:
            raise ValueError("customer_total is negative")
        if self.detoura_revenue_net < -0.005:
            raise ValueError("a discount cannot exceed Detoura's own revenue")
        return self

    # --- derived --------------------------------------------------------------
    @property
    def supplier_total(self) -> float:
        return round_half_up(
            self.supplier_transport + self.supplier_baggage + self.supplier_fees
        )

    @property
    def detoura_revenue_gross(self) -> float:
        """Service fee + markup, before any promotion."""
        return round_half_up(self.detoura_service_fee + self.detoura_markup)

    @property
    def detoura_revenue_net(self) -> float:
        """What Detoura keeps after the promotion (a promo targeting the order
        total still comes out of Detoura's margin, never the supplier's)."""
        return round_half_up(self.detoura_revenue_gross - self.discount)

    @property
    def gross_before_discount(self) -> float:
        return round_half_up(
            self.supplier_total + self.detoura_revenue_gross + self.tax
        )

    @property
    def customer_total(self) -> float:
        return round_half_up(self.gross_before_discount - self.discount)


class CommercialQuote(BaseModel):
    """A priced journey: the tier, the transparent breakdown, and enough
    provenance to explain and reproduce it."""

    model_config = ConfigDict(frozen=True)

    service_tier: ServiceTier
    breakdown: PriceBreakdown
    markup_policy: PricingPolicyRef
    promo_code: str | None = Field(default=None, max_length=40)
    promo_discount: float = Field(default=0.0, ge=0.0)
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def currency(self) -> str:
        return self.breakdown.currency

    @property
    def customer_total(self) -> float:
        return self.breakdown.customer_total
