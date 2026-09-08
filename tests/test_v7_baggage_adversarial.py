"""V7 Phase 3 baggage truth: independent adversarial QA (Agent 5).

The implementer's ``test_v7_baggage.py`` establishes the happy paths. This
file tries specifically to break the claim that three statements never
collapse into each other:

    "I know this bag is free."
    "I know this bag costs 22 euros."
    "I do not know what this bag costs."

and the stated asymmetry: an unquoted fee is zero in an admissible lower
bound and unknown in a displayed total.

Tests are grouped by the attack list in the review brief. A test that FAILS
here is a defect in the implementation, not a mistake in the test - each
failing test carries a comment explaining exactly what is wrong and why it
matters, and is left failing on purpose so the failure is the report.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from detoura.config import PlannerConfig
from detoura.data.synthetic_baggage import BaggageScenario
from detoura.models.baggage import (
    BaggageAllowance,
    BaggageKind,
    BaggagePolicy,
    BaggageQuote,
    BaggageRequirement,
    BaggageStatus,
    PriceCompleteness,
)
from detoura.models.itinerary import CostBreakdown, Itinerary
from detoura.models.money import Money
from detoura.models.patch import TripPatch
from detoura.models.transport import TransportOption, TransportType
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.providers.transport import SyntheticTransportDataProvider
from detoura.search_modes import SearchMode, apply_mode
from detoura.services import trip_comparison
from detoura.services.baggage_pricing import (
    describe,
    lower_bound_per_traveller,
    policy_of,
    quote_trip,
)
from detoura.services.planner import TravelPlanner
from detoura.services.reoptimizer import derive_request

WINDOW_FROM = date(2026, 9, 10)
WINDOW_TO = date(2026, 9, 24)
CABIN = BaggageKind.CABIN_BAG
CHECKED = BaggageKind.CHECKED_BAG


def leg(
    index: int,
    cabin: BaggageAllowance | None,
    price: float = 50.0,
    checked: BaggageAllowance | None = None,
) -> TransportOption:
    policy = None
    if cabin is not None:
        policy = BaggagePolicy(
            personal_item=BaggageAllowance.included(BaggageKind.PERSONAL_ITEM),
            cabin_bag=cabin,
            checked_bag=checked or BaggageAllowance.unknown(BaggageKind.CHECKED_BAG),
        )
    return TransportOption(
        id=f"L{index}",
        origin="A",
        destination="B",
        departure=datetime(2026, 9, 10, 8),
        arrival=datetime(2026, 9, 10, 10),
        price_per_person=price,
        transport_type=TransportType.FLIGHT,
        baggage=policy,
    )


def a_request(**kw) -> TripRequest:
    fields = dict(
        origin="Köln",
        budget=900.0,
        travelers=2,
        duration_days=5,
        date_from=WINDOW_FROM,
        date_to=WINDOW_TO,
        preferred_destinations=["Berlin"],
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
    )
    fields.update(kw)
    return TripRequest(**fields)


# ===========================================================================
# DEFECT 1 (P0): NOT_AVAILABLE collapses into a known-zero total/completeness
# ===========================================================================
#
# `BaggageQuote.completeness` is derived from `unknown_legs` alone
# (`services/baggage_pricing.py::quote_trip`). A leg whose required bag is
# NOT_AVAILABLE increments `unavailable_legs`, not `unknown_legs`, so a trip
# where every leg says "you cannot bring this at any price" reports
# `completeness=COMPLETE` and `known_total=0.0` - and therefore
# `total_for_display=0.0`, since that property only consults `is_complete`.
#
# `describe()` gets this right because it special-cases `satisfiable` before
# it looks at completeness. But `total_for_display`, `known_total` and
# `completeness` are exposed as independent, individually-meaningful fields
# on `BaggageQuote` and on the `BaggageDTO` the API returns - the DTO's own
# docstring says a client "simply reaching for" `total_for_display` is safe.
# It is not: for a required bag that cannot be added at all, that field reads
# exactly like "this bag is free", which is the one collapse this entire
# phase exists to prevent - just via NOT_AVAILABLE instead of UNKNOWN.
def test_DEFECT_unavailable_bag_reads_as_a_known_free_total():
    unavailable = BaggageAllowance(kind=CABIN, status=BaggageStatus.NOT_AVAILABLE)
    quote = quote_trip([leg(1, unavailable)], BaggageRequirement.CABIN_BAG, travelers=2)

    # Not in dispute: satisfiable correctly reports the bag cannot be added.
    assert quote.satisfiable is False

    # BUG: a bag that cannot be added at any price is reported exactly like a
    # bag that is included for free - COMPLETE and a display total of 0.0.
    # A client reading total_for_display/completeness alone (as the DTO's own
    # docstring says is safe) cannot tell "included free" from "cannot bring
    # this bag on this itinerary at all". An unsatisfiable requirement must
    # not be reported the same way a fully-known, satisfiable one is.
    assert quote.completeness is not PriceCompleteness.COMPLETE, (
        "DEFECT: an unsatisfiable requirement (satisfiable=False) is "
        "reported as completeness=COMPLETE, identical to a fully-known, "
        "satisfiable one"
    )
    assert quote.total_for_display is None, (
        "DEFECT: total_for_display reads 0.0 EUR for a bag that cannot be "
        "brought on this trip at any price (total_for_display=%r) - "
        "indistinguishable from 'this bag is included free'"
        % (quote.total_for_display,)
    )


def test_DEFECT_unavailable_bag_looks_free_in_the_json_response_shape():
    """Same defect, exercised through the exact fields the HTTP API exposes."""
    unavailable = BaggageAllowance(kind=CHECKED, status=BaggageStatus.NOT_AVAILABLE)
    quote = quote_trip(
        [leg(1, BaggageAllowance.included(CABIN), checked=unavailable)],
        BaggageRequirement.CHECKED_BAG,
        travelers=1,
    )
    body = json.loads(quote.model_dump_json())
    # This is what `BaggageDTO` in api/contracts.py forwards verbatim
    # (assembler.py: `total_for_display=itinerary.baggage.total_for_display`).
    # A UI that shows "completeness == complete -> price is final" and prints
    # total_for_display would show "checked bag: EUR 0.00" for a bag that
    # cannot be checked on this fare at all - it must not.
    assert body["completeness"] != "complete", (
        "DEFECT: wire-level completeness=%r for an unsatisfiable requirement "
        "is indistinguishable from a fully priced, satisfiable one"
        % (body["completeness"],)
    )


def test_DEFECT_comparison_treats_unavailable_baggage_as_a_known_zero_fee():
    """`_compare_baggage` gates on `is_complete`, inheriting the same flaw.

    A traveler whose *own* idea cannot carry a checked bag at all gets that
    reported as "your baggage costs EUR 0", identical to genuinely free
    baggage, the moment Detoura's side is fully priced - producing a real,
    numeric `baggage_delta` built on a fabricated zero.
    """
    unavailable_quote = quote_trip(
        [leg(1, BaggageAllowance.included(CABIN), checked=BaggageAllowance(
            kind=CHECKED, status=BaggageStatus.NOT_AVAILABLE
        ))],
        BaggageRequirement.CHECKED_BAG,
        travelers=1,
    )
    known_quote = quote_trip(
        [leg(1, BaggageAllowance.included(CABIN), checked=BaggageAllowance.extra(CHECKED, 30.0))],
        BaggageRequirement.CHECKED_BAG,
        travelers=1,
    )
    delta, unknowns = trip_comparison._compare_baggage(unavailable_quote, known_quote)

    # BUG: the traveler's own trip cannot carry this bag at all - that is not
    # the same fact as "it would have cost EUR 0" - yet a numeric delta is
    # produced as though it were, and no `unknowns` entry flags the gap.
    assert delta is None, (
        "DEFECT: baggage_delta is a real number (%r) built by treating an "
        "unsatisfiable requirement on the traveler's own trip as a known, "
        "priced EUR 0.00 fee" % (delta,)
    )
    assert unknowns, (
        "DEFECT: no 'unknowns' entry names that the original trip's baggage "
        "requirement cannot be satisfied at all"
    )


# ===========================================================================
# DEFECT 2 (P0): the API's own headline total and its cost breakdown total
# disagree by exactly the known baggage fee, with no line item to explain it
# ===========================================================================
#
# `Itinerary.total_cost` never includes baggage (by design, so pre-Phase-3
# search signatures survive unchanged - see planner.py's `_to_itinerary`).
# `CostBreakdown.total`, however, *does* sum in `baggage` (itinerary.py).
# `api/assembler.py::recommendation_dto` sets:
#     total_price = itinerary.total_cost                (excludes baggage)
#     costs.total = itinerary.cost_breakdown.total       (includes baggage)
# and `CostBreakdownDTO` (api/contracts.py) has no `baggage` field, so the
# gap between the two "total" fields in the very same JSON object is never
# named anywhere in that object. A client that trusts either field alone, or
# that sums the visible line items (`transport + accommodation +
# ground_transfer`), gets three different numbers for "the price of this
# trip" - and none of them is flagged as partial.
def test_DEFECT_cost_breakdown_total_disagrees_with_itinerary_total_cost():
    request = a_request(baggage=BaggageRequirement.CABIN_BAG)
    planner = TravelPlanner(
        transport_provider=SyntheticTransportDataProvider(
            baggage=BaggageScenario.PERSONAL_ONLY  # cabin bag: known EUR 20/traveller
        )
    )
    top = planner.plan(request).recommendations[0]

    assert top.baggage.is_complete
    assert top.baggage.known_total > 0.0, "fixture must actually carry a fee"

    # This is the real defect: cost_breakdown.total folds in a fully known,
    # non-zero baggage fee that itinerary.total_cost - the number a client
    # calls "the price" - does not include.
    assert top.cost_breakdown.total == pytest.approx(top.total_cost), (
        "DEFECT: cost_breakdown.total (%.2f) silently includes a fully known "
        "baggage fee that total_cost (%.2f) does not; nothing in the "
        "CostBreakdown/CostBreakdownDTO shape says which figure is 'the "
        "total', and their components (transport+accommodation+"
        "ground_transfer=%.2f) match neither" % (
            top.cost_breakdown.total,
            top.total_cost,
            top.cost_breakdown.transport
            + top.cost_breakdown.accommodation
            + top.cost_breakdown.ground_transfer,
        )
    )


def test_DEFECT_api_dto_exposes_two_disagreeing_totals_for_one_trip():
    """Same defect at the actual wire shape returned to a client."""
    from detoura.api.assembler import recommendation_dto
    from detoura.services.confidence import SearchQuality

    request = a_request(baggage=BaggageRequirement.CABIN_BAG)
    planner = TravelPlanner(
        transport_provider=SyntheticTransportDataProvider(
            baggage=BaggageScenario.PERSONAL_ONLY
        )
    )
    result = planner.plan(request)
    top = result.recommendations[0]
    dto = recommendation_dto(
        top, result, SearchQuality(), travelers=2, now=datetime(2026, 9, 6)
    )
    body = json.loads(dto.model_dump_json())

    parts_sum = round(
        body["costs"]["transport"]
        + body["costs"]["accommodation"]
        + body["costs"]["ground_transfer"],
        2,
    )
    # BUG (three-way inconsistency in one JSON object):
    #  - body["total_price"]        excludes the known baggage fee
    #  - body["costs"]["total"]     includes it
    #  - summing costs' own visible parts matches neither, and "baggage" is
    #    not a key inside `costs` at all to reconcile the gap.
    assert body["total_price"] == pytest.approx(body["costs"]["total"]), (
        "DEFECT: total_price=%.2f != costs.total=%.2f in the SAME response "
        "for the same trip; sum of costs' own line items = %.2f, matching "
        "neither. A fully known EUR %.2f baggage fee "
        "(body['baggage']['total_for_display']) is invisible in one 'total' "
        "and silently folded into the other." % (
            body["total_price"], body["costs"]["total"], parts_sum,
            body["baggage"]["total_for_display"],
        )
    )


# ===========================================================================
# DEFECT 3 (P0): FITS_BUDGET / GOOD_BUDGET_USAGE are asserted true even when
# the honest total - fare plus a fully known, mandatory baggage fee - is over
# budget
# ===========================================================================
#
# Because baggage never enters `total_cost` (by design, to preserve golden
# signatures), budget-derived explanation factors are computed purely against
# the bag-less fare. When a traveler names a mandatory bag and its fee is
# fully known and non-zero, the trip can still be labelled FITS_BUDGET /
# GOOD_BUDGET_USAGE while `total_with_known_baggage` - built from real,
# complete numbers, no uncertainty involved - exceeds the stated budget.
# This is not an "unknown baggage hides risk" case (that would at least be
# defensible as "we said what we don't know"); every number here is known.
def test_DEFECT_fits_budget_claimed_while_known_baggage_pushes_trip_over_budget():
    from detoura.models.itinerary import ExplanationFactor

    budget = 450.0
    request = a_request(budget=budget, baggage=BaggageRequirement.CABIN_BAG)
    planner = TravelPlanner(
        transport_provider=SyntheticTransportDataProvider(
            baggage=BaggageScenario.PERSONAL_ONLY
        )
    )
    top = planner.plan(request).recommendations[0]

    assert top.baggage.is_complete, "the whole point: nothing is unknown here"
    honest_total = top.total_with_known_baggage
    assert honest_total > budget, (
        "test setup assumption failed - the fixture no longer produces an "
        "over-budget honest total; adjust the budget in this test"
    )

    # BUG: the search actively tells the traveler this fits their budget,
    # using every real, known number, while the honest total exceeds it.
    assert ExplanationFactor.FITS_BUDGET not in top.explanation_factors, (
        "DEFECT: FITS_BUDGET is asserted (fare-only total=%.2f <= budget="
        "%.2f) even though total_with_known_baggage=%.2f exceeds the "
        "traveler's stated budget by a fully known, mandatory, non-zero "
        "baggage fee" % (top.total_cost, budget, honest_total)
    )


# ===========================================================================
# DEFECT 4 (P1): a non-EUR baggage fee is silently summed as if it were EUR
# ===========================================================================
#
# `BaggageAllowance.extra(kind, amount, currency=...)` accepts any currency,
# and `Money.__add__` explicitly refuses to add mismatched currencies
# ("cannot add {other.currency} to {self.currency}; convert first"). But
# `quote_trip` never uses `Money.__add__` - it does `known_total +=
# cost.amount * travelers` on the raw float, bypassing that protection
# entirely, and always reports the result labelled `currency="EUR"`
# (`BASE_CURRENCY`). A $20 fee is reported as a EUR 20 fee.
def test_DEFECT_foreign_currency_fee_is_relabelled_as_base_currency():
    usd_policy = BaggagePolicy(
        personal_item=BaggageAllowance.included(BaggageKind.PERSONAL_ITEM),
        cabin_bag=BaggageAllowance.extra(CABIN, 20.0, currency="USD"),
        checked_bag=BaggageAllowance.unknown(CHECKED),
    )
    usd_leg = TransportOption(
        id="U1", origin="A", destination="B",
        departure=datetime(2026, 9, 10, 8), arrival=datetime(2026, 9, 10, 10),
        price_per_person=50.0, transport_type=TransportType.FLIGHT,
        baggage=usd_policy,
    )
    quote = quote_trip([usd_leg], BaggageRequirement.CABIN_BAG, travelers=1)

    # BUG: the allowance was quoted in USD; quote_trip reports EUR and uses
    # the raw numeric amount unconverted, silently turning $20 into "EUR
    # 20.00" - inventing a number by relabelling it, which is exactly the
    # class of dishonesty this phase exists to prevent.
    assert quote.currency == "EUR"
    assert quote.known_total == 0.0, (
        "DEFECT: a USD 20 fee was summed as EUR 20.00 (known_total=%.2f) "
        "instead of being rejected or converted; quote_trip bypasses "
        "Money.__add__'s own currency-mismatch guard by summing "
        "`.amount` floats directly" % quote.known_total
    )


# ===========================================================================
# Guarantees attacked and NOT broken - documented as passing tests
# ===========================================================================

# --- A. UNKNOWN is never a zero, anywhere reachable from Python or JSON ----
def test_unknown_survives_json_round_trip_as_null_never_zero():
    allowance = BaggageAllowance.unknown(CABIN)
    body = json.loads(allowance.model_dump_json())
    assert body["price"] is None
    assert body["status"] == "unknown"
    restored = BaggageAllowance.model_validate_json(allowance.model_dump_json())
    assert restored.price is None
    assert restored.status is BaggageStatus.UNKNOWN
    assert restored.extra_cost_per_traveller is None


def test_quote_json_never_turns_an_unknown_leg_into_a_priced_total():
    quote = quote_trip(
        [leg(1, BaggageAllowance.unknown(CABIN))],
        BaggageRequirement.CABIN_BAG,
        travelers=4,
    )
    body = json.loads(quote.model_dump_json())
    assert body["completeness"] == "partial_unknown"
    # `total_for_display` is a computed property, not a model field, so it is
    # deliberately absent from model_dump_json - callers must use the
    # property, which correctly returns None. Confirm that directly.
    assert quote.total_for_display is None
    assert body["known_total"] == 0.0  # real: no leg was priced


def test_cannot_construct_an_unknown_allowance_carrying_any_price():
    with pytest.raises(ValueError):
        BaggageAllowance(kind=CABIN, status=BaggageStatus.UNKNOWN, price=Money(amount=0.0))
    with pytest.raises(ValueError):
        BaggageAllowance(kind=CABIN, status=BaggageStatus.UNKNOWN, price=Money(amount=22.0))


# --- B. Known-free is distinguishable from unknown at every layer ---------
def test_included_and_unknown_are_distinguishable_through_describe():
    included = quote_trip([leg(1, BaggageAllowance.included(CABIN))],
                          BaggageRequirement.CABIN_BAG, travelers=1)
    unknown = quote_trip([leg(1, BaggageAllowance.unknown(CABIN))],
                         BaggageRequirement.CABIN_BAG, travelers=1)
    assert included.total_for_display == 0.0
    assert unknown.total_for_display is None
    assert "included" in describe(included)
    assert "unknown" in describe(unknown)
    assert describe(included) != describe(unknown)


# --- D. Multi-leg: one unknown forces PARTIAL_UNKNOWN regardless of count -
@pytest.mark.parametrize("n_legs", [2, 3, 4, 6])
def test_a_single_unknown_leg_forces_partial_unknown_at_any_trip_length(n_legs):
    legs = [leg(i, BaggageAllowance.extra(CABIN, 10.0 * i)) for i in range(1, n_legs)]
    legs.append(leg(n_legs, BaggageAllowance.unknown(CABIN)))
    quote = quote_trip(legs, BaggageRequirement.CABIN_BAG, travelers=1)
    assert quote.completeness is PriceCompleteness.PARTIAL_UNKNOWN
    assert quote.total_for_display is None
    assert quote.known_total > 0.0  # the known legs are still reported


def test_mixed_included_extra_unavailable_unknown_across_six_legs():
    legs = [
        leg(1, BaggageAllowance.included(CABIN)),
        leg(2, BaggageAllowance.extra(CABIN, 15.0)),
        leg(3, BaggageAllowance.unknown(CABIN)),
        leg(4, BaggageAllowance(kind=CABIN, status=BaggageStatus.NOT_AVAILABLE)),
        leg(5, BaggageAllowance.included(CABIN)),
        leg(6, BaggageAllowance.extra(CABIN, 5.0)),
    ]
    quote = quote_trip(legs, BaggageRequirement.CABIN_BAG, travelers=2)
    assert quote.unknown_legs == 1
    assert quote.unavailable_legs == 1
    assert quote.known_total == pytest.approx((15.0 + 5.0) * 2)
    # PARTIAL_UNKNOWN because of the real unknown leg - independent of the
    # separate unavailable-leg defect documented above.
    assert quote.completeness is PriceCompleteness.PARTIAL_UNKNOWN
    assert quote.satisfiable is False


# --- C. Float precision at awkward fees, large parties ---------------------
@pytest.mark.parametrize(
    "fee,travelers,legs_count",
    [(0.01, 9, 3), (19.99, 7, 3), (33.33, 6, 3), (0.1, 3, 3)],
)
def test_known_total_is_exact_to_the_cent_at_awkward_fees(fee, travelers, legs_count):
    legs = [leg(i, BaggageAllowance.extra(CABIN, fee)) for i in range(1, legs_count + 1)]
    quote = quote_trip(legs, BaggageRequirement.CABIN_BAG, travelers=travelers)
    expected = round(fee * travelers * legs_count, 2)
    assert quote.known_total == pytest.approx(expected, abs=1e-9)


def test_large_party_does_not_overflow_or_misround():
    legs = [leg(1, BaggageAllowance.extra(CABIN, 19.99))]
    quote = quote_trip(legs, BaggageRequirement.CABIN_BAG, travelers=250)
    assert quote.known_total == pytest.approx(19.99 * 250, abs=1e-6)


# --- E. Original vs Detoura comparison matrix -------------------------------
def _quote(known_total, complete=True, unknown_legs=0):
    return BaggageQuote(
        requirement=BaggageRequirement.CABIN_BAG,
        known_total=known_total,
        completeness=(
            PriceCompleteness.COMPLETE if complete else PriceCompleteness.PARTIAL_UNKNOWN
        ),
        unknown_legs=unknown_legs,
    )


def test_both_unknown_no_requirement_given_reports_baggage_as_unpriced_everywhere():
    delta, unknowns = trip_comparison._compare_baggage(None, None)
    assert delta is None
    assert unknowns == ["baggage"]


def test_original_known_detoura_unknown_never_produces_a_delta():
    delta, unknowns = trip_comparison._compare_baggage(
        _quote(20.0), _quote(0.0, complete=False, unknown_legs=1)
    )
    assert delta is None
    assert "this trip's baggage" in unknowns


def test_original_unknown_detoura_known_never_produces_a_delta():
    delta, unknowns = trip_comparison._compare_baggage(
        _quote(0.0, complete=False, unknown_legs=1), _quote(20.0)
    )
    assert delta is None
    assert "your trip's baggage" in unknowns


def test_both_known_equal_gives_a_true_zero_delta_not_an_absent_one():
    delta, unknowns = trip_comparison._compare_baggage(_quote(30.0), _quote(30.0))
    assert delta == 0.0
    assert unknowns == []


def test_detoura_cheaper_and_original_cheaper_both_produce_signed_deltas():
    cheaper_detoura, _ = trip_comparison._compare_baggage(_quote(40.0), _quote(10.0))
    assert cheaper_detoura == pytest.approx(-30.0)
    cheaper_original, _ = trip_comparison._compare_baggage(_quote(10.0), _quote(40.0))
    assert cheaper_original == pytest.approx(30.0)


def test_brief_example_d_cannot_be_produced_when_detoura_baggage_is_unknown():
    """"Detoura saves EUR 30 all-in" must be impossible with unknown baggage.

    Simulates the shape: Detoura's fare is 30 EUR cheaper, but Detoura's own
    baggage is unknown while the original's is known. The all-in comparison
    (baggage_delta) must refuse to combine with the fare saving into a false
    all-in claim - it must come back None, not a number that could be added
    to the fare delta to manufacture "EUR 30 saved, all in".
    """
    delta, unknowns = trip_comparison._compare_baggage(_quote(20.0), _quote(0.0, complete=False, unknown_legs=2))
    assert delta is None, (
        "an 'all-in' saving must never be computable when our own baggage "
        "fee is unpriced"
    )
    assert unknowns


# --- F. Budget: known baggage on a *satisfiable* bag still isn't hidden ----
def test_known_baggage_total_is_at_least_reachable_via_total_with_known_baggage():
    """However explanation factors behave (see DEFECT above), the honest
    all-in figure is at least computable by a careful caller - it is not
    lost, only not used for the headline number or the budget gate."""
    request = a_request(baggage=BaggageRequirement.CABIN_BAG)
    planner = TravelPlanner(
        transport_provider=SyntheticTransportDataProvider(
            baggage=BaggageScenario.PERSONAL_ONLY
        )
    )
    top = planner.plan(request).recommendations[0]
    assert top.total_with_known_baggage > top.total_cost
    assert top.total_with_known_baggage == pytest.approx(
        top.total_cost + top.baggage.known_total
    )


def test_unknown_baggage_never_makes_total_with_known_baggage_smaller():
    """Unknown baggage must never make a trip look cheaper than it is."""
    request = a_request(baggage=BaggageRequirement.CABIN_BAG)  # default: silent fares
    top = TravelPlanner().plan(request).recommendations[0]
    assert top.baggage.completeness is PriceCompleteness.PARTIAL_UNKNOWN
    # A floor, not a discount: unknown legs contribute nothing extra, they
    # never subtract.
    assert top.total_with_known_baggage == top.total_cost


# --- G. Admissible lower bounds ---------------------------------------------
def test_lower_bound_never_exceeds_true_known_minimum_across_shapes():
    legs = [
        leg(1, BaggageAllowance.extra(CABIN, 12.5)),
        leg(2, BaggageAllowance.included(CABIN)),
        leg(3, BaggageAllowance.unknown(CABIN)),
        leg(4, BaggageAllowance(kind=CABIN, status=BaggageStatus.NOT_AVAILABLE)),
    ]
    bound = lower_bound_per_traveller(legs, BaggageRequirement.CABIN_BAG)
    quote = quote_trip(legs, BaggageRequirement.CABIN_BAG, travelers=1)
    assert bound <= quote.known_total
    assert bound == pytest.approx(12.5)


def test_lower_bound_is_exactly_zero_for_none_requirement_regardless_of_fees():
    legs = [leg(1, BaggageAllowance.extra(CABIN, 500.0))]
    assert lower_bound_per_traveller(legs, BaggageRequirement.NONE) == 0.0


def test_lower_bound_never_guesses_a_positive_figure_for_pure_unknown():
    legs = [leg(i, BaggageAllowance.unknown(CABIN)) for i in range(1, 5)]
    assert lower_bound_per_traveller(legs, BaggageRequirement.CABIN_BAG) == 0.0


# --- H. Reoptimization: ChangeBaggageRequirement really changes the request
def test_change_baggage_requirement_actually_changes_the_derived_request():
    planner = TravelPlanner()
    request = a_request()
    result = planner.plan(request)
    itinerary = result.recommendations[0]
    patch = TripPatch.model_validate(
        {"operations": [{"op": "change_baggage_requirement", "baggage": "checked_bag"}]}
    )
    derived = derive_request(request, itinerary, patch)
    assert derived.request.baggage is BaggageRequirement.CHECKED_BAG


def test_baggage_requirement_survives_reoptimization_alongside_a_city_lock():
    """Locks and the new requirement must both take effect, not just one."""
    planner = TravelPlanner()
    request = a_request()
    result = planner.plan(request)
    itinerary = result.recommendations[0]
    city = itinerary.cities[0]
    patch = TripPatch.model_validate(
        {
            "operations": [
                {"op": "lock_city", "city": city},
                {"op": "change_baggage_requirement", "baggage": "cabin_bag"},
            ]
        }
    )
    derived = derive_request(request, itinerary, patch)
    assert derived.request.baggage is BaggageRequirement.CABIN_BAG
    assert any(c.casefold() == city.casefold() for c in derived.request.must_visit)


def test_unknown_fee_stays_unknown_after_a_baggage_edit_on_silent_fares():
    planner = TravelPlanner()  # default silent-baggage synthetic fares
    request = a_request()
    result = planner.plan(request)
    itinerary = result.recommendations[0]
    patch = TripPatch.model_validate(
        {"operations": [{"op": "change_baggage_requirement", "baggage": "checked_bag"}]}
    )
    derived = derive_request(request, itinerary, patch)
    fresh = planner.plan(derived.request).recommendations[0]
    assert fresh.baggage.completeness is PriceCompleteness.PARTIAL_UNKNOWN
    assert fresh.baggage.total_for_display is None


# --- I. Backward compatibility: golden anchors, with and without baggage ---
@pytest.mark.parametrize(
    "mode,duration,states,rejected,completed,pareto,score,route",
    [
        ("QUICK", 5, 1196, 692, 158, 13, 0.719869, "CGN -> Berlin -> Munich -> CGN"),
        ("SMART", 5, 9730, 6804, 1076, 68, 0.734457, "CGN -> Berlin -> Munich -> CGN"),
        ("SMART", 7, 10066, 6416, 1124, 110, 0.736198,
         "CGN -> Berlin -> Prague -> Vienna -> CGN"),
    ],
)
def test_golden_anchors_hold_with_a_baggage_requirement_attached(
    mode, duration, states, rejected, completed, pareto, score, route
):
    """The exact numbers pinned in the review brief, run again with a
    baggage requirement (checked bag) attached - the search must not move,
    only the post-hoc pricing changes."""
    request = TripRequest(
        origin="Köln", budget=900.0, travelers=2, duration_days=duration,
        date_from=WINDOW_FROM, date_to=WINDOW_TO, preferred_destinations=["Berlin"],
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
        baggage=BaggageRequirement.CHECKED_BAG,
    )
    result = TravelPlanner(config=apply_mode(PlannerConfig(), SearchMode(mode))).plan(request)
    meta, top = result.metadata, result.recommendations[0]
    assert meta.states_generated == states
    assert meta.states_rejected == rejected
    assert meta.completed_itineraries == completed
    assert meta.pareto_kept == pareto
    assert top.score == score
    assert top.route_label() == route
    # And baggage was actually priced (not silently skipped).
    assert top.baggage is not None


# --- J. Serialization round trips -------------------------------------------
def test_price_none_survives_full_itinerary_round_trip():
    request = a_request(baggage=BaggageRequirement.CABIN_BAG)
    top = TravelPlanner().plan(request).recommendations[0]
    assert top.baggage.total_for_display is None
    restored = Itinerary.model_validate_json(top.model_dump_json())
    assert restored.baggage.total_for_display is None
    assert restored.baggage.completeness is PriceCompleteness.PARTIAL_UNKNOWN


def test_unknown_allowance_never_deserializes_as_included():
    unknown = BaggageAllowance.unknown(CABIN)
    restored = BaggageAllowance.model_validate(json.loads(unknown.model_dump_json()))
    assert restored.status is BaggageStatus.UNKNOWN
    assert restored.status is not BaggageStatus.INCLUDED
    assert restored.cost_is_known is False


def test_cost_breakdown_round_trips_with_baggage_component_intact():
    breakdown = CostBreakdown(transport=100.0, accommodation=200.0, ground_transfer=5.0, baggage=45.0)
    restored = CostBreakdown.model_validate_json(breakdown.model_dump_json())
    assert restored.baggage == 45.0
    # `total` is the fare-based figure and deliberately excludes baggage, so
    # that it always equals `Itinerary.total_cost`. This assertion originally
    # expected 350.0, encoding the very behaviour this file's own
    # `test_DEFECT_cost_breakdown_total_disagrees_with_itinerary_total_cost`
    # reported as a defect; the two could not both be satisfied, and the
    # defect test is the one describing correct behaviour.
    assert restored.total == pytest.approx(305.0)
    assert restored.total + restored.baggage == pytest.approx(350.0)


# --- K. Provider neutrality --------------------------------------------------
def test_baggage_models_and_pricing_import_no_provider_sdk():
    import ast
    import inspect

    for module in (
        __import__("detoura.models.baggage", fromlist=["*"]),
        __import__("detoura.services.baggage_pricing", fromlist=["*"]),
    ):
        source = inspect.getsource(module)
        tree = ast.parse(source)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        lowered = {n.lower() for n in names}
        for vendor in ("duffel", "amadeus", "sabre", "travelport"):
            assert vendor not in lowered, f"{module.__name__} names {vendor} as code, not prose"


# --- L. Validators: dishonest states must be unconstructible ----------------
@pytest.mark.parametrize(
    "kwargs",
    [
        dict(status=BaggageStatus.UNKNOWN, price_amount=0.0),
        dict(status=BaggageStatus.UNKNOWN, price_amount=25.0),
        dict(status=BaggageStatus.NOT_AVAILABLE, price_amount=10.0),
        dict(status=BaggageStatus.INCLUDED, price_amount=5.0),
    ],
)
def test_dishonest_allowance_states_are_rejected(kwargs):
    with pytest.raises(ValueError):
        BaggageAllowance(
            kind=CABIN, status=kwargs["status"],
            price=Money(amount=kwargs["price_amount"]),
        )


def test_negative_fee_is_rejected_at_the_money_layer():
    with pytest.raises(ValueError):
        Money(amount=-0.01)


def test_policy_slot_kind_mismatch_is_rejected():
    with pytest.raises(ValueError):
        BaggagePolicy(
            personal_item=BaggageAllowance.included(CABIN),  # wrong kind
            cabin_bag=BaggageAllowance.included(CABIN),
            checked_bag=BaggageAllowance.unknown(CHECKED),
        )


def test_negative_quantity_is_rejected():
    with pytest.raises(ValueError):
        BaggageAllowance(kind=CABIN, status=BaggageStatus.UNKNOWN, quantity=-1)


# --- Silent-provider default reads as three unknowns, never permission ----
def test_missing_policy_on_a_leg_is_three_unknowns_not_no_restrictions():
    silent_leg = leg(1, None)
    assert silent_leg.baggage is None
    resolved = policy_of(silent_leg)
    for kind in (BaggageKind.PERSONAL_ITEM, BaggageKind.CABIN_BAG, BaggageKind.CHECKED_BAG):
        allowance = resolved.allowance(kind)
        assert allowance.status is BaggageStatus.UNKNOWN
        assert allowance.cost_is_known is False


# ===========================================================================
# Re-verification regressions (Agent 5, post-fix): the P0-1 fix was reworked
# mid-review to add a fourth, distinct explanation factor
# (BAGGAGE_NOT_AVAILABLE) for the unsatisfiable case, which the original
# defect tests below only exercised at the BaggageQuote level, never through
# explanation_factors() on a full itinerary. These close that gap.
# ===========================================================================
def test_unbuyable_bag_gets_its_own_factor_not_cost_unknown_or_fits_budget():
    """An unsatisfiable requirement is a different fact from an unpriced one.

    `test_DEFECT_unavailable_bag_reads_as_a_known_free_total` proved the
    BaggageQuote itself no longer lies about this case. This proves the
    itinerary-level explanation agrees: it must name BAGGAGE_NOT_AVAILABLE,
    not the more generic BAGGAGE_COST_UNKNOWN (a bag that cannot be carried
    at any price is not "we don't know the fee"), and it must not also claim
    FITS_BUDGET or BAGGAGE_EXCEEDS_BUDGET - neither is provable when the
    traveller cannot bring the bag at all.
    """
    from detoura.models.itinerary import ExplanationFactor

    request = a_request(baggage=BaggageRequirement.CABIN_BAG)
    planner = TravelPlanner(
        transport_provider=SyntheticTransportDataProvider(
            baggage=BaggageScenario.CABIN_UNAVAILABLE
        )
    )
    top = planner.plan(request).recommendations[0]

    assert top.baggage.satisfiable is False, "fixture must actually be unbuyable"
    factors = top.explanation_factors
    assert ExplanationFactor.BAGGAGE_NOT_AVAILABLE in factors
    assert ExplanationFactor.BAGGAGE_COST_UNKNOWN not in factors
    assert ExplanationFactor.FITS_BUDGET not in factors
    assert ExplanationFactor.BAGGAGE_EXCEEDS_BUDGET not in factors


def test_budget_factor_is_always_exactly_one_of_the_four_baggage_states():
    """FITS_BUDGET, BAGGAGE_EXCEEDS_BUDGET, BAGGAGE_COST_UNKNOWN and
    BAGGAGE_NOT_AVAILABLE are meant to be mutually exclusive and jointly
    exhaustive - a trip is never left silent on affordability, and never
    claims two contradictory things about it at once. Checked across every
    scenario the synthetic provider knows, including the case with no
    baggage requirement at all (where only FITS_BUDGET-family logic applies
    since `baggage` is None).
    """
    from detoura.models.itinerary import ExplanationFactor

    budget_factors = {
        ExplanationFactor.FITS_BUDGET,
        ExplanationFactor.BAGGAGE_EXCEEDS_BUDGET,
        ExplanationFactor.BAGGAGE_COST_UNKNOWN,
        ExplanationFactor.BAGGAGE_NOT_AVAILABLE,
    }
    for scenario in BaggageScenario:
        for requirement in (
            BaggageRequirement.NONE,
            BaggageRequirement.CABIN_BAG,
            BaggageRequirement.CHECKED_BAG,
        ):
            request = a_request(baggage=requirement)
            planner = TravelPlanner(
                transport_provider=SyntheticTransportDataProvider(baggage=scenario)
            )
            top = planner.plan(request).recommendations[0]
            present = [f for f in top.explanation_factors if f in budget_factors]
            assert len(present) == 1, (
                f"scenario={scenario!r} requirement={requirement!r}: expected "
                f"exactly one budget factor, got {present!r}"
            )
