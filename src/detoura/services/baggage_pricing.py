"""Pricing a trip's baggage, and admitting when we cannot (V7 Phase 3).

One function computes what a trip's bags cost, and every caller uses it. That
is not tidiness: Phase 1 shipped a defect precisely because two places measured
"transit" and quietly disagreed, and a second extraction site here would be a
second chance to disagree about something the traveller pays.

The asymmetry this module exists to hold
----------------------------------------

An unquoted baggage fee is treated as **zero in an admissible lower bound** and
as **unknown in a displayed total**. Those look contradictory and are not:

* A lower bound answers "what is the least this trip could possibly cost?" Zero
  is a *true* statement about a fee nobody has quoted, and it keeps the bound
  admissible. Guessing a positive figure would make the bound overestimate, and
  the search would prune trips the traveller could actually afford - a silent
  loss of correct answers, which is the failure V6 spent real effort ruling out
  and the V7 brief restates as "never treat an unknown direct-return bound as
  unreachable".

* A displayed total answers "what will I pay?" Zero there is a *false*
  statement, and it is the specific lie this whole phase exists to stop.

So the same unknown contributes nothing to :func:`lower_bound_per_traveller`
and forces ``PARTIAL_UNKNOWN`` in :func:`quote_trip`. Both are correct; they
answer different questions.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from ..models.baggage import (
    BaggageKind,
    BaggagePolicy,
    BaggageQuote,
    BaggageRequirement,
    BaggageStatus,
    PriceCompleteness,
)
from ..models.money import BASE_CURRENCY
from ..models.transport import TransportOption


def policy_of(leg: TransportOption) -> BaggagePolicy:
    """The leg's stated policy, or an all-unknown one when it stated nothing.

    ``leg.baggage is None`` means the provider was silent, which is three
    UNKNOWNs - not an absence of restrictions.
    """
    return leg.baggage if leg.baggage is not None else BaggagePolicy.all_unknown()


def quote_trip(
    legs: Sequence[TransportOption],
    requirement: BaggageRequirement,
    *,
    travelers: int,
) -> BaggageQuote:
    """What the required bag costs across every leg, for the whole party.

    Priced **per leg and per traveller**, because that is how airlines charge:
    a cabin bag on a three-leg trip for two people is six purchases, and a
    trip-level multiplier would be wrong the moment one leg differs from
    another.

    A single unquoted leg makes the whole trip ``PARTIAL_UNKNOWN``, however
    many other legs were priced. The known figure is still reported - it is
    real - but :attr:`BaggageQuote.total_for_display` refuses to hand it over
    as if it were the answer.
    """
    if travelers < 1:
        raise ValueError("travelers must be >= 1")
    kind = requirement.kind
    if kind is None:
        return BaggageQuote.not_required()

    known_total = 0.0
    unknown_legs = 0
    unavailable_legs = 0

    for leg in legs:
        allowance = policy_of(leg).allowance(kind)
        if allowance.status is BaggageStatus.NOT_AVAILABLE:
            # Cannot be bought at any price. Counted separately from unknown:
            # "you may not bring this" and "we don't know the fee" call for
            # completely different things to be said to the traveller.
            unavailable_legs += 1
            continue
        cost = allowance.extra_cost_per_traveller
        if cost is None:
            unknown_legs += 1
            continue
        if cost.currency != BASE_CURRENCY:
            # A fee quoted in another currency is not a fee we can state in
            # this trip's terms. Summing the bare number would relabel USD 20
            # as EUR 20 - inventing a figure by renaming it, which is the same
            # dishonesty as inventing one outright. Conversion belongs at the
            # provider boundary, where PriceNormalizer has a rate source; until
            # something converts it, the honest report is that we do not know.
            unknown_legs += 1
            continue
        known_total += cost.amount * travelers

    # An unbuyable bag counts as incomplete too. The sum is finished, but
    # there is no price for a thing that cannot be bought, and reporting
    # COMPLETE would make it read exactly like a bag that travels free.
    completeness = (
        PriceCompleteness.COMPLETE
        if unknown_legs == 0 and unavailable_legs == 0
        else PriceCompleteness.PARTIAL_UNKNOWN
    )
    return BaggageQuote(
        requirement=requirement,
        known_total=round(known_total, 2),
        currency=BASE_CURRENCY,
        completeness=completeness,
        unknown_legs=unknown_legs,
        unavailable_legs=unavailable_legs,
    )


def lower_bound_per_traveller(
    legs: Iterable[TransportOption], requirement: BaggageRequirement
) -> float:
    """The least the required baggage could cost one traveller on these legs.

    **Only quoted fees enter this sum.** An unknown contributes zero, which is
    what keeps the bound admissible - see the module docstring. Never call this
    to build a number shown to a traveller; call :func:`quote_trip`, which
    reports what it does not know.

    .. note::
       **Not yet wired into pruning.** Nothing in the planner, the validator or
       the optimizer calls this today, because Phase 3 prices baggage *after*
       the search: a baggage requirement changes what we claim about a trip's
       price, not which trips exist. So this function currently protects
       nothing.

       It is kept, and tested, because the moment baggage affects feasibility -
       a hard requirement that must fit inside the budget - the pruning floor
       will need exactly this rule, and deriving it under deadline is how an
       inadmissible bound gets shipped. The tests are the specification for
       that future wiring, not a claim about present behaviour.
    """
    kind = requirement.kind
    if kind is None:
        return 0.0
    total = 0.0
    for leg in legs:
        cost = policy_of(leg).allowance(kind).extra_cost_per_traveller
        if cost is not None:
            total += cost.amount
    return round(total, 2)


def describe(quote: BaggageQuote) -> str:
    """One line about the bags, honest about the gaps.

    Assembled from the structured quote, never written around it, so it cannot
    claim more than the data supports.
    """
    if quote.requirement is BaggageRequirement.NONE:
        return "No baggage requirement was given, so no baggage is priced here."

    label = quote.requirement.value.replace("_", " ")
    if not quote.satisfiable:
        return (
            f"{label.capitalize()} cannot be added on "
            f"{quote.unavailable_legs} leg(s) of this trip."
        )
    if quote.is_complete:
        if quote.known_total == 0.0:
            return f"{label.capitalize()} is included on every leg."
        return f"{label.capitalize()} costs {quote.known_total:.2f} {quote.currency} for the party."
    if quote.known_total == 0.0:
        return (
            f"{label.capitalize()} is not priced on {quote.unknown_legs} leg(s), "
            "so the cost is unknown."
        )
    return (
        f"{quote.known_total:.2f} {quote.currency} of {label} fees are known, "
        f"plus an unquoted fee on {quote.unknown_legs} leg(s)."
    )
