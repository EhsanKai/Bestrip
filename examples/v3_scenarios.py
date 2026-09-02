"""The V3 demonstration: three scenarios x three profiles.

    python examples/v3_scenarios.py            # all three scenarios
    python examples/v3_scenarios.py --budget-sensitivity
    python examples/v3_scenarios.py --baseline

All prices, hotels, transfers and destination metadata are synthetic.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import date, timedelta

from detoura import TravelPlanner, TravelPreferences, TripRequest
from detoura.profiles import PROFILES, ProfileName
from detoura.services.budget_sensitivity import analyze_budget_sensitivity


@dataclass(frozen=True)
class Scenario:
    label: str
    budget: float
    duration_days: int


SCENARIOS = (
    Scenario("A", 250.0, 5),
    Scenario("B", 450.0, 5),
    Scenario("C", 600.0, 7),
)


def build(scenario: Scenario) -> TripRequest:
    start = date(2026, 9, 10)
    return TripRequest(
        origin="Köln",
        budget=scenario.budget,
        travelers=2,
        duration_days=scenario.duration_days,
        date_from=start,
        date_to=start + timedelta(days=scenario.duration_days),
        date_flexible=True,
        transport_preferences=["flight", "train"],
        preferred_destinations=["Madrid"],
        avoid_destinations=["Paris"],
        preferred_experiences=["culture", "history", "food"],
        preferences=TravelPreferences(
            history=0.8, nature=0.7, nightlife=0.2, culture=0.8, food=0.6,
            multiple_cities=0.7,
        ),
    )


def show_scenario(planner: TravelPlanner, scenario: Scenario) -> None:
    request = build(scenario)
    print()
    print("=" * 92)
    print(
        f"SCENARIO {scenario.label}   Köln, {scenario.budget:.0f} EUR, "
        f"2 travelers, {scenario.duration_days} days, prefers Madrid, avoids Paris"
    )
    print("=" * 92)

    baseline = planner.plan(request).baseline
    if baseline:
        print(
            f"  baseline (a conventional single-destination search): "
            f"{baseline.destination} {baseline.total_cost:.2f} EUR, "
            f"{baseline.nights} nights"
        )
    else:
        print("  baseline: none affordable")

    for profile in ProfileName:
        started = time.perf_counter()
        result = planner.plan(request, profile=profile)
        elapsed = time.perf_counter() - started
        weights = PROFILES[profile].weights.normalized()
        print()
        print(
            f"  {profile.value:<11} "
            f"(cost {weights['cost']:.2f}  experience {weights['experience']:.2f}  "
            f"diversity {weights['diversity']:.2f}  intensity {weights['intensity']:.2f})"
            f"   {elapsed:.2f}s"
        )
        if not result.recommendations:
            print("      no itinerary fits this request")
            for warning in result.metadata.warnings:
                print(f"      WARNING: {warning}")
            continue

        print(
            f"      {'#':<2} {'value':>6} {'total':>8} {'trans':>7} {'rooms':>7} "
            f"{'ride':>6} {'transit':>8} {'usable':>8} {'exp':>5} {'int':>5}  route"
        )
        for itinerary in result.recommendations:
            costs = itinerary.cost_breakdown
            value = itinerary.value_breakdown
            print(
                f"      {itinerary.rank:<2} {itinerary.score:>6.4f} "
                f"{itinerary.total_cost:>8.2f} {costs.transport:>7.1f} "
                f"{costs.accommodation:>7.1f} {costs.ground_transfer:>6.1f} "
                f"{itinerary.total_transport_minutes / 60:>7.1f}h "
                f"{itinerary.usable_destination_minutes / 60:>7.1f}h "
                f"{value.experience:>5.2f} {value.travel_intensity:>5.2f}  "
                f"{itinerary.route_label()}"
            )
        best = result.recommendations[0]
        rooms = ", ".join(
            f"{s.city} {s.nights}n {s.accommodation_tier or '-'}"
            f" ({(s.accommodation_rating or 0) * 5:.1f}*)"
            for s in best.stays
        )
        print(f"      #1 rooms: {rooms}")
        print(
            f"      #1 factors: "
            f"{', '.join(f.value for f in best.explanation_factors[:8])}"
        )
        metrics = result.metadata.provider_metrics
        if metrics:
            print(
                f"      {result.metadata.states_generated} states, "
                f"{metrics['lookups']:.0f} provider lookups -> "
                f"{metrics['misses']:.0f} upstream "
                f"({metrics['hit_rate']:.0%} cached)"
            )


def show_baseline_comparison(planner: TravelPlanner) -> None:
    """The product question: is the recommendation better than the user's idea?"""
    request = build(SCENARIOS[1])
    result = planner.plan(request, profile=ProfileName.BEST_VALUE)
    baseline = result.baseline
    print()
    print("=" * 92)
    print("BASELINE COMPARISON - the traveler's own idea against ours")
    print("=" * 92)
    if not baseline or not result.recommendations:
        print("  nothing to compare")
        return
    print(
        f"  Their idea : {baseline.destination:<22} "
        f"{baseline.total_cost:>7.2f} EUR   {baseline.nights} nights   1 city"
    )
    for itinerary in result.recommendations[:3]:
        comparison = itinerary.baseline_comparison
        delta = -comparison.money_saved
        print(
            f"  Ours #{itinerary.rank}    : {' + '.join(itinerary.cities):<22} "
            f"{itinerary.total_cost:>7.2f} EUR   "
            f"{sum(s.nights for s in itinerary.stays)} nights   "
            f"{len(itinerary.cities)} cit{'y' if len(itinerary.cities) == 1 else 'ies'}"
            f"   {delta:+.2f} EUR   "
            f"{itinerary.usable_destination_minutes / 60:.0f}h usable"
        )


def show_budget_sensitivity(planner: TravelPlanner) -> None:
    request = build(SCENARIOS[1])
    print()
    print("=" * 92)
    print("BUDGET SENSITIVITY - where a bigger budget actually changes the answer")
    print("=" * 92)
    analysis = analyze_budget_sensitivity(
        planner, request, budgets=[250.0, 300.0, 350.0, 400.0, 450.0, 550.0]
    )
    print(analysis.render())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget-sensitivity", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()

    print("SYNTHETIC DATA - not real prices, hotels, transfers or availability")
    planner = TravelPlanner()

    if args.budget_sensitivity:
        show_budget_sensitivity(planner)
        return
    if args.baseline:
        show_baseline_comparison(planner)
        return

    for scenario in SCENARIOS:
        show_scenario(planner, scenario)
    show_baseline_comparison(planner)


if __name__ == "__main__":
    main()
