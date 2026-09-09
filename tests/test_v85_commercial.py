"""V8.5 Phase A: the commercial pricing engine.

Supplier fare and Detoura revenue stay separate. Markup is deterministic and
bounded. Promotions are Detoura-funded, capped at the eligible component, and
never make a total negative. A booking keeps the policy version it was priced
with. Nothing here makes a network call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from detoura.models.commercial import PriceBreakdown, PricingPolicyRef, ServiceTier
from detoura.models.markup import (
    DynamicMarkupPolicy,
    MarkupBounds,
    MarkupContext,
    MarkupRule,
)
from detoura.models.money import from_minor_units, to_minor_units
from detoura.models.promo import (
    PromoCode,
    PromoContext,
    PromoKind,
    PromoTarget,
    evaluate_promo,
)
from detoura.persistence import bootstrap, economics, policies, promos
from detoura.persistence.db import Database
from detoura.services.commercial import CommercialPricingService


@pytest.fixture
def db() -> Database:
    return bootstrap(Database(":memory:"))


@pytest.fixture
def pricing(db) -> CommercialPricingService:
    return CommercialPricingService(db)


# --- money helpers --------------------------------------------------------
@pytest.mark.parametrize("amount", [0.0, 12.34, 12.005, 999999.99, 0.1])
def test_minor_units_round_trip(amount):
    assert from_minor_units(to_minor_units(amount)) == pytest.approx(
        round(amount + 1e-9, 2), abs=0.01
    )


# --- breakdown invariants ------------------------------------------------
def test_breakdown_derives_customer_total():
    b = PriceBreakdown(
        currency="EUR", supplier_transport=400.0,
        detoura_service_fee=8.0, detoura_markup=12.0,
    )
    assert b.supplier_total == 400.0
    assert b.detoura_revenue_gross == 20.0
    assert b.customer_total == 420.0
    assert b.detoura_revenue_net == 20.0


def test_breakdown_rejects_discount_above_gross():
    with pytest.raises(ValueError):
        PriceBreakdown(
            currency="EUR", supplier_transport=100.0,
            detoura_markup=10.0, discount=200.0,
        )


def test_breakdown_rejects_discount_above_detoura_revenue():
    # supplier 100 + markup 10, a 40 discount would push Detoura net negative
    with pytest.raises(ValueError):
        PriceBreakdown(
            currency="EUR", supplier_transport=100.0,
            detoura_markup=10.0, discount=40.0,
        )


# --- markup engine -------------------------------------------------------
def test_seed_policy_prices_both_tiers(pricing):
    basic = pricing.quote(
        supplier_transport=400.0, currency="EUR", ticket_count=3,
        service_tier=ServiceTier.BASIC,
    ).quote.breakdown
    allin = pricing.quote(
        supplier_transport=400.0, currency="EUR", ticket_count=3,
        service_tier=ServiceTier.ALL_IN_ONE,
    ).quote.breakdown
    assert basic.supplier_total == 400.0 and allin.supplier_total == 400.0
    assert basic.detoura_markup == pytest.approx(12.0)  # 3%
    assert basic.detoura_service_fee == 0.0
    assert allin.detoura_markup == pytest.approx(20.0)  # 5%
    assert allin.detoura_service_fee == pytest.approx(6.0)
    assert basic.customer_total == 412.0
    assert allin.customer_total == 426.0
    # the product invariant
    assert allin.customer_total >= basic.customer_total


def test_markup_is_deterministic(pricing):
    ctx = dict(supplier_transport=333.33, currency="EUR", ticket_count=2,
               service_tier=ServiceTier.ALL_IN_ONE)
    a = pricing.quote(**ctx).quote.breakdown.customer_total
    b = pricing.quote(**ctx).quote.breakdown.customer_total
    assert a == b


def test_bounds_clamp_a_misconfigured_rule():
    policy = DynamicMarkupPolicy(
        policy_id="x", version=1,
        rules=(MarkupRule(label="greedy", percentage=0.9, fixed_fee=900.0),),
        bounds=MarkupBounds(max_percentage=0.1, max_fixed_fee=20.0,
                            max_total_fee=50.0),
    )
    d = policy.evaluate(MarkupContext(
        service_tier=ServiceTier.BASIC, ticket_count=1,
        supplier_total=1000.0, currency="EUR",
    ))
    assert d.bounded is True
    assert d.total_fee <= 50.0 + 1e-6
    assert d.service_fee >= 0.0 and d.markup >= 0.0


def test_markup_context_has_no_personal_fields():
    # The context model is a closed set. If a personal/protected attribute is
    # ever added here, this test must fail and force a review.
    allowed = {"service_tier", "ticket_count", "supplier_total", "currency",
               "market"}
    assert set(MarkupContext.model_fields) == allowed


def test_no_rule_match_means_zero_fee():
    policy = DynamicMarkupPolicy(
        policy_id="x", version=1,
        rules=(MarkupRule(when_tier=ServiceTier.ALL_IN_ONE, percentage=0.05),),
    )
    d = policy.evaluate(MarkupContext(
        service_tier=ServiceTier.BASIC, ticket_count=1,
        supplier_total=500.0, currency="EUR",
    ))
    assert d.total_fee == 0.0
    assert d.matched_rule == "(none)"


# --- promo engine ------------------------------------------------------
def _ctx(**kw) -> PromoContext:
    base = dict(
        now=datetime(2026, 6, 1, tzinfo=timezone.utc),
        service_tier=ServiceTier.ALL_IN_ONE, currency="EUR",
        order_value=420.0, detoura_fee_base=20.0,
        global_redemptions=0, user_redemptions=0,
    )
    base.update(kw)
    return PromoContext(**base)


def test_percentage_promo_on_detoura_fee():
    promo = PromoCode(code="TEN", kind=PromoKind.PERCENTAGE, value=10.0)
    ev = evaluate_promo(promo, _ctx())
    assert ev.accepted and ev.discount == pytest.approx(2.0)  # 10% of 20


def test_promo_never_exceeds_eligible_component():
    promo = PromoCode(code="HUGE", kind=PromoKind.FIXED, value=500.0)
    ev = evaluate_promo(promo, _ctx(detoura_fee_base=20.0))
    assert ev.accepted and ev.discount == pytest.approx(20.0)


def test_promo_respects_max_discount():
    promo = PromoCode(code="CAP", kind=PromoKind.PERCENTAGE, value=100.0,
                      max_discount=5.0)
    ev = evaluate_promo(promo, _ctx(detoura_fee_base=20.0))
    assert ev.discount == pytest.approx(5.0)


def test_expired_and_future_codes_rejected():
    promo = PromoCode(
        code="WINDOW", kind=PromoKind.FIXED, value=3.0,
        starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    assert not evaluate_promo(promo, _ctx()).accepted  # now is June
    early = _ctx(now=datetime(2025, 12, 1, tzinfo=timezone.utc))
    assert not evaluate_promo(promo, early).accepted


def test_limits_rejected():
    promo = PromoCode(code="LIM", kind=PromoKind.FIXED, value=3.0,
                      global_limit=5, per_user_limit=1)
    assert not evaluate_promo(promo, _ctx(global_redemptions=5)).accepted
    assert not evaluate_promo(promo, _ctx(user_redemptions=1)).accepted


def test_min_order_value_enforced():
    promo = PromoCode(code="MIN", kind=PromoKind.FIXED, value=3.0,
                      min_order_value=1000.0)
    assert not evaluate_promo(promo, _ctx(order_value=420.0)).accepted


def test_tier_eligibility():
    promo = PromoCode(code="AIO", kind=PromoKind.FIXED, value=3.0,
                      eligible_tiers=(ServiceTier.ALL_IN_ONE,))
    assert evaluate_promo(promo, _ctx(service_tier=ServiceTier.ALL_IN_ONE)).accepted
    assert not evaluate_promo(promo, _ctx(service_tier=ServiceTier.BASIC)).accepted


def test_promo_on_zero_fee_tier_is_rejected_with_reason(pricing, db):
    promos.save_promo(db, PromoCode(code="FEEONLY", kind=PromoKind.PERCENTAGE,
                                    value=50.0), actor="test")
    # Force a policy where BASIC has no fee at all.
    policies.save_policy(db, DynamicMarkupPolicy(
        policy_id="detoura.markup", version=2, label="no basic fee",
        rules=(MarkupRule(when_tier=ServiceTier.ALL_IN_ONE, fixed_fee=8.0),),
    ), active=True, actor="test")
    res = pricing.quote(
        supplier_transport=300.0, currency="EUR", ticket_count=2,
        service_tier=ServiceTier.BASIC, promo_code="FEEONLY",
    )
    assert res.promo is not None and not res.promo.accepted
    assert res.quote.breakdown.discount == 0.0
    assert res.quote.breakdown.customer_total == 300.0


# --- persistence: policy versioning + immutability ---------------------
def test_saving_a_new_policy_version_keeps_the_old_one(db):
    builtin = policies.BUILTIN_VERSION
    v_builtin = policies.get_policy(db, "detoura.markup", builtin)
    assert v_builtin is not None
    policies.save_policy(db, DynamicMarkupPolicy(
        policy_id="detoura.markup", version=builtin + 1, label="ops v",
        rules=(MarkupRule(when_tier=ServiceTier.BASIC, percentage=0.09),),
    ), active=True, actor="ops:alice")
    assert policies.active_policy(db).version == builtin + 1
    # the built-in version is untouched
    assert policies.get_policy(db, "detoura.markup", builtin) is not None


def test_economics_ledger_is_write_once(db, pricing):
    res = pricing.quote(
        supplier_transport=400.0, currency="EUR", ticket_count=3,
        service_tier=ServiceTier.ALL_IN_ONE,
    )
    ok1 = economics.write_snapshot(
        db, booking_id="bk_1", journey_reference="DTR-V8-AAA",
        quote=res.quote, snapshot={"x": 1},
    )
    ok2 = economics.write_snapshot(
        db, booking_id="bk_1", journey_reference="DTR-V8-AAA",
        quote=res.quote, snapshot={"x": 2},
    )
    assert ok1 is True and ok2 is False
    row = economics.get(db, "bk_1")
    assert row.customer_price == res.quote.breakdown.customer_total
    assert row.markup_policy_version == policies.BUILTIN_VERSION
    assert row.snapshot == {"x": 1}  # first write wins


def test_unknown_costs_are_none_not_zero(db, pricing):
    res = pricing.quote(supplier_transport=400.0, currency="EUR",
                        ticket_count=2, service_tier=ServiceTier.BASIC)
    economics.write_snapshot(db, booking_id="bk_2",
                             journey_reference="DTR-V8-BBB",
                             quote=res.quote, snapshot={})
    row = economics.get(db, "bk_2")
    assert row.provider_cost_estimate is None
    assert row.payment_cost is None
    assert row.has_unknown_costs is True
    assert row.contribution_margin is None  # not computable while costs unknown


def test_redemption_recorded_once_per_booking(db):
    from detoura.models.promo import PromoRedemption
    r = PromoRedemption(code="WELCOME5", booking_id="bk_9", user_key="u1",
                        discount_amount=1.0, currency="EUR",
                        redeemed_at=datetime.now(timezone.utc))
    assert promos.record_redemption(db, r) is True
    assert promos.record_redemption(db, r) is False
    g, u = promos.redemption_counts(db, "WELCOME5", "u1")
    assert g == 1 and u == 1


def test_disabled_seed_promo_is_not_applied(db, pricing):
    promos.set_enabled(db, "WELCOME5", False, actor="ops")
    res = pricing.quote(
        supplier_transport=400.0, currency="EUR", ticket_count=3,
        service_tier=ServiceTier.ALL_IN_ONE, promo_code="WELCOME5",
    )
    assert not res.promo.accepted
    assert res.quote.breakdown.discount == 0.0
