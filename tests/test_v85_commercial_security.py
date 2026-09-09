"""V8.5 Phase A security: the server owns all commercial truth.

Agent 5's brief for Phase A: client-supplied supplier price / service fee /
markup / final price must be ignored; a customer cannot change an ALL_IN_ONE
price; promo abuse (expired, disabled, over-limit, reuse of a single-use code,
below-minimum) must fail closed; a 100%+ discount and a negative markup must be
impossible to produce.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from detoura.models.commercial import ServiceTier
from detoura.models.markup import DynamicMarkupPolicy, MarkupBounds, MarkupRule
from detoura.models.promo import PromoCode, PromoKind
from detoura.persistence import bootstrap, promos
from detoura.persistence.db import Database
from detoura.services.commercial import CommercialPricingService


@pytest.fixture
def db():
    return bootstrap(Database(":memory:"))


@pytest.fixture
def client(tmp_path):
    os.environ["DETOURA_DB_PATH"] = str(tmp_path / "sec.db")
    from detoura.api.app import create_app

    c = TestClient(create_app())
    yield c
    os.environ.pop("DETOURA_DB_PATH", None)


LEGS = [
    {"origin": "CGN", "destination": "BER", "departure": "2026-10-01T09:00:00Z",
     "arrival": "2026-10-01T10:10:00Z", "carrier": "LH", "flight_number": "1",
     "price_per_person": 120.0},
    {"origin": "BER", "destination": "MUC", "departure": "2026-10-03T09:00:00Z",
     "arrival": "2026-10-03T10:15:00Z", "carrier": "LH", "flight_number": "2",
     "price_per_person": 95.0},
    {"origin": "MUC", "destination": "CGN", "departure": "2026-10-05T09:00:00Z",
     "arrival": "2026-10-05T10:10:00Z", "carrier": "LH", "flight_number": "3",
     "price_per_person": 110.0},
]


def _create(client, **extra):
    body = {"demo_trip_label": "Sec", "demo_currency": "EUR",
            "demo_total": 325.0, "demo_travelers": 1, "demo_legs": LEGS}
    body.update(extra)
    r = client.post("/api/v1/booking-intents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --- the request body cannot set a price ------------------------------
def test_client_supplied_price_fields_are_ignored(client):
    j = _create(
        client,
        # none of these are real fields; pydantic drops them, but assert the
        # resulting price is the server's, not anything the client asked for
        customer_price=1.0, detoura_markup=0.0, service_fee=0.0,
        supplier_transport=1.0, discount=999.0, customer_total=1.0,
    )
    c = j["commercial"]
    assert c["breakdown"]["supplier_total"] == 325.0
    assert c["breakdown"]["customer_total"] == 341.25  # 325 + 5% basic markup
    assert c["breakdown"]["discount"] == 0.0


def test_customer_cannot_undercut_all_in_one_price(client):
    j = _create(client, service_tier="ALL_IN_ONE")
    bid = j["booking_id"]
    # try to re-price to a bogus cheaper tier / inject amounts
    r = client.post(
        f"/api/v1/booking-intents/{bid}/commercial",
        json={"service_tier": "ALL_IN_ONE", "customer_total": 1.0,
              "detoura_markup": 0.0},
    )
    assert r.status_code == 200
    c = r.json()["commercial"]
    assert c["breakdown"]["customer_total"] > 325.0
    assert c["breakdown"]["detoura_revenue_gross"] > 0.0


def test_unknown_service_tier_is_rejected(client):
    r = client.post("/api/v1/booking-intents", json={
        "demo_currency": "EUR", "demo_total": 325.0, "demo_legs": LEGS,
        "service_tier": "FREE_LUNCH",
    })
    assert r.status_code == 422


def test_commercial_options_locked_after_confirm(client):
    j = _create(client)
    bid = j["booking_id"]
    client.post(f"/api/v1/booking-intents/{bid}/travelers", json={"travelers": [{
        "given_name": "Ada", "family_name": "L", "born_on": "1990-01-01",
        "email": "a@b.com", "phone": "+441234567"}]})
    client.post(f"/api/v1/booking-intents/{bid}/confirm", json={})
    r = client.post(f"/api/v1/booking-intents/{bid}/commercial",
                    json={"service_tier": "ALL_IN_ONE"})
    assert r.status_code == 409


# --- promo abuse fails closed ---------------------------------------
def test_disabled_promo_rejected(db):
    promos.save_promo(db, PromoCode(code="OFF", kind=PromoKind.PERCENTAGE,
                                    value=50.0, enabled=False), actor="t")
    svc = CommercialPricingService(db)
    res = svc.quote(supplier_transport=400.0, currency="EUR", ticket_count=3,
                    service_tier=ServiceTier.ALL_IN_ONE, promo_code="OFF")
    assert not res.promo.accepted and res.quote.breakdown.discount == 0.0


def test_single_use_promo_cannot_be_reused_by_same_user(db):
    from detoura.models.promo import PromoRedemption
    promos.save_promo(db, PromoCode(code="ONCE", kind=PromoKind.FIXED, value=5.0,
                                    per_user_limit=1), actor="t")
    promos.record_redemption(db, PromoRedemption(
        code="ONCE", booking_id="bk_a", user_key="anonymous",
        discount_amount=5.0, currency="EUR",
        redeemed_at=datetime.now(timezone.utc)))
    svc = CommercialPricingService(db)
    res = svc.quote(supplier_transport=400.0, currency="EUR", ticket_count=3,
                    service_tier=ServiceTier.ALL_IN_ONE, promo_code="ONCE")
    assert not res.promo.accepted


def test_global_limit_blocks_further_use(db):
    from detoura.models.promo import PromoRedemption
    promos.save_promo(db, PromoCode(code="GLOB1", kind=PromoKind.FIXED, value=5.0,
                                    global_limit=1), actor="t")
    promos.record_redemption(db, PromoRedemption(
        code="GLOB1", booking_id="bk_x", user_key="someone",
        discount_amount=5.0, currency="EUR",
        redeemed_at=datetime.now(timezone.utc)))
    svc = CommercialPricingService(db)
    res = svc.quote(supplier_transport=400.0, currency="EUR", ticket_count=3,
                    service_tier=ServiceTier.ALL_IN_ONE, promo_code="GLOB1",
                    user_key="a-different-user")
    assert not res.promo.accepted


def test_hundred_percent_promo_cannot_zero_the_journey(db):
    promos.save_promo(db, PromoCode(code="ALLOFF", kind=PromoKind.PERCENTAGE,
                                    value=100.0), actor="t")
    svc = CommercialPricingService(db)
    res = svc.quote(supplier_transport=400.0, currency="EUR", ticket_count=3,
                    service_tier=ServiceTier.ALL_IN_ONE, promo_code="ALLOFF")
    b = res.quote.breakdown
    # 100% of the Detoura fee at most - the supplier fare is untouched
    assert b.discount <= b.detoura_revenue_gross + 1e-6
    assert b.customer_total >= b.supplier_total - 1e-6
    assert b.customer_total > 0


def test_promo_targeting_order_total_still_cannot_exceed_detoura_revenue(db):
    from detoura.models.promo import PromoTarget
    promos.save_promo(db, PromoCode(
        code="ORDER50", kind=PromoKind.PERCENTAGE, value=50.0,
        target=PromoTarget.ORDER_TOTAL), actor="t")
    svc = CommercialPricingService(db)
    res = svc.quote(supplier_transport=400.0, currency="EUR", ticket_count=3,
                    service_tier=ServiceTier.ALL_IN_ONE, promo_code="ORDER50")
    b = res.quote.breakdown
    assert b.discount <= b.detoura_revenue_gross + 1e-6
    assert b.detoura_revenue_net >= -1e-6
    assert b.customer_total >= b.supplier_total - 1e-6


def test_percentage_promo_over_100_is_rejected_at_definition():
    with pytest.raises(ValueError):
        PromoCode(code="BAD", kind=PromoKind.PERCENTAGE, value=150.0)


def test_negative_or_zero_promo_value_rejected_at_definition():
    with pytest.raises(ValueError):
        PromoCode(code="NEG", kind=PromoKind.FIXED, value=-10.0)
    with pytest.raises(ValueError):
        PromoCode(code="ZERO", kind=PromoKind.FIXED, value=0.0)


def test_markup_rule_cannot_be_negative():
    with pytest.raises(ValueError):
        MarkupRule(percentage=-0.1)
    with pytest.raises(ValueError):
        MarkupRule(fixed_fee=-5.0)


def test_markup_bounds_are_always_enforced_even_with_a_hostile_policy(db):
    from detoura.persistence import policies
    policies.save_policy(db, DynamicMarkupPolicy(
        policy_id="detoura.markup", version=99, label="hostile",
        rules=(MarkupRule(percentage=1.0, fixed_fee=1000.0),),
        bounds=MarkupBounds(max_percentage=0.15, max_fixed_fee=25.0,
                            max_total_fee=120.0),
    ), active=True, actor="attacker")
    svc = CommercialPricingService(db)
    b = svc.quote(supplier_transport=5000.0, currency="EUR", ticket_count=3,
                  service_tier=ServiceTier.BASIC).quote.breakdown
    assert b.detoura_revenue_gross <= 120.0 + 1e-6
