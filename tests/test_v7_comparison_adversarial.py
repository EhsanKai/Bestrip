"""V7 Phase 1, adversarial review (Agent 5, independent QA).

Written against `services/trip_comparison.py` and the V7 additions to
`services/baseline.py` with one goal: make the comparison say something that
is not true, and find out which of its stated guarantees actually hold.

Two kinds of test live here and they are deliberately not mixed.

**Guarantees that held.** Everything under "HELD" is a guarantee the module
claims and that survived a direct attempt to break it. These pass, and they are
worth as much as the failures: they are the fence around the parts of the
feature that are sound.

**Defects.** Everything under "DEFECT" asserts the behaviour the module's own
docstrings promise, and fails because the code does something else. They are
left failing on purpose. A defect test that is skipped or xfailed is a defect
that ships; the failure is the report.

Anchors were measured against a pristine ``c20fbf4`` checkout, so the
regression assertions here are comparisons, not recollections.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

import pytest

from detoura.config import PlannerConfig
from detoura.models.itinerary import (
    BaselineResult,
    CostBreakdown,
    Itinerary,
    TravelValueBreakdown,
)
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.profiles import ProfileName, get_profile
from detoura.providers.accommodation import (
    NoAccommodationProvider,
    SyntheticAccommodationDataProvider,
)
from detoura.providers.destinations import StaticDestinationProvider
from detoura.providers.ground_transfer import (
    FreeGroundTransferProvider,
    SyntheticGroundTransferProvider,
)
from detoura.providers.transport import SyntheticTransportDataProvider
from detoura.algorithms.travel_value import TravelValueScorer
from detoura.search_modes import SearchMode, apply_mode
from detoura.services.baseline import BaselinePlanner
from detoura.services.planner import TravelPlanner
from detoura.services.trip_comparison import (
    HOURS_EPSILON,
    PRICE_EPSILON,
    SCORE_EPSILON,
    TRANSIT_EPSILON,
    ComparisonVerdict,
    Favours,
    compare_trips,
)

from .conftest import leg

WINDOW_FROM = date(2026, 9, 5)
WINDOW_TO = date(2026, 9, 25)


# ---------------------------------------------------------------------------
# Builders. Deliberately separate from the implementer's: theirs bake in the
# assumption this review is testing, that a baseline has no ground transfers.
# ---------------------------------------------------------------------------
def a_baseline(**kw) -> BaselineResult:
    fields = dict(
        destination="Berlin",
        total_cost=400.0,
        duration_days=5.0,
        total_travel_minutes=300,
        usable_destination_minutes=3600,
        nights=4,
        scored=True,
        experience_score=0.60,
        preference_match=0.60,
        accommodation_score=0.60,
        convenience_score=0.70,
        travel_value=0.60,
        cost_breakdown=CostBreakdown(transport=400.0),
    )
    fields.update(kw)
    return BaselineResult(**fields)


def an_itinerary(**kw) -> Itinerary:
    cities = kw.pop("cities", ["Berlin"])
    breakdown = dict(
        cost=0.5,
        experience=kw.pop("experience", 0.60),
        preferences=kw.pop("preference", 0.60),
        time=0.5,
        diversity=0.5,
        accommodation=kw.pop("accommodation", 0.60),
        total=0.7,
    )
    departure = datetime(2026, 9, 10, 8, 0)
    fields = dict(
        rank=1,
        score=0.7,
        total_cost=400.0,
        duration_days=5.0,
        origin_airport="CGN",
        return_airport="CGN",
        cities=cities,
        legs=[leg("CGN", "Berlin", departure, 90, 50.0)],
        total_travel_minutes=300,
        ground_transfer_minutes=0,
        usable_destination_minutes=3600,
        departure=departure,
        arrival=datetime(2026, 9, 15, 20, 0),
        value_breakdown=TravelValueBreakdown(
            profile=ProfileName.BEST_VALUE, **breakdown
        ),
    )
    fields.update(kw)
    return Itinerary(**fields)


def a_request(**kw) -> TripRequest:
    fields = dict(
        origin="Köln",
        budget=1200.0,
        travelers=2,
        date_from=WINDOW_FROM,
        date_to=WINDOW_TO,
        duration_days=7,
        preferred_destinations=["Berlin"],
        preferences=TravelPreferences(history=0.9, food=0.8),
    )
    fields.update(kw)
    return TripRequest(**fields)


def planned(request: TripRequest, mode: SearchMode = SearchMode.QUICK):
    planner = TravelPlanner(config=apply_mode(PlannerConfig(), mode))
    return planner, planner.plan(request)


def baseline_transfer_minutes(planner: TravelPlanner, request: TripRequest,
                              baseline: BaselineResult) -> int:
    """Round-trip ground-transfer minutes the baseline itself paid for.

    Read from the same provider and the same airport the baseline used, and
    cross-checked against the money the baseline reports, so this is the
    baseline's own transfer rather than a plausible one.
    """
    airport = baseline.legs[0].origin
    options = planner.ground_transfer.search(request.origin, airport)
    if not options:
        return 0
    option = options[0]
    assert baseline.cost_breakdown.ground_transfer == pytest.approx(
        round(option.total_price(request.travelers) * 2, 2)
    ), "picked a different transfer from the one the baseline was priced with"
    return option.duration_minutes * 2


def hours(minutes: float) -> float:
    return round(minutes / 60.0, 1)


# ===========================================================================
# DEFECT 1 (HIGH) - the baseline's own ground transfers vanish from transit
#
# `compare_trips` reads `itinerary.total_transport_minutes` (intercity legs +
# ground transfers) for Detoura and `baseline.total_travel_minutes` (intercity
# legs only) for the traveler. The baseline pays for transfers - the money is
# in `cost_breakdown.ground_transfer` and the scored SearchState carries the
# minutes - but `BaselineResult` has no field to put the time in, so the time
# is discarded. The two sides of the transit axis are different quantities.
# ===========================================================================
def test_baseline_result_carries_the_transit_time_it_was_charged_for():
    """A baseline that pays for a transfer has spent time on it.

    The money survives into `cost_breakdown.ground_transfer`; the minutes are
    dropped on the floor, and the comparison then reads the surviving number as
    if it were the whole journey.
    """
    request = a_request(duration_days=5)
    planner, result = planned(request)
    baseline = result.baseline
    assert baseline is not None and baseline.cost_breakdown.ground_transfer > 0

    dropped = baseline_transfer_minutes(planner, request, baseline)
    assert dropped > 0

    legs_only = sum(option.duration_minutes for option in baseline.legs)
    # Resolved by mirroring `Itinerary` rather than by folding transfers into
    # `total_travel_minutes`. That field means intercity legs on the itinerary
    # side, so redefining it here would have swapped one asymmetry for another;
    # the transfers get their own field and a `total_transport_minutes` that
    # matches the itinerary property name for name.
    assert baseline.total_travel_minutes == legs_only
    assert baseline.ground_transfer_minutes == dropped
    assert baseline.total_transport_minutes == legs_only + dropped, (
        f"baseline reports {baseline.total_transport_minutes} door-to-door "
        f"minutes but actually spends {legs_only + dropped} ({dropped} of them "
        "on the ground transfers it was charged EUR "
        f"{baseline.cost_breakdown.ground_transfer} for)"
    )


def test_an_identical_route_is_not_reported_as_a_transit_difference():
    """The sharpest form of the defect, straight out of the real planner.

    `CGN -> Berlin -> CGN`: the recommendation is the baseline's own route,
    the same 150 minutes of flying, out of the same airport, with the same
    40-minute round-trip train. The comparison prints a transit gap anyway.
    """
    request = a_request(duration_days=7)
    planner, result = planned(request)
    baseline = result.baseline
    same_route = next(
        it for it in result.recommendations
        if it.cities == [baseline.destination]
        and it.total_travel_minutes == baseline.total_travel_minutes
    )
    dropped = baseline_transfer_minutes(planner, request, baseline)
    assert same_route.ground_transfer_minutes == dropped, "not the same journey"

    comparison = compare_trips(same_route, baseline)
    assert comparison.transit_time_delta == 0.0, (
        f"{same_route.route_label()} spends exactly as long in transit as the "
        f"baseline ({same_route.total_transport_minutes} min each), yet the "
        f"comparison reports a gap of {comparison.transit_time_delta}h"
    )


def test_a_verdict_never_rests_on_a_difference_that_does_not_exist():
    """Same request; here the phantom gap is the *whole* verdict.

    Observed: ORIGINAL_BETTER, summary "Your original Berlin trip wins on:
    0.7h less in transit." Expected: the trips are the same trip.
    """
    request = a_request(duration_days=7)
    planner, result = planned(request)
    baseline = result.baseline
    same_route = next(
        it for it in result.recommendations
        if it.cities == [baseline.destination]
        and it.total_travel_minutes == baseline.total_travel_minutes
    )
    comparison = compare_trips(same_route, baseline)
    assert comparison.verdict is not ComparisonVerdict.ORIGINAL_BETTER, (
        "the comparison declares the traveler's idea the winner on the "
        f"strength of {comparison.tradeoffs} - a difference of zero. "
        f"Summary as shipped: {comparison.summary!r}"
    )


def test_reported_transit_gap_matches_the_whole_journey():
    """Across the anchor scenario, on every rank, at once."""
    request = a_request(
        budget=900.0,
        date_from=date(2026, 9, 10),
        date_to=date(2026, 9, 24),
        duration_days=5,
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
    )
    planner, result = planned(request, SearchMode.SMART)
    baseline = result.baseline
    dropped = baseline_transfer_minutes(planner, request, baseline)

    wrong = []
    for itinerary in result.recommendations:
        comparison = compare_trips(itinerary, baseline)
        # Subtract first, round last. Rounding each side to a tenth of an hour
        # before subtracting is the artefact DEFECT 4 is about, so computing
        # the expectation that way would assert the bug back into place.
        truth = round(
            (itinerary.total_transport_minutes
             - (baseline.total_travel_minutes + dropped)) / 60.0,
            6,
        )
        if comparison.transit_time_delta != truth:
            wrong.append((itinerary.rank, comparison.transit_time_delta, truth))
    assert not wrong, (
        "reported vs whole-journey transit delta, per rank: "
        + "; ".join(f"rank{r}: said {s}h, truth {t}h" for r, s, t in wrong)
    )


# ===========================================================================
# DEFECT 2 (HIGH) - swapping the traveler's destination is called "adding"
#
# `dropped_original_destination` is computed and put on the DTO, then plays no
# part in the verdict, the tradeoffs, the unknowns or the prose. The prose
# instead says the alternative "adds" the cities that replaced it.
# ===========================================================================
def test_dropping_the_destination_appears_in_the_summary():
    """The traveler asked for Berlin. This trip does not go to Berlin."""
    comparison = compare_trips(
        an_itinerary(
            cities=["Prague", "Vienna"], usable_destination_minutes=5400,
            experience=0.80,
        ),
        a_baseline(destination="Berlin"),
    )
    assert comparison.dropped_original_destination is True
    assert "Berlin" in comparison.summary, (
        "the recommendation does not visit Berlin and the summary never says "
        f"so: {comparison.summary!r}"
    )


def test_dropping_the_destination_is_a_tradeoff_not_a_silent_flag():
    """A DETOURA_BETTER verdict that quietly deletes the destination.

    "Wins on something material, loses on nothing material" is what
    DETOURA_BETTER means. Not going where the traveler asked to go is a loss.
    """
    comparison = compare_trips(
        an_itinerary(
            cities=["Prague"], total_cost=300.0, total_travel_minutes=200,
            usable_destination_minutes=5400, experience=0.80,
            preference=0.80, accommodation=0.80,
        ),
        a_baseline(destination="Berlin"),
    )
    assert comparison.dropped_original_destination is True
    assert comparison.tradeoffs or comparison.verdict is not ComparisonVerdict.DETOURA_BETTER, (
        "verdict DETOURA_BETTER with an empty tradeoff list, on a trip that "
        f"never visits Berlin. Summary: {comparison.summary!r}"
    )


def test_prose_does_not_call_a_substitution_an_addition():
    """"adds Prague and Vienna" is a false statement when Berlin was removed.

    Adding is what happens on top of the traveler's idea. This replaced it.
    """
    comparison = compare_trips(
        an_itinerary(cities=["Prague", "Vienna"], usable_destination_minutes=5400),
        a_baseline(destination="Berlin"),
    )
    assert not (
        "adds" in comparison.summary and comparison.dropped_original_destination
    ), (
        "summary claims cities were added to a trip whose destination was "
        f"replaced: {comparison.summary!r}"
    )


# ===========================================================================
# DEFECT 3 (HIGH) - the two trips are different lengths and nobody is told
#
# `min_duration_utilization` is 0.6, so a 5-day request is happily baselined
# with a 3-night trip. Price and usable time are then compared across trips of
# different length, and the length is in neither the metrics, the summary nor
# the unknowns. baseline.py's own comment says the guard exists to stop
# exactly this ("a one-night 'five-day trip' would win on cost and make every
# comparison against it meaningless"); at 0.6 it does not stop it.
# ===========================================================================
def test_trip_length_is_comparable_or_disclosed():
    """Anchor request asks for 5 days. Baseline is 3.05, rank 1 is 4.81."""
    request = a_request(
        budget=900.0,
        date_from=date(2026, 9, 10),
        date_to=date(2026, 9, 24),
        duration_days=5,
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
    )
    _, result = planned(request, SearchMode.SMART)
    baseline = result.baseline
    top = result.recommendations[0]
    comparison = compare_trips(top, baseline)

    nights_apart = abs(sum(top.stay_days) - baseline.nights)
    disclosed = any(
        word in comparison.summary.lower() for word in ("night", "day", "longer")
    ) or any("night" in unknown or "length" in unknown for unknown in comparison.unknowns)
    assert nights_apart <= 1 or disclosed, (
        f"comparing a {baseline.nights}-night original against a "
        f"{sum(top.stay_days)}-night alternative and presenting "
        f"{comparison.usable_time_delta}h more in destinations as a win, with "
        f"no mention of the length difference: {comparison.summary!r}"
    )


# ===========================================================================
# DEFECT 4 (MEDIUM) - rounding promotes a sub-epsilon difference to material
#
# The module's own words: the epsilons "exist so a rounding artefact cannot
# become a sales pitch". `_compare_metric` rounds the delta to 6dp before
# testing it against the epsilon, and `_hours` rounds each side to 1dp before
# they are subtracted at all, so both directions of rounding can lift a
# genuinely immaterial difference over the line.
# ===========================================================================
@pytest.mark.parametrize(
    "metric,build,epsilon",
    [
        ("price", lambda d: (an_itinerary(total_cost=400.0 + d), a_baseline(total_cost=400.0)), PRICE_EPSILON),
        ("experience", lambda d: (an_itinerary(experience=0.60 + d), a_baseline(experience=0.60)), SCORE_EPSILON),
    ],
)
def test_a_difference_below_the_epsilon_is_never_material(metric, build, epsilon):
    delta = epsilon * (1 - 1e-7)
    assert delta < epsilon
    itinerary, baseline = build(delta)
    comparison = compare_trips(itinerary, baseline)
    found = next(m for m in comparison.metrics if m.metric == metric)
    assert not found.material, (
        f"{metric} difference of {delta!r} is below the {epsilon} epsilon, but "
        f"was rounded to {found.delta} and declared material - verdict "
        f"{comparison.verdict.value}, summary {comparison.summary!r}"
    )


def test_hour_rounding_does_not_invent_a_material_transit_gap():
    """27 real minutes apart, which is inside TRANSIT_EPSILON's 30."""
    baseline_minutes, detoura_minutes = 300, 327
    true_gap = (detoura_minutes - baseline_minutes) / 60.0
    assert true_gap < TRANSIT_EPSILON

    comparison = compare_trips(
        an_itinerary(total_travel_minutes=detoura_minutes),
        a_baseline(total_travel_minutes=baseline_minutes),
    )
    found = next(m for m in comparison.metrics if m.metric == "transit_hours")
    assert not found.material, (
        f"{detoura_minutes - baseline_minutes} minutes is {true_gap:.2f}h, "
        f"under the {TRANSIT_EPSILON}h epsilon, yet each side was rounded to "
        f"0.1h first and the gap became {found.delta}h - verdict "
        f"{comparison.verdict.value}: {comparison.summary!r}"
    )


# ===========================================================================
# DEFECT 5 (MEDIUM) - the unknown-is-not-zero guard is one-sided
#
# `compare_trips` refuses an unscored baseline because "zeros compared against
# real scores would manufacture a landslide out of a missing argument". The
# same hole on the itinerary side is undefended: `value_breakdown` is an
# optional field and its absence makes experience/preference/accommodation
# read as a hard 0.0, manufacturing the landslide in the other direction.
# ===========================================================================
def test_an_itinerary_without_scores_is_refused_like_an_unscored_baseline():
    assert compare_trips(an_itinerary(), a_baseline(scored=False)) is None

    comparison = compare_trips(an_itinerary(value_breakdown=None), a_baseline())
    assert comparison is None, (
        "an itinerary with no value_breakdown reports 0.0 for experience, "
        "preference and accommodation, and the comparison publishes those "
        "placeholders as findings: verdict "
        f"{comparison.verdict.value}, {comparison.summary!r}"
    )


# ===========================================================================
# HELD - the single-scorer guarantee. Attacked hard; did not move.
# ===========================================================================
@pytest.mark.parametrize(
    "label,travelers,duration,accommodation,transfers",
    [
        ("solo", 1, 5, True, True),
        ("pair", 2, 5, True, True),
        ("party of five", 5, 5, True, True),
        ("no accommodation provider", 2, 5, False, True),
        ("free ground transfer", 2, 5, True, False),
        ("neither", 2, 5, False, False),
        ("short trip", 2, 2, True, True),
        ("long multi-night trip", 2, 12, True, True),
    ],
)
def test_scored_state_is_the_same_trip_as_the_priced_result(
    label, travelers, duration, accommodation, transfers
):
    """`state_for` must describe the trip the traveler was quoted.

    Attacked with every shape the brief named. The state's cost, usable
    minutes, intercity minutes, night count and city list match the published
    `BaselineResult` in all of them, to the cent and to the minute.
    """
    config = PlannerConfig()
    destinations = StaticDestinationProvider()
    planner = BaselinePlanner(
        config,
        transport_provider=SyntheticTransportDataProvider(),
        destination_provider=destinations,
        accommodation_provider=(
            SyntheticAccommodationDataProvider()
            if accommodation
            else NoAccommodationProvider()
        ),
        ground_transfer_provider=(
            SyntheticGroundTransferProvider()
            if transfers
            else FreeGroundTransferProvider()
        ),
    )
    scorer = TravelValueScorer(config, destinations)
    request = a_request(budget=2500.0, travelers=travelers, duration_days=duration)
    dates = [date(2026, 9, 5 + offset) for offset in range(14)]

    captured: dict = {}
    original = BaselinePlanner.state_for

    def capture(self, candidate, req):
        state = original(self, candidate, req)
        captured["state"] = state
        return state

    BaselinePlanner.state_for = capture
    try:
        result = planner.compute(
            request,
            origin_airports=["CGN", "DUS", "FRA"],
            start_dates=dates,
            scorer=scorer,
            profile=get_profile("BEST_VALUE"),
        )
    finally:
        BaselinePlanner.state_for = original

    assert result is not None, f"{label}: no baseline to check"
    state = captured["state"]
    assert state.total_cost == result.total_cost, f"{label}: price diverged"
    assert state.usable_destination_minutes == result.usable_destination_minutes
    assert state.total_travel_minutes == result.total_travel_minutes
    assert list(state.cities) == [result.destination]
    assert state.stay_days[0] == result.nights
    assert state.completed is True


def test_the_room_that_was_scored_is_the_room_that_was_priced():
    """The provider contract says cheapest-first; the baseline books rooms[0].

    If those two ever disagree the scored stay would carry a premium the
    priced stay never paid. Checked against the provider's own ordering.
    """
    provider = SyntheticAccommodationDataProvider()
    rooms = provider.search("Berlin", date(2026, 9, 10), date(2026, 9, 14), 2)
    assert rooms
    assert rooms[0].total_price(2) == min(r.total_price(2) for r in rooms)


# ===========================================================================
# HELD - unknown never collapses into zero on the baseline side
# ===========================================================================
def test_an_unscored_baseline_cannot_produce_a_comparison_by_any_path():
    assert compare_trips(an_itinerary(), None) is None
    assert compare_trips(an_itinerary(), a_baseline(scored=False)) is None
    # The zeros an unscored baseline carries, made explicit and still refused.
    unscored = a_baseline(
        scored=False, experience_score=0.0, preference_match=0.0,
        accommodation_score=0.0, travel_value=0.0,
    )
    assert compare_trips(an_itinerary(), unscored) is None


def test_baggage_is_reported_unknown_and_disclosed_everywhere():
    for itinerary in (an_itinerary(), an_itinerary(total_cost=900.0)):
        comparison = compare_trips(itinerary, a_baseline())
        assert comparison.baggage_delta is None
        assert comparison.baggage_delta != 0.0
        assert "baggage" in comparison.unknowns
        assert "baggage" in comparison.summary
    # Including the verdict that says nothing separates the trips.
    same = compare_trips(an_itinerary(), a_baseline())
    assert same.verdict is ComparisonVerdict.EQUIVALENT
    assert "baggage" in same.summary


# ===========================================================================
# HELD - determinism
# ===========================================================================
def test_comparison_is_byte_identical_across_repeats():
    digests = set()
    for _ in range(25):
        comparison = compare_trips(
            an_itinerary(
                cities=["Prague", "Berlin", "Vienna", "Munich"],
                total_cost=555.5, total_travel_minutes=410,
                usable_destination_minutes=5000,
            ),
            a_baseline(total_cost=402.25),
        )
        digests.add(hashlib.sha256(comparison.model_dump_json().encode()).hexdigest())
    assert len(digests) == 1


def test_planner_level_comparisons_are_byte_identical_across_fresh_planners():
    """No dict or set iteration order leaks into metrics, prose or lists."""
    request = a_request(duration_days=5)
    digests = set()
    for _ in range(3):
        _, result = planned(request)
        blob = json.dumps(
            [compare_trips(i, result.baseline).model_dump()
             for i in result.recommendations],
            sort_keys=True, default=str,
        )
        digests.add(hashlib.sha256(blob.encode()).hexdigest())
    assert len(digests) == 1


# ===========================================================================
# HELD - materiality boundaries behave as documented (>= epsilon is material)
# ===========================================================================
@pytest.mark.parametrize(
    "delta,material",
    [(PRICE_EPSILON, True), (-PRICE_EPSILON, True), (PRICE_EPSILON - 0.01, False)],
)
def test_price_epsilon_boundary(delta, material):
    comparison = compare_trips(
        an_itinerary(total_cost=400.0 + delta), a_baseline(total_cost=400.0)
    )
    found = next(m for m in comparison.metrics if m.metric == "price")
    assert found.material is material
    if not material:
        assert found.favours is Favours.NEITHER
        assert comparison.verdict is ComparisonVerdict.EQUIVALENT


def test_an_exact_tie_is_equivalent_and_claims_nothing():
    comparison = compare_trips(an_itinerary(), a_baseline())
    assert comparison.verdict is ComparisonVerdict.EQUIVALENT
    assert comparison.advantages == []
    assert comparison.tradeoffs == []
    assert repr(comparison.price_delta) == "0.0"  # not -0.0
    assert all(m.favours is Favours.NEITHER for m in comparison.metrics)


def test_every_material_metric_lands_on_exactly_one_side():
    """Swept, not sampled: no metric is silently dropped from both lists."""
    for cost in (300.0, 400.0, 402.0, 405.0, 900.0):
        for usable in (1800, 3540, 3600, 3660, 7200):
            for transit in (200, 270, 300, 330, 700):
                comparison = compare_trips(
                    an_itinerary(
                        total_cost=cost, usable_destination_minutes=usable,
                        total_travel_minutes=transit,
                    ),
                    a_baseline(),
                )
                material = [m for m in comparison.metrics if m.material]
                assert len(material) == len(comparison.advantages) + len(
                    comparison.tradeoffs
                )
                assert all(
                    m.favours in (Favours.DETOURA, Favours.ORIGINAL) for m in material
                )
                if comparison.verdict is ComparisonVerdict.MIXED:
                    assert comparison.advantages and comparison.tradeoffs
                if comparison.verdict is ComparisonVerdict.DETOURA_BETTER:
                    assert not comparison.tradeoffs
                if comparison.verdict is ComparisonVerdict.ORIGINAL_BETTER:
                    assert not comparison.advantages
                for loss in comparison.tradeoffs:
                    assert "Your original" in comparison.summary


def test_hours_epsilon_is_the_documented_one():
    """Guards the constants the verdict logic hangs on."""
    assert (PRICE_EPSILON, HOURS_EPSILON, TRANSIT_EPSILON, SCORE_EPSILON) == (
        5.0, 1.0, 0.5, 0.02
    )


# ===========================================================================
# HELD - the verdict never contradicts the objective the product ranks by
#
# `travel_value` and `convenience_score` are computed for the baseline and
# then not compared. That is a real gap (see the report), but the failure mode
# it would enable - a DETOURA_BETTER verdict over an original that scores
# higher on the planner's own objective - could not be produced in 4,085 real
# comparisons. This test keeps a slice of that sweep.
# ===========================================================================
def test_detoura_better_never_contradicts_the_planners_own_objective():
    offenders = []
    for profile in list(ProfileName):
        for destination in ("Berlin", "Prague", "Vienna"):
            for budget in (500.0, 900.0):
                request = a_request(
                    budget=budget, duration_days=5,
                    preferred_destinations=[destination],
                )
                _, result = planned(request)
                baseline = result.baseline
                if baseline is None or not baseline.scored:
                    continue
                for itinerary in result.recommendations:
                    comparison = compare_trips(itinerary, baseline)
                    if (
                        comparison.verdict is ComparisonVerdict.DETOURA_BETTER
                        and baseline.travel_value > itinerary.score
                    ):
                        offenders.append(
                            f"{profile.value}/{destination}/{budget}/rank"
                            f"{itinerary.rank}: baseline {baseline.travel_value} > "
                            f"recommendation {itinerary.score}"
                        )
    assert not offenders, "; ".join(offenders)


# ===========================================================================
# HELD - hostile inputs do not crash the comparison
# ===========================================================================
@pytest.mark.parametrize(
    "label,overrides",
    [
        ("no cities", dict(cities=[])),
        ("no legs", dict(legs=[])),
        ("unicode city names", dict(cities=["Köln", "Zürich", "東京"])),
        ("zero usable minutes", dict(usable_destination_minutes=0)),
        ("zero cost", dict(total_cost=0.0)),
        ("enormous cost", dict(total_cost=1e15)),
        ("enormous transit", dict(total_travel_minutes=10**9)),
        ("many cities", dict(cities=[f"City{n}" for n in range(200)])),
    ],
)
def test_hostile_itineraries_do_not_crash(label, overrides):
    comparison = compare_trips(an_itinerary(**overrides), a_baseline())
    assert comparison is not None
    assert isinstance(comparison.summary, str) and comparison.summary
    assert comparison.model_dump_json()


def test_hostile_baselines_do_not_crash():
    for baseline in (
        a_baseline(destination="東京"),
        a_baseline(total_cost=0.0),
        a_baseline(usable_destination_minutes=0),
        a_baseline(total_travel_minutes=0),
        a_baseline(destination="Berlin", nights=0),
    ):
        comparison = compare_trips(an_itinerary(), baseline)
        assert comparison.model_dump_json()


def test_destination_matching_is_case_insensitive_both_ways():
    for typed, visited in (("Berlin", "berlin"), ("BERLIN", "Berlin"), ("köln", "KÖLN")):
        comparison = compare_trips(
            an_itinerary(cities=[visited]), a_baseline(destination=typed)
        )
        assert comparison.dropped_original_destination is False
        assert comparison.added_cities == []


# ===========================================================================
# HELD - Phase 1 is additive: no recommendation, price, route, score, Pareto
# size or state count moved. Anchors measured against a pristine c20fbf4.
# ===========================================================================
@pytest.mark.parametrize(
    "mode,duration,states,rejected,completed,pareto,top_score,top_route",
    [
        # Re-measured after `a_request` was corrected. The original anchors
        # here were taken while the helper passed a non-existent `interests=`
        # kwarg to TravelPreferences, which pydantic silently ignored - so they
        # described a default-0.5-preference search, not the anchor scenario
        # they were labelled with. These values come from a pristine c20fbf4
        # run of this exact request.
        (SearchMode.QUICK, 5, 1196, 692, 158, 13, 0.719869, "CGN -> Berlin -> Munich -> CGN"),
        (SearchMode.SMART, 5, 9730, 6804, 1076, 68, 0.734457, "CGN -> Berlin -> Munich -> CGN"),
        (SearchMode.SMART, 7, 10066, 6416, 1124, 110, 0.736198, "CGN -> Berlin -> Prague -> Vienna -> CGN"),
    ],
)
def test_search_is_unchanged_from_v65(
    mode, duration, states, rejected, completed, pareto, top_score, top_route
):
    request = a_request(
        budget=900.0, duration_days=duration,
        date_from=date(2026, 9, 10), date_to=date(2026, 9, 24),
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
    )
    _, result = planned(request, mode)
    assert result.metadata.states_generated == states
    assert result.metadata.states_rejected == rejected
    assert result.metadata.completed_itineraries == completed
    assert result.metadata.pareto_kept == pareto
    top = result.recommendations[0]
    assert top.score == top_score
    assert top.route_label() == top_route


def test_the_old_baseline_comparison_field_is_untouched():
    """The shipped frontend reads `baseline_comparison`. It still gets it.

    Values checked against the same request run on c20fbf4.
    """
    request = a_request(
        budget=900.0, duration_days=5,
        date_from=date(2026, 9, 10), date_to=date(2026, 9, 24),
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
    )
    _, result = planned(request, SearchMode.SMART)
    old = result.recommendations[0].baseline_comparison
    assert old is not None
    assert old.baseline_destination == "Berlin"
    assert old.baseline_cost == 277.5
    assert old.money_saved == round(277.5 - result.recommendations[0].total_cost, 2)
    assert old.additional_cities == len(result.recommendations[0].cities) - 1
    assert old.additional_travel_minutes == (
        result.recommendations[0].total_travel_minutes - 150
    )


def test_scoring_the_baseline_adds_fields_and_changes_nothing_else():
    """Scored and unscored baselines describe the identical trip."""
    request = a_request(duration_days=5)
    config = PlannerConfig()
    destinations = StaticDestinationProvider()
    planner = BaselinePlanner(
        config,
        transport_provider=SyntheticTransportDataProvider(),
        destination_provider=destinations,
        accommodation_provider=SyntheticAccommodationDataProvider(),
        ground_transfer_provider=SyntheticGroundTransferProvider(),
    )
    dates = [date(2026, 9, 5 + offset) for offset in range(14)]
    common = dict(origin_airports=["CGN", "DUS", "FRA"], start_dates=dates)

    unscored = planner.compute(request, **common)
    scored = planner.compute(
        request, **common,
        scorer=TravelValueScorer(config, destinations),
        profile=get_profile("BEST_VALUE"),
    )
    assert unscored.scored is False and scored.scored is True
    v7_only = {
        "scored", "experience_score", "preference_match", "accommodation_score",
        "convenience_score", "travel_value",
    }
    before = unscored.model_dump()
    after = scored.model_dump()
    assert {k for k in before if before[k] != after[k]} == v7_only


# ===========================================================================
# HELD - the API contract
# ===========================================================================
def test_api_serialises_both_comparison_fields(client):
    response = client.post(
        "/api/v1/search",
        json={
            "origin": "Köln", "budget": 900, "travelers": 2,
            "date_from": "2026-09-10", "date_to": "2026-09-24",
            "duration_days": 5, "preferred_destinations": ["Berlin"],
            "interests": ["history", "food", "culture"],
        },
    )
    assert response.status_code == 200
    body = json.loads(response.text)  # invalid JSON (NaN, Infinity) would raise
    recommendation = body["recommendations"][0]

    old = recommendation["baseline_comparison"]
    assert set(old) == {
        "destination", "total_price", "nights", "usable_hours", "price_delta",
        "extra_cities", "extra_usable_hours", "extra_travel_minutes",
    }

    new = recommendation["comparison"]
    assert new["verdict"] in {v.value for v in ComparisonVerdict}
    assert new["baggage_delta"] is None
    assert new["unknowns"] == ["baggage"]
    assert len(new["metrics"]) == 7
    for metric in new["metrics"]:
        assert metric["favours"] in {f.value for f in Favours}
        assert metric["delta"] == pytest.approx(
            round(metric["detoura"] - metric["original"], 6), abs=1e-6
        )
    if new["verdict"] == ComparisonVerdict.MIXED.value:
        assert new["advantages"] and new["tradeoffs"]


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from detoura.api.app import create_app

    return TestClient(create_app())
