"""Value for money: did the room premium earn its keep? (V4)

V3 scored room *quality* and deliberately left price out, which meant the
accommodation component could say a room was good but never whether it was
worth what it cost. This is the diagnostic that closes that gap - and it stays
a diagnostic: it is reported, never weighted, because price already enters
Travel Value through ``cost``.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from detoura.algorithms.accommodation_value import (
    NEUTRAL_VALUE_FOR_MONEY,
    VALUE_REFERENCE_RATE,
    AccommodationScorer,
)
from detoura.data.synthetic_accommodation import TIERS
from detoura.models.accommodation import AccommodationOption
from detoura.models.itinerary import ExplanationFactor
from detoura.models.trip import AccommodationPreference
from detoura.profiles import ProfileName

from .conftest import WINDOW_FROM, leg, make_state

CHECK_IN, CHECK_OUT = date(2026, 9, 10), date(2026, 9, 13)
BASE_RATE = 60.0


def tiered(tier_index: int, *, price: float | None = None) -> AccommodationOption:
    """One room from the synthetic tier ladder, at the standard base rate."""
    tier = TIERS[tier_index]
    return AccommodationOption(
        id=f"{tier.tier.value}-{price or tier.price_multiplier}",
        city="Prague",
        name=f"Prague {tier.tier.value}",
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        price_per_night=BASE_RATE * tier.price_multiplier if price is None else price,
        capacity=tier.capacity,
        tier=tier.tier,
        accommodation_type=tier.accommodation_type,
        rating=tier.rating,
        location_score=tier.location_score,
        free_cancellation=tier.free_cancellation,
    )


BUDGET, STANDARD, COMFORT = tiered(0), tiered(1), tiered(2)


def trip(booked: AccommodationOption, cheapest: AccommodationOption):
    """A one-city round trip that books ``booked`` when ``cheapest`` was offered."""
    return make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
            leg("Prague", "DUS", datetime(2026, 9, 13, 18, 0), 75, 55.0),
        ],
        travelers=2,
        rooms={"Prague": booked},
        cheapest_rooms={"Prague": cheapest},
    )


@pytest.fixture(scope="module")
def scorer() -> AccommodationScorer:
    return AccommodationScorer()


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------
def test_taking_the_cheapest_room_is_neutral_not_good(scorer):
    """No trade was made, so there is nothing to praise."""
    score, premium, gain = scorer.value_for_money(
        trip(BUDGET, BUDGET), AccommodationPreference.BALANCED
    )
    assert score == NEUTRAL_VALUE_FOR_MONEY
    assert premium == 0.0
    assert gain == 0.0


def test_a_stay_with_no_alternative_reports_no_premium():
    """One option on offer is not a premium of its whole price."""
    state = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
            leg("Prague", "DUS", datetime(2026, 9, 13, 18, 0), 75, 55.0),
        ],
        travelers=2,
        rooms={"Prague": COMFORT},  # no cheapest_rooms: nothing else was fetched
    )
    stay = state.stays[0]
    assert stay.cheapest_alternative is COMFORT
    assert stay.accommodation_premium == 0.0


def test_a_trip_with_no_rooms_at_all_is_neutral(scorer):
    state = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 8, 0), 75, 55.0),
            leg("Prague", "DUS", datetime(2026, 9, 13, 18, 0), 75, 55.0),
        ]
    )
    score, premium, _ = scorer.value_for_money(state, AccommodationPreference.BALANCED)
    assert score == NEUTRAL_VALUE_FOR_MONEY
    assert premium == 0.0


# ---------------------------------------------------------------------------
# The shape of the ladder
# ---------------------------------------------------------------------------
def test_the_first_step_up_is_worth_it_and_the_second_is_not(scorer):
    """The point of the diagnostic: not all upgrades are equal.

    budget -> standard buys a lot of quality for a little money; standard ->
    comfort buys less quality for more. A single "room quality" number cannot
    tell those apart, and this does.
    """
    first, _, _ = scorer.value_for_money(
        trip(STANDARD, BUDGET), AccommodationPreference.BALANCED
    )
    second, _, _ = scorer.value_for_money(
        trip(COMFORT, STANDARD), AccommodationPreference.BALANCED
    )
    assert first > NEUTRAL_VALUE_FOR_MONEY > second
    assert first > 0.8 and second < 0.35


def test_the_full_ladder_lands_on_neutral_by_construction(scorer):
    """VALUE_REFERENCE_RATE is calibrated to this trade, so it must hold.

    If the tier table changes and nobody re-derives the constant, this fails -
    which is the point of asserting it.
    """
    score, _, _ = scorer.value_for_money(
        trip(COMFORT, BUDGET), AccommodationPreference.BALANCED
    )
    assert score == pytest.approx(NEUTRAL_VALUE_FOR_MONEY, abs=0.03)


def test_the_reference_rate_matches_the_tier_table(scorer):
    """Guards the documented derivation of the constant itself."""
    gain = scorer.score_option(
        COMFORT, AccommodationPreference.BALANCED
    ) - scorer.score_option(BUDGET, AccommodationPreference.BALANCED)
    premium_per_night = COMFORT.price_per_night - BUDGET.price_per_night
    assert gain / premium_per_night == pytest.approx(VALUE_REFERENCE_RATE, rel=0.05)


# ---------------------------------------------------------------------------
# Monotonicity - the two properties the score owes
# ---------------------------------------------------------------------------
def test_a_cheaper_premium_for_the_same_quality_scores_better(scorer):
    """Monotone decreasing in price."""
    dear = tiered(1, price=BASE_RATE * 2.0)
    cheap = tiered(1, price=BASE_RATE * 1.2)
    dear_score, _, _ = scorer.value_for_money(
        trip(dear, BUDGET), AccommodationPreference.BALANCED
    )
    cheap_score, _, _ = scorer.value_for_money(
        trip(cheap, BUDGET), AccommodationPreference.BALANCED
    )
    assert cheap_score > dear_score


def test_more_quality_for_the_same_premium_scores_better(scorer):
    """Monotone increasing in quality."""
    same_price_comfort = tiered(2, price=STANDARD.price_per_night)
    better, _, _ = scorer.value_for_money(
        trip(same_price_comfort, BUDGET), AccommodationPreference.BALANCED
    )
    worse, _, _ = scorer.value_for_money(
        trip(STANDARD, BUDGET), AccommodationPreference.BALANCED
    )
    assert better > worse


def test_it_is_monotone_across_a_price_sweep(scorer):
    """No local reversals anywhere in the range."""
    scores = [
        scorer.value_for_money(
            trip(tiered(1, price=BASE_RATE + step), BUDGET),
            AccommodationPreference.BALANCED,
        )[0]
        for step in range(5, 60, 5)
    ]
    assert scores == sorted(scores, reverse=True), scores


def test_a_quality_seeker_values_the_same_upgrade_more(scorer):
    """The traveler's own preference is what the premium is judged against."""
    quality, _, _ = scorer.value_for_money(
        trip(STANDARD, BUDGET), AccommodationPreference.QUALITY
    )
    thrifty, _, _ = scorer.value_for_money(
        trip(STANDARD, BUDGET), AccommodationPreference.CHEAPEST
    )
    assert quality > thrifty


def test_the_score_stays_in_range(scorer):
    """Even for an absurd trade in either direction."""
    free_upgrade = tiered(2, price=BUDGET.price_per_night + 0.01)
    daylight_robbery = tiered(1, price=BASE_RATE * 40)
    high, _, _ = scorer.value_for_money(
        trip(free_upgrade, BUDGET), AccommodationPreference.BALANCED
    )
    low, _, _ = scorer.value_for_money(
        trip(daylight_robbery, BUDGET), AccommodationPreference.BALANCED
    )
    assert high == 1.0
    assert 0.0 <= low < 0.05


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def test_the_premium_is_a_party_total_over_every_stay(scorer):
    """Two travelers, three nights: the premium is what was actually paid."""
    state = trip(STANDARD, BUDGET)
    stay = state.stays[0]
    assert stay.nights == 3
    # One room sleeps two, so the party takes one room for three nights.
    expected = (STANDARD.price_per_night - BUDGET.price_per_night) * 3
    assert stay.accommodation_premium == pytest.approx(expected)
    _, premium, _ = scorer.value_for_money(state, AccommodationPreference.BALANCED)
    assert premium == pytest.approx(expected)


def test_the_assessment_carries_the_diagnostic(scorer):
    assessment = scorer.assess(trip(STANDARD, BUDGET), AccommodationPreference.BALANCED)
    direct = scorer.value_for_money(trip(STANDARD, BUDGET), AccommodationPreference.BALANCED)
    assert assessment.value_for_money == direct[0]
    assert assessment.premium == direct[1]
    assert assessment.premium_quality_gain == direct[2]


# ---------------------------------------------------------------------------
# It must not change what wins
# ---------------------------------------------------------------------------
def test_value_for_money_is_not_a_travel_value_component():
    """Price must not be scored twice. This is the rule the diagnostic obeys."""
    from detoura.profiles import COMPONENTS

    assert "value_for_money" not in COMPONENTS
    assert len(COMPONENTS) == 9


def test_the_accommodation_component_ignores_the_premium(planner, koln_request):
    """Same rooms, different baselines: quality is unchanged, the verdict isn't."""
    scorer = AccommodationScorer()
    dear_baseline = trip(STANDARD, BUDGET)
    no_baseline = trip(STANDARD, STANDARD)
    assert scorer.assess(
        dear_baseline, AccommodationPreference.BALANCED
    ).score == scorer.assess(no_baseline, AccommodationPreference.BALANCED).score
    assert scorer.assess(
        dear_baseline, AccommodationPreference.BALANCED
    ).value_for_money != scorer.assess(
        no_baseline, AccommodationPreference.BALANCED
    ).value_for_money


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def planned():
    from detoura.services.planner import TravelPlanner

    from .conftest import trip_request

    planner = TravelPlanner()
    return planner.plan(trip_request(budget=700, duration_days=6, travelers=2))


def test_every_stay_reports_what_the_cheapest_room_would_have_cost(planned):
    booked = [
        stay
        for itinerary in planned.recommendations
        for stay in itinerary.stays
        if stay.accommodation_cost > 0
    ]
    assert booked
    for stay in booked:
        assert stay.cheapest_alternative_cost is not None
        assert stay.cheapest_alternative_cost <= stay.accommodation_cost
        assert stay.accommodation_premium == pytest.approx(
            stay.accommodation_cost - stay.cheapest_alternative_cost, abs=0.01
        )


def test_the_breakdown_reports_the_diagnostic(planned):
    for itinerary in planned.recommendations:
        value = itinerary.value_breakdown
        assert 0.0 <= value.accommodation_value_for_money <= 1.0
        assert value.accommodation_premium >= 0.0
        assert value.accommodation_premium == pytest.approx(
            sum(stay.accommodation_premium for stay in itinerary.stays), abs=0.01
        )


def test_the_premium_is_explained_or_its_absence_is(planned):
    """Whatever happened, the traveler is told which of the three it was."""
    verdicts = {
        ExplanationFactor.ROOM_UPGRADE_WORTH_IT,
        ExplanationFactor.ROOM_UPGRADE_POOR_VALUE,
        ExplanationFactor.CHEAPEST_ROOMS_TAKEN,
    }
    for itinerary in planned.recommendations:
        if not any(stay.accommodation_cost > 0 for stay in itinerary.stays):
            continue
        stated = verdicts & set(itinerary.explanation_factors)
        # The dead zone around neutral deliberately claims nothing.
        marginal = 0.4 < itinerary.value_breakdown.accommodation_value_for_money < 0.6
        assert stated or marginal, itinerary.explanation_factors


def test_no_premium_means_the_cheapest_rooms_were_taken(planned):
    for itinerary in planned.recommendations:
        if itinerary.value_breakdown.accommodation_premium > 0:
            continue
        if not any(stay.accommodation_cost > 0 for stay in itinerary.stays):
            continue
        assert (
            ExplanationFactor.CHEAPEST_ROOMS_TAKEN in itinerary.explanation_factors
        )


def test_a_single_tier_search_never_pays_a_premium():
    """With one option per stay there is no trade to make, by construction."""
    from detoura.config import PlannerConfig
    from detoura.services.planner import TravelPlanner

    from .conftest import trip_request

    planner = TravelPlanner(config=PlannerConfig(accommodation_options_per_stay=1))
    result = planner.plan(trip_request(budget=700, duration_days=6))
    assert result.recommendations
    for itinerary in result.recommendations:
        assert itinerary.value_breakdown.accommodation_premium == 0.0
        assert itinerary.value_breakdown.accommodation_value_for_money == (
            NEUTRAL_VALUE_FOR_MONEY
        )


def test_it_reaches_the_api():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from detoura.api.app import create_app

    body = TestClient(create_app()).post(
        "/plan-trip",
        json={
            "origin": "Köln",
            "budget": 700,
            "travelers": 2,
            "duration_days": 6,
            "date_from": WINDOW_FROM.isoformat(),
            "date_to": "2026-09-18",
            "date_flexible": True,
            "transport_preferences": ["flight", "train"],
        },
    ).json()
    assert body["recommendations"]
    value = body["recommendations"][0]["value_breakdown"]
    assert "accommodation_value_for_money" in value
    assert "accommodation_premium" in value
    stay = body["recommendations"][0]["stays"][0]
    assert "accommodation_premium" in stay


def test_the_profile_still_decides_what_wins():
    """CHEAPEST must not start buying upgrades because they score well."""
    from detoura.services.planner import TravelPlanner

    from .conftest import trip_request

    planner = TravelPlanner()
    request = trip_request(budget=700, duration_days=6, travelers=2)
    cheapest = planner.plan(request, profile=ProfileName.CHEAPEST)
    best_value = planner.plan(request, profile=ProfileName.BEST_VALUE)
    assert cheapest.recommendations and best_value.recommendations
    assert (
        cheapest.recommendations[0].value_breakdown.accommodation_premium
        <= best_value.recommendations[0].value_breakdown.accommodation_premium
    )
