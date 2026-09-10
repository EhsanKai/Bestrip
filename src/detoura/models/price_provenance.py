"""Price provenance + reconciliation (V8.5 release blocker fix).

Three prices must never be conflated:

A. **Whole-trip estimate** - the optimizer's modelled cost for the whole trip,
   including accommodation and transfers Detoura is *not* booking.
B. **Bookable supplier transport subtotal** - the current provider fares for the
   tickets Detoura can actually book, whole party.
C. **Customer payable total** - B plus Detoura's fee, minus a promo, plus any
   configured tax.

The customer is charged from (B), never from (A). This module recomputes (B)
straight from the per-leg fares and checks that the number the commercial
engine priced against is the same, within rounding tolerance. If it is not,
confirmation is refused - the alternative is charging someone for a hotel
Detoura never books.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .money import round_half_up


class LegPrice(BaseModel):
    model_config = ConfigDict(frozen=True)
    sequence: int
    label: str
    per_person: float = Field(ge=0.0)
    travelers: int = Field(ge=1)
    revalidated: bool = False

    @property
    def party_total(self) -> float:
        return round_half_up(self.per_person * self.travelers)


class PriceReconciliation(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    currency: str
    #: What the commercial engine used as the supplier transport base.
    priced_supplier_transport: float
    #: The authoritative subtotal recomputed from the per-leg fares (party).
    bookable_ticket_subtotal: float
    delta: float
    tolerance: float
    reason: str = ""
    legs: list[LegPrice] = Field(default_factory=list)

    @property
    def blocks_confirmation(self) -> bool:
        return not self.ok


def reconcile(
    *,
    currency: str,
    legs: list[LegPrice],
    priced_supplier_transport: float,
    trip_estimate: dict | None = None,
) -> PriceReconciliation:
    """Check the priced supplier transport against the sum of the bookable
    ticket fares."""
    bookable = round_half_up(sum(l.party_total for l in legs))
    priced = round_half_up(priced_supplier_transport)
    delta = round_half_up(priced - bookable)
    tol = max(0.5, round_half_up(0.01 * bookable))

    reason = ""
    ok = abs(delta) <= tol + 1e-9

    # A pointed guard against the exact defect: the whole-trip estimate (which
    # models accommodation) must never have become the supplier fare.
    est = trip_estimate or {}
    est_total = float(est.get("total") or 0.0)
    est_accom = float(est.get("accommodation") or 0.0)
    if (
        est_accom > 0.005
        and abs(priced - est_total) <= 0.5
        and abs(priced - bookable) > tol
    ):
        ok = False
        reason = (
            "the priced supplier transport equals the whole-trip estimate, "
            "which includes accommodation Detoura is not booking"
        )
    elif not ok:
        reason = (
            f"priced supplier transport {priced:.2f} {currency} does not "
            f"reconcile with the bookable ticket subtotal {bookable:.2f} "
            f"(off by {delta:+.2f}, tolerance {tol:.2f})"
        )

    return PriceReconciliation(
        ok=ok, currency=currency,
        priced_supplier_transport=priced,
        bookable_ticket_subtotal=bookable,
        delta=delta, tolerance=tol, reason=reason, legs=legs,
    )
