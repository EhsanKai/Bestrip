"""Monetary representation (V3).

Real providers quote in whatever currency they like, sometimes per person,
sometimes per room, sometimes with taxes folded in and sometimes not. The
optimizer must not have to care: it works in one currency, tax-inclusive, with
an explicit basis, and normalization happens once at the provider boundary.

This module deliberately does **not** fetch exchange rates. It defines the
representation and the seam; a real deployment injects a rate source.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

#: The currency the optimizer reasons in. Everything is converted to this
#: before it reaches a search state.
BASE_CURRENCY = "EUR"


class PriceBasis(str, Enum):
    """What a quoted price actually covers.

    Getting this wrong is the single most common way to be badly wrong about a
    trip's cost, so it is explicit rather than implied by the field name.
    """

    PER_PERSON = "per_person"
    PER_ROOM_NIGHT = "per_room_night"
    TOTAL = "total"


class Money(BaseModel):
    """An amount in a named currency."""

    model_config = ConfigDict(frozen=True)

    amount: float = Field(ge=0.0)
    currency: str = BASE_CURRENCY
    tax_included: bool = True
    """Whether taxes and mandatory fees are already in :attr:`amount`."""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.amount:.2f} {self.currency}"

    def __add__(self, other: "Money") -> "Money":
        if other.currency != self.currency:
            raise ValueError(
                f"cannot add {other.currency} to {self.currency}; convert first"
            )
        if other.tax_included != self.tax_included:
            raise ValueError("cannot add tax-inclusive and tax-exclusive amounts")
        return Money(
            amount=_round(self.amount + other.amount),
            currency=self.currency,
            tax_included=self.tax_included,
        )

    def scaled(self, factor: float) -> "Money":
        if factor < 0:
            raise ValueError("factor must not be negative")
        return Money(
            amount=_round(self.amount * factor),
            currency=self.currency,
            tax_included=self.tax_included,
        )


@runtime_checkable
class ExchangeRateSource(Protocol):
    """Supplies the rate from one currency to another."""

    def rate(self, from_currency: str, to_currency: str) -> float: ...


class FixedExchangeRates:
    """A deterministic rate table.

    Real deployments swap in a live source. Rates here are illustrative and,
    like everything else in the MVP, synthetic - they exist so the conversion
    path is exercised and tested, not so anyone can price a real booking.
    """

    #: Units of ``BASE_CURRENCY`` per one unit of the key currency.
    DEFAULT_RATES: dict[str, float] = {
        "EUR": 1.00,
        "USD": 0.92,
        "GBP": 1.17,
        "CHF": 1.05,
        "CZK": 0.040,
        "HUF": 0.0026,
        "DKK": 0.134,
    }

    def __init__(self, rates: dict[str, float] | None = None) -> None:
        self._rates = dict(rates or self.DEFAULT_RATES)

    def rate(self, from_currency: str, to_currency: str) -> float:
        if from_currency == to_currency:
            return 1.0
        try:
            source = self._rates[from_currency]
            target = self._rates[to_currency]
        except KeyError as error:
            raise ValueError(f"no exchange rate for {error.args[0]}") from error
        if target <= 0:
            raise ValueError(f"invalid rate for {to_currency}")
        return source / target


class PriceNormalizer:
    """Converts provider quotes into the optimizer's internal representation.

    The single place currency conversion happens. Scattering it through the
    codebase is how a planner ends up comparing dollars against euros.
    """

    def __init__(
        self,
        rates: ExchangeRateSource | None = None,
        *,
        base_currency: str = BASE_CURRENCY,
        tax_uplift: float = 0.0,
    ) -> None:
        self.rates = rates or FixedExchangeRates()
        self.base_currency = base_currency
        self.tax_uplift = tax_uplift
        """Multiplier applied to tax-exclusive quotes, e.g. ``0.20`` for 20% VAT."""

    def to_base(self, money: Money) -> Money:
        """Convert to the base currency, taxes included."""
        amount = money.amount * self.rates.rate(money.currency, self.base_currency)
        if not money.tax_included:
            amount *= 1.0 + self.tax_uplift
        return Money(
            amount=_round(amount), currency=self.base_currency, tax_included=True
        )

    def per_person(self, money: Money, basis: PriceBasis, travelers: int) -> float:
        """The per-person amount in base currency, whatever the quote's basis."""
        if travelers < 1:
            raise ValueError("travelers must be >= 1")
        base = self.to_base(money)
        if basis is PriceBasis.PER_PERSON:
            return base.amount
        if basis is PriceBasis.TOTAL:
            return _round(base.amount / travelers)
        raise ValueError(f"{basis} is not a per-traveler basis")

    def party_total(self, money: Money, basis: PriceBasis, travelers: int) -> float:
        """The whole-party amount in base currency, whatever the quote's basis."""
        if travelers < 1:
            raise ValueError("travelers must be >= 1")
        base = self.to_base(money)
        if basis is PriceBasis.PER_PERSON:
            return _round(base.amount * travelers)
        if basis is PriceBasis.TOTAL:
            return base.amount
        raise ValueError(
            f"{basis} depends on nights and room count; price the stay explicitly"
        )


def _round(amount: float) -> float:
    """Round half-up to cents, the way money is rounded."""
    return float(
        Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
