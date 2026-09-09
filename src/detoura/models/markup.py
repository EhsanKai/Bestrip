"""The dynamic markup engine (V8.5) - TEST / INTERNAL commercial behaviour.

A ``DynamicMarkupPolicy`` turns a small, explicit context into Detoura's own
fee for a booking. It is:

* **deterministic** - the same context always yields the same fee;
* **bounded** - hard caps are enforced *after* the rules, so a mis-configured
  rule can never produce an out-of-range fee;
* **auditable** - every evaluation returns the rule it matched and a plain
  explanation of the arithmetic.

Permitted context dimensions are only these: service tier, number of tickets
(trip complexity / orchestration workload), order value, currency, configured
market. **Never** nationality, race, religion, health, gender, age or any other
personal or protected characteristic - the context model has no field for one,
by design, and this is enforced by tests. This engine does not do opaque
personalised price discrimination.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .commercial import PricingPolicyRef, ServiceTier
from .money import round_half_up


class MarkupContext(BaseModel):
    """The complete, closed set of inputs a markup rule may see.

    If a value is not a field here, a policy cannot key on it. That is the
    point of the model.
    """

    model_config = ConfigDict(frozen=True)

    service_tier: ServiceTier
    ticket_count: int = Field(ge=1, le=12)
    supplier_total: float = Field(ge=0.0)
    currency: str = Field(min_length=3, max_length=3)
    market: str = Field(default="EU", min_length=2, max_length=8)


class MarkupRule(BaseModel):
    """One rule. It applies when every stated condition is met; the policy
    evaluates its rules in order and the first match wins."""

    model_config = ConfigDict(frozen=True)

    label: str = Field(default="", max_length=120)
    when_tier: ServiceTier | None = None
    when_market: str | None = Field(default=None, max_length=8)
    min_tickets: int = Field(default=1, ge=1, le=12)
    min_supplier_total: float = Field(default=0.0, ge=0.0)

    percentage: float = Field(default=0.0, ge=0.0, le=1.0)
    """Fraction of ``supplier_total`` taken as markup, e.g. ``0.03`` for 3%."""
    fixed_fee: float = Field(default=0.0, ge=0.0, le=1_000.0)
    """A flat service fee added on top, in the context currency."""

    def matches(self, ctx: MarkupContext) -> bool:
        if self.when_tier is not None and self.when_tier is not ctx.service_tier:
            return False
        if self.when_market is not None and self.when_market != ctx.market:
            return False
        if ctx.ticket_count < self.min_tickets:
            return False
        if ctx.supplier_total + 1e-9 < self.min_supplier_total:
            return False
        return True


class MarkupBounds(BaseModel):
    """Hard limits applied to every evaluation, whatever the rules say."""

    model_config = ConfigDict(frozen=True)

    max_percentage: float = Field(default=0.25, ge=0.0, le=1.0)
    max_fixed_fee: float = Field(default=100.0, ge=0.0, le=1_000.0)
    min_total_fee: float = Field(default=0.0, ge=0.0)
    max_total_fee: float = Field(default=250.0, ge=0.0)

    @model_validator(mode="after")
    def _order(self) -> "MarkupBounds":
        if self.max_total_fee + 1e-9 < self.min_total_fee:
            raise ValueError("max_total_fee is below min_total_fee")
        return self


class MarkupDecision(BaseModel):
    """The outcome of evaluating a policy. ``service_fee`` is the flat part,
    ``markup`` the percentage part; both are already bounded."""

    model_config = ConfigDict(frozen=True)

    service_fee: float = Field(ge=0.0)
    markup: float = Field(ge=0.0)
    policy: PricingPolicyRef
    matched_rule: str
    bounded: bool
    """True if a cap changed the raw fee - surfaced so an operator can see it."""
    explanation: tuple[str, ...]

    @property
    def total_fee(self) -> float:
        return round_half_up(self.service_fee + self.markup)


class DynamicMarkupPolicy(BaseModel):
    """A named, versioned set of ordered rules plus the bounds that contain
    them. Tune through configuration; the example seeds are not commercial
    truth."""

    model_config = ConfigDict(frozen=True)

    policy_id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)
    label: str = Field(default="", max_length=120)
    rules: tuple[MarkupRule, ...] = Field(default_factory=tuple)
    bounds: MarkupBounds = Field(default_factory=MarkupBounds)

    @property
    def ref(self) -> PricingPolicyRef:
        return PricingPolicyRef(
            policy_id=self.policy_id, version=self.version, label=self.label
        )

    def evaluate(self, ctx: MarkupContext) -> MarkupDecision:
        matched = next((r for r in self.rules if r.matches(ctx)), None)
        trail: list[str] = [f"policy {self.policy_id} v{self.version}"]

        if matched is None:
            trail.append("no rule matched - Detoura fee is 0")
            return MarkupDecision(
                service_fee=0.0, markup=0.0, policy=self.ref,
                matched_rule="(none)", bounded=False, explanation=tuple(trail),
            )

        rule_name = matched.label or f"{matched.when_tier or 'any'} rule"
        trail.append(f"matched: {rule_name}")

        pct_rate = min(matched.percentage, self.bounds.max_percentage)
        if pct_rate != matched.percentage:
            trail.append(
                f"percentage capped {matched.percentage:.3f} -> {pct_rate:.3f}"
            )
        raw_markup = round_half_up(ctx.supplier_total * pct_rate)

        fixed = min(matched.fixed_fee, self.bounds.max_fixed_fee)
        if fixed != matched.fixed_fee:
            trail.append(f"fixed fee capped {matched.fixed_fee:.2f} -> {fixed:.2f}")

        trail.append(
            f"base {ctx.supplier_total:.2f} x {pct_rate:.3f} = {raw_markup:.2f}"
            f" markup, + {fixed:.2f} service fee"
        )

        total = raw_markup + fixed
        bounded = pct_rate != matched.percentage or fixed != matched.fixed_fee
        clamped = min(max(total, self.bounds.min_total_fee), self.bounds.max_total_fee)
        if abs(clamped - total) > 0.005:
            trail.append(
                f"total fee clamped {total:.2f} -> {clamped:.2f}"
                f" (bounds {self.bounds.min_total_fee:.2f}"
                f"..{self.bounds.max_total_fee:.2f})"
            )
            bounded = True
            # Absorb the clamp into the markup first; keep the stated service
            # fee stable, and never let either part go negative.
            markup = max(0.0, round_half_up(clamped - fixed))
            fixed = round_half_up(clamped - markup)
        else:
            markup = raw_markup

        return MarkupDecision(
            service_fee=round_half_up(fixed),
            markup=round_half_up(markup),
            policy=self.ref,
            matched_rule=rule_name,
            bounded=bounded,
            explanation=tuple(trail),
        )
