"""CommercialPricingService (V8.5) - turns supplier costs into a transparent,
policy-backed customer price.

It is the single place the markup engine and the promo engine are combined, and
the single place a ``PriceBreakdown`` is constructed for a real booking. The
server owns every number here; nothing in the request body may set a price, a
fee, a margin or a discount.

Order of operations:

1. evaluate the active :class:`DynamicMarkupPolicy` -> Detoura service fee + markup
2. build the breakdown with no discount yet
3. if a promo code was given, evaluate it against that breakdown
4. apply the discount - capped so it never exceeds Detoura's own gross revenue
   (a promotion is Detoura-funded; the supplier is always paid in full and
   Detoura never books the commercial component at a loss)
5. return the quote plus the promo evaluation, so the UI can explain a rejection
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..models.commercial import (
    CommercialQuote,
    PriceBreakdown,
    PricingPolicyRef,
    ServiceTier,
)
from ..models.markup import DynamicMarkupPolicy, MarkupContext, MarkupDecision
from ..models.money import round_half_up
from ..models.promo import (
    PromoContext,
    PromoEvaluation,
    PromoTarget,
    evaluate_promo,
)
from ..persistence import policies, promos
from ..persistence.db import Database


@dataclass(slots=True)
class QuoteResult:
    quote: CommercialQuote
    markup: MarkupDecision
    promo: PromoEvaluation | None


class CommercialPricingService:
    def __init__(
        self,
        db: Database,
        *,
        market: str = "EU",
        detoura_tax_rate: float = 0.0,
        now=None,
    ) -> None:
        self._db = db
        self._market = market
        self._tax_rate = max(0.0, detoura_tax_rate)
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- the entry point ----------------------------------------------------
    def quote(
        self,
        *,
        supplier_transport: float,
        currency: str,
        ticket_count: int,
        service_tier: ServiceTier,
        supplier_baggage: float = 0.0,
        supplier_fees: float = 0.0,
        promo_code: str | None = None,
        user_key: str = "anonymous",
        markup_policy: DynamicMarkupPolicy | None = None,
    ) -> QuoteResult:
        currency = currency.upper()
        policy = markup_policy or policies.active_policy(self._db)
        supplier_transport = round_half_up(max(0.0, supplier_transport))
        supplier_baggage = round_half_up(max(0.0, supplier_baggage))
        supplier_fees = round_half_up(max(0.0, supplier_fees))
        supplier_total = round_half_up(
            supplier_transport + supplier_baggage + supplier_fees
        )

        markup = policy.evaluate(
            MarkupContext(
                service_tier=service_tier,
                ticket_count=max(1, min(ticket_count, 12)),
                supplier_total=supplier_total,
                currency=currency,
                market=self._market,
            )
        )

        tax = round_half_up((markup.service_fee + markup.markup) * self._tax_rate)

        explain: list[str] = list(markup.explanation)
        promo_eval: PromoEvaluation | None = None
        discount = 0.0

        if promo_code:
            promo_eval, discount, promo_notes = self._apply_promo(
                promo_code=promo_code,
                user_key=user_key,
                currency=currency,
                service_tier=service_tier,
                detoura_gross=round_half_up(markup.service_fee + markup.markup),
                order_value=round_half_up(supplier_total + markup.service_fee + markup.markup + tax),
            )
            explain.extend(promo_notes)

        breakdown = PriceBreakdown(
            currency=currency,
            supplier_transport=supplier_transport,
            supplier_baggage=supplier_baggage,
            supplier_fees=supplier_fees,
            detoura_service_fee=markup.service_fee,
            detoura_markup=markup.markup,
            discount=discount,
            tax=tax,
            explanation=tuple(explain),
        )

        quote = CommercialQuote(
            service_tier=service_tier,
            breakdown=breakdown,
            markup_policy=markup.policy,
            promo_code=(promo_eval.code if promo_eval and promo_eval.accepted else None),
            promo_discount=discount,
        )
        return QuoteResult(quote=quote, markup=markup, promo=promo_eval)

    # -- promo -------------------------------------------------------------
    def _apply_promo(
        self,
        *,
        promo_code: str,
        user_key: str,
        currency: str,
        service_tier: ServiceTier,
        detoura_gross: float,
        order_value: float,
    ) -> tuple[PromoEvaluation, float, list[str]]:
        promo = promos.get_promo(self._db, promo_code)
        if promo is None:
            ev = PromoEvaluation.rejected(
                promo_code.strip().upper(), "unknown code"
            )
            return ev, 0.0, [f"promo {ev.code}: unknown code"]

        g, u = promos.redemption_counts(self._db, promo.code, user_key)
        ctx = PromoContext(
            now=self._now(),
            service_tier=service_tier,
            currency=currency,
            order_value=order_value,
            detoura_fee_base=detoura_gross,
            global_redemptions=g,
            user_redemptions=u,
        )
        ev = evaluate_promo(promo, ctx)
        notes = list(ev.explanation)
        if not ev.accepted:
            return ev, 0.0, notes

        # Detoura-funded, never below zero: cap at Detoura's own gross revenue
        # regardless of what the promo targeted.
        applied = min(ev.discount, detoura_gross)
        if applied + 0.005 < ev.discount:
            notes.append(
                f"promo discount reduced {ev.discount:.2f} -> {applied:.2f}"
                " (a promotion cannot exceed Detoura's own revenue)"
            )
            ev = ev.model_copy(update={"discount": round_half_up(applied)})
        return ev, round_half_up(applied), notes

    # -- recording a redemption ------------------------------------------
    def markup_policy_ref(self) -> PricingPolicyRef:
        return policies.active_policy(self._db).ref
