"""Detoura promotion codes (V8.5) - provider-neutral, Detoura-funded.

A promotion reduces **Detoura's own commercial component**, not the supplier
fare. ``WELCOME10`` is "10% off the Detoura service fee", never "10% off the
airline ticket". A promo may be configured to target the order total instead,
but even then the reduction comes out of Detoura's margin - the supplier is
always paid in full.

Guarantees enforced here:

* a discount never exceeds the eligible component it applies to;
* a discount never makes any total negative;
* an expired, disabled, out-of-window, over-limit or ineligible code yields a
  zero discount and a stated reason - never a silent partial success.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .commercial import ServiceTier
from .money import round_half_up

_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,39}$")


class PromoKind(str, Enum):
    PERCENTAGE = "PERCENTAGE"
    FIXED = "FIXED"


class PromoTarget(str, Enum):
    """Which amount the discount is measured against and subtracted from."""

    DETOURA_FEE = "DETOURA_FEE"
    ORDER_TOTAL = "ORDER_TOTAL"


class PromoCode(BaseModel):
    """A configured promotion. Persisted in SQLite; see
    :mod:`detoura.persistence.promos`."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=3, max_length=40)
    label: str = Field(default="", max_length=120)
    enabled: bool = True

    starts_at: datetime | None = None
    ends_at: datetime | None = None

    global_limit: int | None = Field(default=None, ge=1)
    per_user_limit: int | None = Field(default=None, ge=1)

    kind: PromoKind = PromoKind.PERCENTAGE
    value: float = Field(gt=0.0)
    """For PERCENTAGE: percent, 0 < value <= 100. For FIXED: an amount in
    ``currency``."""
    currency: str = Field(default="EUR", min_length=3, max_length=3)

    target: PromoTarget = PromoTarget.DETOURA_FEE
    min_order_value: float = Field(default=0.0, ge=0.0)
    max_discount: float | None = Field(default=None, gt=0.0)

    eligible_tiers: tuple[ServiceTier, ...] = Field(default_factory=tuple)
    """Empty means every tier is eligible."""
    eligible_service_types: tuple[str, ...] = Field(default_factory=tuple)

    created_at: datetime | None = None

    @field_validator("code")
    @classmethod
    def _norm_code(cls, v: str) -> str:
        v = v.strip().upper()
        if not _CODE_RE.match(v):
            raise ValueError(
                "code must be 3-40 chars, A-Z 0-9 _ - and start alphanumeric"
            )
        return v

    @model_validator(mode="after")
    def _check(self) -> "PromoCode":
        if self.kind is PromoKind.PERCENTAGE and self.value > 100.0:
            raise ValueError("a percentage promo cannot exceed 100%")
        if (
            self.starts_at is not None
            and self.ends_at is not None
            and self.ends_at <= self.starts_at
        ):
            raise ValueError("ends_at must be after starts_at")
        return self

    def is_live_at(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        if self.starts_at is not None and now < self.starts_at:
            return False
        if self.ends_at is not None and now >= self.ends_at:
            return False
        return True


class PromoRedemption(BaseModel):
    """One accepted use of a code, written when a booking is finalised."""

    model_config = ConfigDict(frozen=True)

    code: str
    booking_id: str
    user_key: str = "anonymous"
    discount_amount: float = Field(ge=0.0)
    currency: str
    redeemed_at: datetime


class PromoContext(BaseModel):
    """Everything :func:`evaluate_promo` needs, gathered by the pricing
    service from the current breakdown and the persisted redemption counts."""

    model_config = ConfigDict(frozen=True)

    now: datetime
    service_tier: ServiceTier
    service_type: str = ""
    currency: str = Field(min_length=3, max_length=3)
    order_value: float = Field(ge=0.0)
    """The pre-discount customer total, for the minimum-order check."""
    detoura_fee_base: float = Field(ge=0.0)
    """Detoura's gross revenue on this booking - the DETOURA_FEE target."""
    global_redemptions: int = Field(default=0, ge=0)
    user_redemptions: int = Field(default=0, ge=0)

    def eligible_base(self, target: PromoTarget) -> float:
        return (
            self.detoura_fee_base
            if target is PromoTarget.DETOURA_FEE
            else self.order_value
        )


class PromoEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    accepted: bool
    discount: float = Field(default=0.0, ge=0.0)
    target: PromoTarget = PromoTarget.DETOURA_FEE
    reason: str = ""
    explanation: tuple[str, ...] = Field(default_factory=tuple)

    @classmethod
    def rejected(cls, code: str, reason: str) -> "PromoEvaluation":
        return cls(code=code, accepted=False, discount=0.0, reason=reason,
                   explanation=(f"{code}: {reason}",))


def evaluate_promo(promo: PromoCode, ctx: PromoContext) -> PromoEvaluation:
    """Pure. Returns the discount this code yields in ``ctx``, or a rejection
    with a stated reason."""
    code = promo.code
    if not promo.enabled:
        return PromoEvaluation.rejected(code, "code is disabled")
    if not promo.is_live_at(ctx.now):
        return PromoEvaluation.rejected(code, "code is not active right now")
    if promo.currency != ctx.currency:
        return PromoEvaluation.rejected(
            code, f"code is in {promo.currency}, this journey is {ctx.currency}"
        )
    if promo.eligible_tiers and ctx.service_tier not in promo.eligible_tiers:
        return PromoEvaluation.rejected(
            code, f"not valid for the {ctx.service_tier.label} service"
        )
    if (
        promo.eligible_service_types
        and ctx.service_type
        and ctx.service_type not in promo.eligible_service_types
    ):
        return PromoEvaluation.rejected(code, "not valid for this booking type")
    if (
        promo.global_limit is not None
        and ctx.global_redemptions >= promo.global_limit
    ):
        return PromoEvaluation.rejected(code, "this code has been fully redeemed")
    if (
        promo.per_user_limit is not None
        and ctx.user_redemptions >= promo.per_user_limit
    ):
        return PromoEvaluation.rejected(code, "you have already used this code")
    if ctx.order_value + 1e-9 < promo.min_order_value:
        return PromoEvaluation.rejected(
            code,
            f"minimum order of {promo.min_order_value:.2f} {ctx.currency} not met",
        )

    base = ctx.eligible_base(promo.target)
    trail = [
        f"{code}: {promo.kind.value.lower()} promo on "
        f"{'Detoura fee' if promo.target is PromoTarget.DETOURA_FEE else 'order total'}"
        f" (base {base:.2f} {ctx.currency})"
    ]
    if base <= 0.005:
        return PromoEvaluation.rejected(
            code,
            "nothing to discount - the "
            + (
                f"{ctx.service_tier.label} service has no Detoura fee"
                if promo.target is PromoTarget.DETOURA_FEE
                else "order total is zero"
            ),
        )

    if promo.kind is PromoKind.PERCENTAGE:
        raw = base * (promo.value / 100.0)
        trail.append(f"{promo.value:.2f}% of {base:.2f} = {raw:.2f}")
    else:
        raw = promo.value
        trail.append(f"flat {raw:.2f} {ctx.currency}")

    discount = raw
    if promo.max_discount is not None and discount > promo.max_discount:
        discount = promo.max_discount
        trail.append(f"capped to max discount {promo.max_discount:.2f}")
    if discount > base:
        discount = base
        trail.append(f"capped to the eligible {base:.2f} (never more)")

    discount = round_half_up(max(0.0, discount))
    if discount <= 0.005:
        return PromoEvaluation.rejected(code, "resolves to no discount here")

    trail.append(f"discount = {discount:.2f} {ctx.currency}")
    return PromoEvaluation(
        code=code, accepted=True, discount=discount, target=promo.target,
        reason="", explanation=tuple(trail),
    )
