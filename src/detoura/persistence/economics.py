"""The booking economics ledger (V8.5).

One row per booking, written once when the journey reaches a terminal state.
It records the *priced* components exactly as the customer was quoted them,
plus the pricing-policy version used. Those never change afterwards - a later
markup-rule change does not touch historical rows.

Four cost fields (provider/API, payment, refund, recovery) are frequently
UNKNOWN at booking time. They are stored as NULL, never 0, and can be filled in
later by ops. "Unknown" and "zero" are different facts and the ledger keeps
them different.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import BaseModel

from ..models.commercial import CommercialQuote
from ..models.money import from_minor_units, to_minor_units
from . import audit
from .db import Database


class EconomicsRow(BaseModel):
    booking_id: str
    journey_reference: str
    created_at: datetime
    currency: str
    service_tier: str
    markup_policy_id: str
    markup_policy_version: int
    promo_code: str | None = None

    supplier_transport: float
    supplier_baggage: float
    supplier_fees: float
    service_fee: float
    markup: float
    discount: float
    tax: float
    customer_price: float

    # None == UNKNOWN (not zero)
    provider_cost_estimate: float | None = None
    payment_cost: float | None = None
    refund: float | None = None
    recovery_cost: float | None = None

    breakdown: dict
    snapshot: dict

    @property
    def supplier_cost(self) -> float:
        return round(
            self.supplier_transport + self.supplier_baggage + self.supplier_fees, 2
        )

    @property
    def detoura_gross_revenue(self) -> float:
        return round(self.service_fee + self.markup - self.discount, 2)

    @property
    def known_costs(self) -> float:
        """Sum of the cost fields that are known. UNKNOWN ones are excluded,
        not treated as zero - see :attr:`has_unknown_costs`."""
        return round(
            sum(
                c
                for c in (
                    self.provider_cost_estimate,
                    self.payment_cost,
                    self.recovery_cost,
                )
                if c is not None
            ),
            2,
        )

    @property
    def has_unknown_costs(self) -> bool:
        return any(
            c is None
            for c in (
                self.provider_cost_estimate,
                self.payment_cost,
                self.recovery_cost,
            )
        )

    @property
    def contribution_margin(self) -> float | None:
        """Detoura revenue minus every attributable cost. ``None`` while any
        cost is still UNKNOWN - a margin computed from partial costs would be
        a lie."""
        if self.has_unknown_costs:
            return None
        refund = self.refund or 0.0
        return round(self.detoura_gross_revenue - self.known_costs - refund, 2)


def write_snapshot(
    db: Database,
    *,
    booking_id: str,
    journey_reference: str,
    quote: CommercialQuote,
    snapshot: dict,
) -> bool:
    """Insert the ledger row. Immutable: a second call for the same
    ``booking_id`` is a no-op and returns ``False``."""
    b = quote.breakdown
    created = datetime.now(timezone.utc).isoformat()
    with db.write() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO booking_economics ("
            " booking_id, journey_reference, created_at, currency, service_tier,"
            " markup_policy_id, markup_policy_version, promo_code,"
            " supplier_transport_minor, supplier_baggage_minor, supplier_fees_minor,"
            " service_fee_minor, markup_minor, discount_minor, tax_minor,"
            " customer_price_minor, breakdown_json, snapshot_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                booking_id, journey_reference, created, b.currency,
                quote.service_tier.value,
                quote.markup_policy.policy_id, quote.markup_policy.version,
                quote.promo_code,
                to_minor_units(b.supplier_transport),
                to_minor_units(b.supplier_baggage),
                to_minor_units(b.supplier_fees),
                to_minor_units(b.detoura_service_fee),
                to_minor_units(b.detoura_markup),
                to_minor_units(b.discount),
                to_minor_units(b.tax),
                to_minor_units(b.customer_total),
                json.dumps(_breakdown_dict(quote), separators=(",", ":")),
                json.dumps(snapshot, default=str, separators=(",", ":")),
            ),
        )
        return cur.rowcount > 0


def update_costs(
    db: Database,
    booking_id: str,
    *,
    actor: str,
    provider_cost_estimate: float | None = None,
    payment_cost: float | None = None,
    refund: float | None = None,
    recovery_cost: float | None = None,
) -> EconomicsRow:
    """Fill in the later-known cost fields. The priced components are never
    touched here."""
    row = get(db, booking_id)
    if row is None:
        raise KeyError(booking_id)
    sets: list[str] = []
    params: list = []
    for col, val in (
        ("provider_cost_estimate_minor", provider_cost_estimate),
        ("payment_cost_minor", payment_cost),
        ("refund_minor", refund),
        ("recovery_cost_minor", recovery_cost),
    ):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(to_minor_units(val))
    if not sets:
        return row
    params.append(booking_id)
    with db.write() as conn:
        conn.execute(
            f"UPDATE booking_economics SET {', '.join(sets)} WHERE booking_id = ?",
            tuple(params),
        )
    audit.record(
        db, actor=actor, action="BOOKING_COSTS_UPDATED",
        target_type="booking_economics", target_id=booking_id,
        before={
            "provider_cost_estimate": row.provider_cost_estimate,
            "payment_cost": row.payment_cost, "refund": row.refund,
            "recovery_cost": row.recovery_cost,
        },
        after={
            "provider_cost_estimate": provider_cost_estimate,
            "payment_cost": payment_cost, "refund": refund,
            "recovery_cost": recovery_cost,
        },
    )
    return get(db, booking_id)  # type: ignore[return-value]


def get(db: Database, booking_id: str) -> EconomicsRow | None:
    row = db.query_one(
        "SELECT * FROM booking_economics WHERE booking_id = ?", (booking_id,)
    )
    return _row(row) if row else None


def list_recent(db: Database, *, limit: int = 200) -> list[EconomicsRow]:
    limit = max(1, min(int(limit), 2000))
    rows = db.query(
        "SELECT * FROM booking_economics ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return [_row(r) for r in rows]


def _m(row, key) -> float:
    return from_minor_units(row[key])


def _mn(row, key) -> float | None:
    return from_minor_units(row[key]) if row[key] is not None else None


def _row(row) -> EconomicsRow:
    return EconomicsRow(
        booking_id=row["booking_id"],
        journey_reference=row["journey_reference"],
        created_at=datetime.fromisoformat(row["created_at"]),
        currency=row["currency"],
        service_tier=row["service_tier"],
        markup_policy_id=row["markup_policy_id"],
        markup_policy_version=row["markup_policy_version"],
        promo_code=row["promo_code"],
        supplier_transport=_m(row, "supplier_transport_minor"),
        supplier_baggage=_m(row, "supplier_baggage_minor"),
        supplier_fees=_m(row, "supplier_fees_minor"),
        service_fee=_m(row, "service_fee_minor"),
        markup=_m(row, "markup_minor"),
        discount=_m(row, "discount_minor"),
        tax=_m(row, "tax_minor"),
        customer_price=_m(row, "customer_price_minor"),
        provider_cost_estimate=_mn(row, "provider_cost_estimate_minor"),
        payment_cost=_mn(row, "payment_cost_minor"),
        refund=_mn(row, "refund_minor"),
        recovery_cost=_mn(row, "recovery_cost_minor"),
        breakdown=json.loads(row["breakdown_json"]),
        snapshot=json.loads(row["snapshot_json"]),
    )


def _breakdown_dict(quote: CommercialQuote) -> dict:
    b = quote.breakdown
    return {
        "currency": b.currency,
        "supplier_transport": b.supplier_transport,
        "supplier_baggage": b.supplier_baggage,
        "supplier_fees": b.supplier_fees,
        "supplier_total": b.supplier_total,
        "detoura_service_fee": b.detoura_service_fee,
        "detoura_markup": b.detoura_markup,
        "detoura_revenue_gross": b.detoura_revenue_gross,
        "discount": b.discount,
        "detoura_revenue_net": b.detoura_revenue_net,
        "tax": b.tax,
        "customer_total": b.customer_total,
        "service_tier": quote.service_tier.value,
        "markup_policy": str(quote.markup_policy),
        "promo_code": quote.promo_code,
        "explanation": list(b.explanation),
    }
