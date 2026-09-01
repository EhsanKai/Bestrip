"""Accommodation as a real price/quality trade-off."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from travel_planner.algorithms.accommodation_value import (
    TYPE_FIT,
    AccommodationScorer,
)
from travel_planner.algorithms.travel_value import TravelValueScorer
from travel_planner.config import PlannerConfig
from travel_planner.models.accommodation import (
    AccommodationOption,
    AccommodationTier,
    AccommodationType,
    rating_from_stars,
)
from travel_planner.models.trip import AccommodationPreference
from travel_planner.profiles import PROFILES, ProfileName
from travel_planner.providers.accommodation import SyntheticAccommodationDataProvider
from travel_planner.services.planner import TravelPlanner

from .conftest import leg, make_state, trip_request

CHECK_IN, CHECK_OUT = date(2026, 9, 10), date(2026, 9, 12)


def hotel(price: float, stars: float, location: float, **kwargs) -> AccommodationOption:
    return AccommodationOption(
        id=f"h-{price:g}-{stars:g}",
        city="Prague",
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        price_per_night=price,
        rating=rating_from_stars(stars),
        location_score=location,
        **kwargs,
    )


@pytest.fixture
def scorer() -> AccommodationScorer:
    return AccommodationScorer()


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------
def test_stars_convert_to_the_internal_scale():
    assert rating_from_stars(5.0) == 1.0
    assert rating_from_stars(4.6) == pytest.approx(0.92)
    assert hotel(65.0, 4.6, 0.92).stars == 4.6
    with pytest.raises(ValueError, match="0..5"):
        rating_from_stars(7.0)


def test_options_carry_quality_attributes():
    option = hotel(
        65.0, 4.6, 0.92,
        accommodation_type=AccommodationType.BOUTIQUE,
        free_cancellation=True,
    )
    assert option.location_score == 0.92
    assert option.accommodation_type is AccommodationType.BOUTIQUE
    assert option.free_cancellation


# ---------------------------------------------------------------------------
# The synthetic ladder
# ---------------------------------------------------------------------------
def test_tiers_buy_real_quality(accommodation):
    options = accommodation.search("Prague", CHECK_IN, CHECK_OUT, 2)
    by_tier = {option.tier: option for option in options}
    assert set(by_tier) == set(AccommodationTier)

    budget = by_tier[AccommodationTier.BUDGET]
    comfort = by_tier[AccommodationTier.COMFORT]
    assert comfort.rating > budget.rating
    assert comfort.location_score > budget.location_score
    assert comfort.price_per_night > budget.price_per_night


def test_price_rises_faster_than_quality(accommodation):
    """Paying more must be a trade-off, not a free upgrade."""
    options = accommodation.search("Prague", CHECK_IN, CHECK_OUT, 2)
    by_tier = {option.tier: option for option in options}
    budget = by_tier[AccommodationTier.BUDGET]
    comfort = by_tier[AccommodationTier.COMFORT]
    price_ratio = comfort.price_per_night / budget.price_per_night
    quality_ratio = comfort.rating / budget.rating
    assert price_ratio > quality_ratio


def test_weekend_nights_cost_more(accommodation):
    """2026-09-11 is a Friday."""
    midweek = accommodation.search("Prague", date(2026, 9, 8), date(2026, 9, 9), 2)[0]
    weekend = accommodation.search("Prague", date(2026, 9, 11), date(2026, 9, 12), 2)[0]
    assert weekend.price_per_night > midweek.price_per_night


# ---------------------------------------------------------------------------
# The score
# ---------------------------------------------------------------------------
def test_quality_rises_with_rating_and_location(scorer):
    poor = scorer.score_option(hotel(45.0, 3.5, 0.60), AccommodationPreference.BALANCED)
    good = scorer.score_option(hotel(65.0, 4.6, 0.92), AccommodationPreference.BALANCED)
    assert good > poor


def test_price_is_not_part_of_the_score(scorer):
    """Cost has its own component; double-counting it would bias the trade."""
    cheap = hotel(45.0, 4.2, 0.7)
    same_but_dear = cheap.model_copy(update={"price_per_night": 400.0})
    preference = AccommodationPreference.BALANCED
    assert scorer.score_option(cheap, preference) == scorer.score_option(
        same_but_dear, preference
    )


def test_type_fit_follows_the_stated_preference(scorer):
    hostel = hotel(30.0, 3.5, 0.6, accommodation_type=AccommodationType.HOSTEL)
    boutique = hotel(120.0, 4.7, 0.9, accommodation_type=AccommodationType.BOUTIQUE)
    assert scorer.score_option(hostel, AccommodationPreference.CHEAPEST) > (
        scorer.score_option(hostel, AccommodationPreference.QUALITY)
    )
    assert scorer.score_option(boutique, AccommodationPreference.QUALITY) > (
        scorer.score_option(boutique, AccommodationPreference.CHEAPEST)
    )
    assert set(TYPE_FIT) == set(AccommodationPreference)


def test_free_cancellation_is_worth_something(scorer):
    rigid = hotel(60.0, 4.0, 0.7, free_cancellation=False)
    flexible = rigid.model_copy(update={"free_cancellation": True})
    preference = AccommodationPreference.BALANCED
    assert scorer.score_option(flexible, preference) > scorer.score_option(
        rigid, preference
    )


def test_stays_are_weighted_by_nights(scorer):
    """Three nights in a good room outweigh one night in a poor one."""
    from .conftest import room

    good = room("Prague", date(2026, 9, 10), date(2026, 9, 13), 50.0).model_copy(
        update={"rating": 0.9, "location_score": 0.9}
    )
    poor = room("Vienna", date(2026, 9, 13), date(2026, 9, 14), 50.0).model_copy(
        update={"rating": 0.3, "location_score": 0.3}
    )
    state = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 75, 40.0),
            leg("Prague", "Vienna", datetime(2026, 9, 13, 10, 0), 240, 20.0),
            leg("Vienna", "DUS", datetime(2026, 9, 14, 18, 0), 95, 30.0),
        ],
        rooms={"Prague": good, "Vienna": poor},
    )
    assessment = scorer.assess(state, AccommodationPreference.BALANCED)
    assert assessment.nights == 4
    assert assessment.booked_stays == 2
    # Weighted towards the three good nights, so above the plain mean.
    assert assessment.rating > (0.9 + 0.3) / 2


def test_no_accommodation_is_neutral_not_bad(scorer):
    state = make_state(
        [
            leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 75, 40.0),
            leg("Prague", "DUS", datetime(2026, 9, 14, 18, 0), 75, 40.0),
        ]
    )
    assert scorer.assess(state, AccommodationPreference.BALANCED).score == 0.5


# ---------------------------------------------------------------------------
# The trade-off, end to end (spec sections 5 and 6)
# ---------------------------------------------------------------------------
def test_a_dearer_better_room_does_not_automatically_win(config, destinations):
    """A 120/night 4.8-star room must not beat a 60/night 4.4-star one for free.

    The upgrade has to pay for itself against BudgetEfficiency, which is the
    trade-off the spec asks for rather than a rule that says "cheaper wins" or
    "better wins".
    """
    from .conftest import room

    value = TravelValueScorer(config, destinations)
    request = trip_request(travelers=1, budget=400, preferred_destinations=[])
    profile = PROFILES[ProfileName.BEST_VALUE]

    def trip(nightly: float, rating: float, location: float):
        stay = room("Prague", date(2026, 9, 10), date(2026, 9, 14), nightly).model_copy(
            update={"rating": rating, "location_score": location}
        )
        return make_state(
            [
                leg("DUS", "Prague", datetime(2026, 9, 10, 9, 0), 75, 40.0),
                leg("Prague", "DUS", datetime(2026, 9, 14, 18, 0), 75, 40.0),
            ],
            rooms={"Prague": stay},
        )

    modest = trip(60.0, rating_from_stars(4.4), 0.70)
    lavish = trip(120.0, rating_from_stars(4.8), 0.85)

    # The lavish room genuinely is better ...
    assert value.accommodation_score(lavish, request) > value.accommodation_score(
        modest, request
    )
    # ... but 240 extra euros do not buy enough to win overall.
    assert value.total(lavish, request, profile) < value.total(modest, request, profile)

    # The same quality gain for 3 euros a night, though, is worth taking - so
    # the model is trading, not simply preferring cheap or preferring good.
    slight = trip(63.0, rating_from_stars(4.8), 0.85)
    assert value.total(slight, request, profile) > value.total(modest, request, profile)

    # And there is a genuine break-even between the two.
    prices = [60.0, 70.0, 80.0, 90.0, 100.0, 120.0]
    totals = [
        value.total(trip(p, rating_from_stars(4.8), 0.85), request, profile)
        for p in prices
    ]
    assert totals == sorted(totals, reverse=True), "paying more must cost score"
    assert totals[0] > value.total(modest, request, profile) > totals[-1]


def test_quality_seekers_and_bargain_hunters_get_different_rooms():
    """The accommodation preference has to change the answer."""
    planner = TravelPlanner(config=PlannerConfig(accommodation_options_per_stay=3))
    base = dict(budget=800, preferred_destinations=[], preferred_city_count=1)
    thrifty = planner.plan(
        trip_request(**base, accommodation_preference=AccommodationPreference.CHEAPEST),
        profile=ProfileName.CHEAPEST,
    )
    fussy = planner.plan(
        trip_request(**base, accommodation_preference=AccommodationPreference.QUALITY),
        profile=ProfileName.BEST_VALUE,
    )
    assert thrifty.recommendations and fussy.recommendations
    assert fussy.recommendations[0].value_breakdown.accommodation_rating > (
        thrifty.recommendations[0].value_breakdown.accommodation_rating
    )


def test_the_search_branches_across_tiers():
    """V3's default gives the optimizer more than one room to choose from."""
    assert PlannerConfig().accommodation_options_per_stay >= 2

    from .conftest import completed_states

    planner = TravelPlanner(config=PlannerConfig(accommodation_options_per_stay=3))
    request = trip_request(budget=800, preferred_city_count=1)
    tiers = {
        stay.accommodation.tier
        for state in completed_states(planner, request)
        for stay in state.stays
        if stay.accommodation is not None
    }
    assert len(tiers) >= 2, f"expected several tiers explored, saw {tiers}"


def test_one_option_per_stay_reproduces_the_v2_behaviour():
    """Always the cheapest room - no trade-off available."""
    from .conftest import completed_states

    planner = TravelPlanner(config=PlannerConfig(accommodation_options_per_stay=1))
    request = trip_request(budget=800, preferred_city_count=1)
    provider = SyntheticAccommodationDataProvider()
    for state in completed_states(planner, request)[:40]:
        for stay in state.stays:
            if stay.accommodation is None:
                continue
            cheapest = provider.search(
                stay.city, stay.arrival.date(), stay.departure.date(), request.travelers
            )[0]
            assert stay.accommodation.tier is cheapest.tier


def test_accommodation_quality_reaches_the_itinerary(planner):
    result = planner.plan(trip_request(budget=650))
    for itinerary in result.recommendations:
        value = itinerary.value_breakdown
        assert 0.0 <= itinerary.accommodation_score <= 1.0
        assert value.accommodation_rating > 0
        assert value.accommodation_location > 0
        for stay in itinerary.stays:
            assert stay.accommodation_rating is not None
            assert stay.accommodation_type in {t.value for t in AccommodationType}
