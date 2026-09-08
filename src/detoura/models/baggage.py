"""Baggage, and what we honestly know about it (V7 Phase 3).

A cheap fare is cheap partly because of what it does not include, and until now
Detoura printed one number and let the reader assume it covered travelling. It
usually does not: most of the fares in this catalog would carry a personal item
and nothing else, and no provider in the current stack says so either way.

The whole module exists to keep three statements apart:

    "I know this bag is free."
    "I know this bag costs 22 euros."
    "I do not know what this bag costs."

The third is not the second with a zero in it. Collapsing them is the failure
this phase is built to prevent, so **unknown is a type here, not a sentinel
float**. `price=None` cannot be added to a total by accident; `Money(0)` can,
and would be a lie the moment a provider had simply stayed silent.

This repository has shipped that exact class of bug before - V6.5's
``if not raw:`` conflated a missing session with an empty one, and corrupt keys
lived forever as a result. The precedent for getting it right is also already
here: ``TransportOption.seats_available = None`` means *unknown*, never
*unlimited*, and the search is careful about the difference.

Nothing in this module mentions a provider. Duffel, Amadeus or a spreadsheet
should all be able to populate it, and V8 should need no redesign to do so.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .money import BASE_CURRENCY, Money


class BaggageKind(str, Enum):
    """The three things travellers actually carry.

    Deliberately not modelled further. Airlines distinguish far more - seat
    bags, sports equipment, weight bands per fare family - and none of it can
    be populated from any provider currently wired up. Modelling a field
    nothing can fill produces a schema that looks informative and is empty.
    """

    PERSONAL_ITEM = "personal_item"
    """The under-seat bag. Nearly always included, but "nearly" is not "is"."""
    CABIN_BAG = "cabin_bag"
    CHECKED_BAG = "checked_bag"


class BaggageStatus(str, Enum):
    """What a fare says about one kind of bag."""

    INCLUDED = "included"
    """In the fare. Costs nothing extra - a *known* zero."""
    EXTRA = "extra"
    """Purchasable for a fee. The fee itself may still be unknown."""
    NOT_AVAILABLE = "not_available"
    """Cannot be added to this fare at any price."""
    UNKNOWN = "unknown"
    """The provider did not say.

    Never collapses into any of the others - and in particular never into
    ``NOT_AVAILABLE``. Assuming the worst is as much an invention as assuming
    the best, and it would slander exactly the cheap fares this feature exists
    to explain.
    """


class BaggageAllowance(BaseModel):
    """What one fare offers for one kind of bag.

    The invariants below are enforced rather than documented, because the whole
    value of this type is that an unknown allowance cannot be mistaken for a
    free one by any caller, however careless.
    """

    model_config = ConfigDict(frozen=True)

    kind: BaggageKind
    status: BaggageStatus
    price: Money | None = None
    """The fee to add this bag, per traveller.

    ``None`` means *not quoted*. For :attr:`BaggageStatus.EXTRA` that is a real
    and common state - "cabin bag not included" with no price attached - and it
    is the reason a status of EXTRA is not enough on its own to price a trip.
    """
    quantity: int = Field(default=1, ge=0)
    max_weight_kg: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def _check_price_matches_status(self) -> "BaggageAllowance":
        if self.status is BaggageStatus.UNKNOWN and self.price is not None:
            raise ValueError(
                "an UNKNOWN allowance cannot carry a price; if a fee is known "
                "the status is EXTRA, and if it is free the status is INCLUDED"
            )
        if self.status is BaggageStatus.NOT_AVAILABLE and self.price is not None:
            raise ValueError(
                "a NOT_AVAILABLE allowance cannot be bought, so it has no price"
            )
        if self.status is BaggageStatus.INCLUDED and self.price is not None:
            if self.price.amount != 0.0:
                raise ValueError(
                    f"an INCLUDED allowance costs nothing extra, not "
                    f"{self.price.amount}"
                )
        return self

    @property
    def cost_is_known(self) -> bool:
        """Whether this allowance can be priced at all.

        ``INCLUDED`` is known and free. ``EXTRA`` is known only when a fee was
        actually quoted. ``UNKNOWN`` never is - which is the entire point.
        """
        if self.status is BaggageStatus.INCLUDED:
            return True
        if self.status is BaggageStatus.EXTRA:
            return self.price is not None
        return False

    @property
    def extra_cost_per_traveller(self) -> Money | None:
        """What adding this bag costs one traveller, or ``None`` if unknown.

        ``None`` here is never safe to treat as zero in a customer-facing
        total. It *is* safe as a contribution to an admissible lower bound -
        see :mod:`detoura.services.baggage_pricing` for why those two are not
        the same statement.
        """
        if self.status is BaggageStatus.INCLUDED:
            return Money(amount=0.0, currency=BASE_CURRENCY)
        if self.status is BaggageStatus.EXTRA:
            return self.price
        return None

    @classmethod
    def unknown(cls, kind: BaggageKind) -> "BaggageAllowance":
        """The honest default for a provider that said nothing."""
        return cls(kind=kind, status=BaggageStatus.UNKNOWN)

    @classmethod
    def included(cls, kind: BaggageKind) -> "BaggageAllowance":
        return cls(
            kind=kind,
            status=BaggageStatus.INCLUDED,
            price=Money(amount=0.0, currency=BASE_CURRENCY),
        )

    @classmethod
    def extra(cls, kind: BaggageKind, amount: float, currency: str = BASE_CURRENCY):
        return cls(
            kind=kind,
            status=BaggageStatus.EXTRA,
            price=Money(amount=amount, currency=currency),
        )

    @classmethod
    def extra_unpriced(cls, kind: BaggageKind) -> "BaggageAllowance":
        """Known to cost *something*, but the provider did not say how much."""
        return cls(kind=kind, status=BaggageStatus.EXTRA)


class BaggagePolicy(BaseModel):
    """Everything one fare says about baggage, for one leg.

    Attached to a :class:`~detoura.models.transport.TransportOption`. A policy
    of ``None`` on that option means the provider supplied nothing at all,
    which reads as three UNKNOWN allowances rather than as an absence of
    restrictions.
    """

    model_config = ConfigDict(frozen=True)

    personal_item: BaggageAllowance
    cabin_bag: BaggageAllowance
    checked_bag: BaggageAllowance

    @model_validator(mode="after")
    def _check_kinds(self) -> "BaggagePolicy":
        for field, expected in (
            (self.personal_item, BaggageKind.PERSONAL_ITEM),
            (self.cabin_bag, BaggageKind.CABIN_BAG),
            (self.checked_bag, BaggageKind.CHECKED_BAG),
        ):
            if field.kind is not expected:
                raise ValueError(
                    f"policy slot for {expected.value} holds a {field.kind.value}"
                )
        return self

    def allowance(self, kind: BaggageKind) -> BaggageAllowance:
        return {
            BaggageKind.PERSONAL_ITEM: self.personal_item,
            BaggageKind.CABIN_BAG: self.cabin_bag,
            BaggageKind.CHECKED_BAG: self.checked_bag,
        }[kind]

    @classmethod
    def all_unknown(cls) -> "BaggagePolicy":
        """What every fare in the current stack honestly is.

        No provider wired up today reports baggage, so this - not "personal
        item only", and certainly not "everything included" - is the truthful
        default.
        """
        return cls(
            personal_item=BaggageAllowance.unknown(BaggageKind.PERSONAL_ITEM),
            cabin_bag=BaggageAllowance.unknown(BaggageKind.CABIN_BAG),
            checked_bag=BaggageAllowance.unknown(BaggageKind.CHECKED_BAG),
        )


class BaggageRequirement(str, Enum):
    """What the traveller says they need to bring.

    ``NONE`` is the default and means "I did not say", which must leave every
    V7 result byte-identical to what it was before this phase existed.
    """

    NONE = "none"
    PERSONAL_ITEM = "personal_item"
    CABIN_BAG = "cabin_bag"
    CHECKED_BAG = "checked_bag"

    @property
    def kind(self) -> BaggageKind | None:
        if self is BaggageRequirement.NONE:
            return None
        return BaggageKind(self.value)


class PriceCompleteness(str, Enum):
    """Whether a quoted total is the whole story."""

    COMPLETE = "complete"
    """Every component the traveller asked about has a known price."""
    PARTIAL_UNKNOWN = "partial_unknown"
    """At least one required component was never quoted.

    A total in this state is a *floor*, not a price. It must never be rendered
    as though the traveller could pay it and travel.
    """


class BaggageQuote(BaseModel):
    """What a whole trip's baggage costs - and what is still unquoted.

    The two numbers are kept apart on purpose. ``known_total`` is a real sum of
    real quotes; ``unknown_legs`` is the count of journeys nobody has priced.
    Reporting the first without the second is precisely the "€20 known baggage
    fees, presented as the total" failure the brief calls out.
    """

    model_config = ConfigDict(frozen=True)

    requirement: BaggageRequirement
    known_total: float = Field(default=0.0, ge=0.0)
    """Party total of every fee that was actually quoted."""
    currency: str = BASE_CURRENCY
    completeness: PriceCompleteness = PriceCompleteness.COMPLETE
    unknown_legs: int = Field(default=0, ge=0)
    """How many legs carry a required bag whose price nobody stated."""
    unavailable_legs: int = Field(default=0, ge=0)
    """Legs where the required bag cannot be bought at all."""

    @property
    def is_complete(self) -> bool:
        return self.completeness is PriceCompleteness.COMPLETE

    @property
    def is_priceable(self) -> bool:
        """Whether an honest, final figure exists for this requirement.

        Both conditions are needed. A fully quoted trip whose bag cannot be
        carried on one leg has a complete *sum* and no meaningful *price* - the
        traveller cannot buy what they asked for at any figure - and reporting
        that as a known total of 0.00 would be indistinguishable from "your bag
        travels free".
        """
        return self.is_complete and self.satisfiable

    @property
    def satisfiable(self) -> bool:
        """Whether the requirement can be met on every leg.

        ``False`` is a statement about availability, not about price. A trip
        with an unbuyable cabin bag fails this even if every other leg quoted a
        fee.
        """
        return self.unavailable_legs == 0

    @property
    def total_for_display(self) -> float | None:
        """The number to print, or ``None`` when there is no honest one.

        Deliberately refuses to return ``known_total`` when something is
        unquoted. A caller that wants the partial figure must ask for
        ``known_total`` explicitly and is thereby forced to decide how to say
        so.
        """
        return self.known_total if self.is_priceable else None

    @classmethod
    def not_required(cls) -> "BaggageQuote":
        """The traveller asked for nothing, so nothing is owed or unknown."""
        return cls(requirement=BaggageRequirement.NONE)
