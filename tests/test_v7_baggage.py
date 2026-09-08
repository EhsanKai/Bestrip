"""V7 Phase 3: baggage truth.

The single thing these tests defend is that three statements stay apart:

    "I know this bag is free."
    "I know this bag costs 22 euros."
    "I do not know what this bag costs."

Most of what follows tries to collapse the third into the first or second,
because that collapse is the only way this feature can lie to somebody.
"""

from __future__ import annotations

import inspect
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
from detoura.models.itinerary import CostBreakdown
from detoura.models.money import Money
from detoura.models.transport import TransportOption, TransportType
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.providers.transport import SyntheticTransportDataProvider
from detoura.search_modes import SearchMode, apply_mode
from detoura.services.baggage_pricing import (
    describe,
    lower_bound_per_traveller,
    quote_trip,
)
from detoura.services.planner import TravelPlanner

WINDOW_FROM = date(2026, 9, 10)
WINDOW_TO = date(2026, 9, 24)
CABIN = BaggageKind.CABIN_BAG


def leg(index: int, cabin: BaggageAllowance | None, price: float = 50.0):
    policy = None
    if cabin is not None:
        policy = BaggagePolicy(
            personal_item=BaggageAllowance.included(BaggageKind.PERSONAL_ITEM),
            cabin_bag=cabin,
            checked_bag=BaggageAllowance.unknown(BaggageKind.CHECKED_BAG),
        )
    return TransportOption(
        id=f"L{index}", origin="A", destination="B",
        departure=datetime(2026, 9, 10, 8), arrival=datetime(2026, 9, 10, 10),
        price_per_person=price, transport_type=TransportType.FLIGHT, baggage=policy,
    )


# ---------------------------------------------------------------------------
# A. UNKNOWN IS NOT ZERO
# ---------------------------------------------------------------------------
def test_unknown_allowance_has_no_price_and_no_cost():
    allowance = BaggageAllowance.unknown(CABIN)
    assert allowance.price is None
    assert allowance.extra_cost_per_traveller is None
    assert allowance.cost_is_known is False


def test_unknown_allowance_cannot_carry_a_price_at_all():
    """The type refuses it, so no caller can construct the lie."""
    with pytest.raises(ValueError, match="UNKNOWN allowance cannot carry a price"):
        BaggageAllowance(kind=CABIN, status=BaggageStatus.UNKNOWN,
                         price=Money(amount=0.0))
    with pytest.raises(ValueError, match="UNKNOWN allowance cannot carry a price"):
        BaggageAllowance(kind=CABIN, status=BaggageStatus.UNKNOWN,
                         price=Money(amount=25.0))


def test_unknown_is_not_unavailable():
    """Assuming the worst invents a fact just as much as assuming the best."""
    assert BaggageStatus.UNKNOWN is not BaggageStatus.NOT_AVAILABLE
    quote = quote_trip([leg(1, BaggageAllowance.unknown(CABIN))],
                       BaggageRequirement.CABIN_BAG, travelers=1)
    assert quote.unknown_legs == 1
    assert quote.unavailable_legs == 0
    assert quote.satisfiable is True


def test_a_missing_policy_reads_as_unknown_not_as_permission():
    """`baggage=None` is silence, not "no restrictions"."""
    quote = quote_trip([leg(1, None)], BaggageRequirement.CABIN_BAG, travelers=1)
    assert quote.completeness is PriceCompleteness.PARTIAL_UNKNOWN
    assert quote.unknown_legs == 1


def test_unknown_total_is_never_displayable():
    quote = quote_trip(
        [leg(1, BaggageAllowance.extra(CABIN, 20.0)),
         leg(2, BaggageAllowance.unknown(CABIN))],
        BaggageRequirement.CABIN_BAG, travelers=1,
    )
    assert quote.known_total == 20.0
    assert quote.total_for_display is None, "a partial sum must not read as a total"
    assert "unquoted" in describe(quote)


# ---------------------------------------------------------------------------
# B. KNOWN FREE, distinguishable from unknown
# ---------------------------------------------------------------------------
def test_included_is_a_known_zero():
    allowance = BaggageAllowance.included(CABIN)
    assert allowance.cost_is_known is True
    assert allowance.extra_cost_per_traveller.amount == 0.0


def test_included_and_unknown_are_different_states_with_the_same_total():
    """Both cost nothing so far; only one of them is a price."""
    free = quote_trip([leg(1, BaggageAllowance.included(CABIN))],
                      BaggageRequirement.CABIN_BAG, travelers=2)
    silent = quote_trip([leg(1, BaggageAllowance.unknown(CABIN))],
                        BaggageRequirement.CABIN_BAG, travelers=2)
    assert free.known_total == silent.known_total == 0.0
    assert free.is_complete and not silent.is_complete
    assert free.total_for_display == 0.0
    assert silent.total_for_display is None


def test_included_cannot_carry_a_nonzero_price():
    with pytest.raises(ValueError, match="costs nothing extra"):
        BaggageAllowance(kind=CABIN, status=BaggageStatus.INCLUDED,
                         price=Money(amount=15.0))


def test_extra_without_a_quote_is_known_to_cost_but_not_how_much():
    """A real shape: "cabin bag not included", no fee stated."""
    allowance = BaggageAllowance.extra_unpriced(CABIN)
    assert allowance.status is BaggageStatus.EXTRA
    assert allowance.cost_is_known is False
    assert allowance.extra_cost_per_traveller is None


# ---------------------------------------------------------------------------
# C / D. Arithmetic: per leg, per traveller, no double counting
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("travelers", [1, 2, 5])
@pytest.mark.parametrize("legs", [1, 2, 4])
def test_known_fees_multiply_by_leg_and_by_traveller(travelers, legs):
    trip = [leg(i, BaggageAllowance.extra(CABIN, 20.0)) for i in range(legs)]
    quote = quote_trip(trip, BaggageRequirement.CABIN_BAG, travelers=travelers)
    assert quote.known_total == pytest.approx(20.0 * legs * travelers)
    assert quote.is_complete


def test_the_brief_example_c_partial_multi_leg():
    """Leg A EUR 20, leg B included, leg C unquoted."""
    quote = quote_trip(
        [leg(1, BaggageAllowance.extra(CABIN, 20.0)),
         leg(2, BaggageAllowance.included(CABIN)),
         leg(3, BaggageAllowance.unknown(CABIN))],
        BaggageRequirement.CABIN_BAG, travelers=1,
    )
    assert quote.known_total == 20.0
    assert quote.completeness is PriceCompleteness.PARTIAL_UNKNOWN
    assert quote.unknown_legs == 1
    assert quote.total_for_display is None


def test_baggage_is_its_own_component_and_stays_out_of_the_ranked_total():
    """`total` is the fare-based figure the optimizer ranked on.

    This assertion originally expected baggage inside `total`. Folding it in
    gave one trip two different totals - the ranked figure and a larger
    reported one, with the named components summing to neither - so baggage is
    now reported alongside and `total` always equals `Itinerary.total_cost`.
    """
    breakdown = CostBreakdown(transport=100.0, accommodation=50.0,
                              ground_transfer=10.0, baggage=20.0)
    assert breakdown.transport == 100.0, "never folded into the fare"
    assert breakdown.total == 160.0
    assert breakdown.total + breakdown.baggage == 180.0
    without = CostBreakdown(transport=100.0, accommodation=50.0, ground_transfer=10.0)
    assert breakdown.total == without.total


def test_the_fare_itself_never_absorbs_baggage():
    option = leg(1, BaggageAllowance.extra(CABIN, 20.0), price=50.0)
    assert option.price_per_person == 50.0


def test_no_requirement_means_nothing_is_owed_or_unknown():
    quote = quote_trip([leg(1, None)], BaggageRequirement.NONE, travelers=3)
    assert quote.known_total == 0.0
    assert quote.is_complete
    assert quote.unknown_legs == 0


def test_unavailable_is_reported_separately_from_unknown():
    unavailable = BaggageAllowance(kind=CABIN, status=BaggageStatus.NOT_AVAILABLE)
    quote = quote_trip([leg(1, unavailable)], BaggageRequirement.CABIN_BAG, travelers=1)
    assert quote.unavailable_legs == 1
    assert quote.unknown_legs == 0
    assert quote.satisfiable is False
    assert "cannot be added" in describe(quote)


# ---------------------------------------------------------------------------
# G. Admissible bounds: the asymmetry
# ---------------------------------------------------------------------------
def test_unknown_contributes_zero_to_the_lower_bound():
    """Zero is a *true* lower bound for a fee nobody quoted.

    Guessing any positive figure would make the bound overestimate, and the
    search would prune trips the traveller could actually afford.
    """
    trip = [leg(1, BaggageAllowance.extra(CABIN, 20.0)),
            leg(2, BaggageAllowance.unknown(CABIN))]
    assert lower_bound_per_traveller(trip, BaggageRequirement.CABIN_BAG) == 20.0


def test_the_lower_bound_never_exceeds_the_known_total_per_traveller():
    trip = [leg(1, BaggageAllowance.extra(CABIN, 20.0)),
            leg(2, BaggageAllowance.included(CABIN)),
            leg(3, BaggageAllowance.unknown(CABIN))]
    bound = lower_bound_per_traveller(trip, BaggageRequirement.CABIN_BAG)
    quote = quote_trip(trip, BaggageRequirement.CABIN_BAG, travelers=1)
    assert bound <= quote.known_total


def test_lower_bound_is_zero_without_a_requirement():
    trip = [leg(1, BaggageAllowance.extra(CABIN, 99.0))]
    assert lower_bound_per_traveller(trip, BaggageRequirement.NONE) == 0.0


# ---------------------------------------------------------------------------
# I. Backward compatibility - the whole phase must be additive
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mode,duration,states,rejected,completed,pareto,score,route",
    [
        ("QUICK", 5, 1196, 692, 158, 13, 0.719869, "CGN -> Berlin -> Munich -> CGN"),
        ("SMART", 5, 9730, 6804, 1076, 68, 0.734457, "CGN -> Berlin -> Munich -> CGN"),
        ("SMART", 7, 10066, 6416, 1124, 110, 0.736198,
         "CGN -> Berlin -> Prague -> Vienna -> CGN"),
    ],
)
def test_no_baggage_request_reproduces_the_golden_signatures(
    mode, duration, states, rejected, completed, pareto, score, route
):
    request = TripRequest(
        origin="Köln", budget=900.0, travelers=2, duration_days=duration,
        date_from=WINDOW_FROM, date_to=WINDOW_TO, preferred_destinations=["Berlin"],
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
    )
    result = TravelPlanner(config=apply_mode(PlannerConfig(), SearchMode(mode))).plan(request)
    meta, top = result.metadata, result.recommendations[0]
    assert meta.states_generated == states
    assert meta.states_rejected == rejected
    assert meta.completed_itineraries == completed
    assert meta.pareto_kept == pareto
    assert top.score == score
    assert top.route_label() == route
    assert top.baggage is None
    assert top.cost_breakdown.baggage == 0.0


def test_synthetic_fares_report_baggage_as_unknown_by_default():
    """The honest default, and the one that keeps the unknown path exercised."""
    provider = SyntheticTransportDataProvider()
    option = provider.search("CGN", "Berlin", WINDOW_FROM)[0]
    assert option.baggage is None


def test_a_baggage_request_does_not_move_the_search():
    """Naming a bag changes what we claim about price, not what we find."""
    base = dict(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=WINDOW_FROM, date_to=WINDOW_TO, preferred_destinations=["Berlin"],
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
    )
    plain = TravelPlanner().plan(TripRequest(**base))
    with_bag = TravelPlanner().plan(
        TripRequest(**base, baggage=BaggageRequirement.CABIN_BAG)
    )
    assert [r.route_label() for r in plain.recommendations] == [
        r.route_label() for r in with_bag.recommendations
    ]
    assert plain.metadata.states_generated == with_bag.metadata.states_generated
    assert with_bag.recommendations[0].baggage is not None
    assert with_bag.recommendations[0].total_cost == plain.recommendations[0].total_cost


def test_a_requested_bag_on_silent_fares_is_partial_unknown():
    """The default catalog says nothing, so a requirement cannot be priced."""
    request = TripRequest(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=WINDOW_FROM, date_to=WINDOW_TO,
        baggage=BaggageRequirement.CABIN_BAG,
    )
    top = TravelPlanner().plan(request).recommendations[0]
    assert top.baggage.completeness is PriceCompleteness.PARTIAL_UNKNOWN
    assert top.baggage.total_for_display is None
    assert top.total_with_known_baggage == top.total_cost


def test_known_fees_reach_the_itinerary_total_without_touching_the_fare():
    request = TripRequest(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=WINDOW_FROM, date_to=WINDOW_TO,
        baggage=BaggageRequirement.CABIN_BAG,
    )
    planner = TravelPlanner(
        transport_provider=SyntheticTransportDataProvider(
            baggage=BaggageScenario.PERSONAL_ONLY
        )
    )
    top = planner.plan(request).recommendations[0]
    assert top.baggage.is_complete
    expected = 20.0 * len(top.legs) * request.travelers
    assert top.baggage.known_total == pytest.approx(expected)
    assert top.cost_breakdown.baggage == pytest.approx(expected)
    assert top.total_with_known_baggage == pytest.approx(top.total_cost + expected)


# ---------------------------------------------------------------------------
# K. Provider neutrality
# ---------------------------------------------------------------------------
def test_no_provider_is_coupled_into_the_baggage_layer():
    """Phase 3 must not depend on whatever V8 integrates.

    Checked against the parsed code rather than the raw text: these modules
    name Duffel and Amadeus in prose precisely to say they are *not* coupled to
    them, and a substring search would fail on the sentence promising the thing
    it is testing for. Imports, identifiers and attributes are what coupling
    actually looks like.
    """
    import ast

    from detoura.models import baggage as baggage_model
    from detoura.services import baggage_pricing

    vendors = ("duffel", "amadeus", "kiwi", "skyscanner")
    for module in (baggage_model, baggage_pricing):
        tree = ast.parse(inspect.getsource(module))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
                names += [alias.name for alias in node.names]
            elif isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
        joined = " ".join(names).lower()
        for vendor in vendors:
            assert vendor not in joined, f"{module.__name__} is coupled to {vendor}"


# ---------------------------------------------------------------------------
# L. Properties
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fee", [0.01, 1.0, 19.99, 45.0, 250.0])
def test_money_precision_is_exact_across_legs_and_party(fee):
    trip = [leg(i, BaggageAllowance.extra(CABIN, fee)) for i in range(3)]
    quote = quote_trip(trip, BaggageRequirement.CABIN_BAG, travelers=2)
    assert quote.known_total == pytest.approx(round(fee * 3 * 2, 2), abs=0.005)


def test_a_negative_fee_is_impossible():
    with pytest.raises(ValueError):
        BaggageAllowance.extra(CABIN, -5.0)


def test_quote_requires_at_least_one_traveller():
    with pytest.raises(ValueError, match="travelers must be >= 1"):
        quote_trip([leg(1, None)], BaggageRequirement.CABIN_BAG, travelers=0)


def test_quotes_are_deterministic():
    trip = [leg(1, BaggageAllowance.extra(CABIN, 20.0)),
            leg(2, BaggageAllowance.unknown(CABIN))]
    first = quote_trip(trip, BaggageRequirement.CABIN_BAG, travelers=2)
    second = quote_trip(trip, BaggageRequirement.CABIN_BAG, travelers=2)
    assert first.model_dump() == second.model_dump()
    assert describe(first) == describe(second)


def test_not_required_quote_is_complete_and_empty():
    quote = BaggageQuote.not_required()
    assert quote.requirement is BaggageRequirement.NONE
    assert quote.is_complete and quote.known_total == 0.0
