"""Scenarios A, B and C: the same request under all three profiles.

    python examples/profiles_demo.py [--budget 450]

All prices, schedules, hotels and transfers are synthetic.
"""

from __future__ import annotations

import argparse
from datetime import date

from detoura import TravelPlanner, TravelPreferences, TripRequest
from detoura.profiles import PROFILES, ProfileName


def build_request(budget: float) -> TripRequest:
    return TripRequest(
        origin="Köln",
        budget=budget,
        travelers=2,
        duration_days=5,
        date_from=date(2026, 9, 10),
        date_to=date(2026, 9, 15),
        date_flexible=True,
        transport_preferences=["flight", "train"],
        preferred_destinations=["Madrid"],
        avoid_destinations=["Paris"],
        preferences=TravelPreferences(
            history=0.8,
            nature=0.7,
            nightlife=0.2,
            culture=0.8,
            food=0.6,
            multiple_cities=0.9,
        ),
    )


def show(planner: TravelPlanner, request: TripRequest, profile: ProfileName) -> None:
    result = planner.plan(request, profile=profile)
    definition = PROFILES[profile]
    weights = definition.weights.normalized()

    print()
    print("=" * 78)
    print(f"{profile.value}  -  {definition.description}")
    print(
        "weights: "
        + "  ".join(f"{name} {value:.2f}" for name, value in weights.items())
    )
    print("=" * 78)

    if not result.recommendations:
        print("  no itinerary fits this request")
        for warning in result.metadata.warnings:
            print(f"  WARNING: {warning}")
        return

    baseline = result.baseline
    if baseline:
        print(
            f"  baseline: {baseline.destination} "
            f"{baseline.total_cost:.2f} {baseline.currency} "
            f"({baseline.nights} nights)  "
            f"[transport {baseline.cost_breakdown.transport:.0f} + "
            f"rooms {baseline.cost_breakdown.accommodation:.0f} + "
            f"transfer {baseline.cost_breakdown.ground_transfer:.0f}]"
        )
    else:
        print("  baseline: none (no affordable Madrid trip)")
    print()

    header = (
        f"  {'#':<2} {'score':>6} {'total':>8} {'trans':>7} {'rooms':>7} "
        f"{'ride':>6} {'days':>5} {'usable':>7}  route"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for itinerary in result.recommendations:
        costs = itinerary.cost_breakdown
        value = itinerary.value_breakdown
        print(
            f"  {itinerary.rank:<2} {itinerary.score:>6.4f} "
            f"{itinerary.total_cost:>8.2f} {costs.transport:>7.1f} "
            f"{costs.accommodation:>7.1f} {costs.ground_transfer:>6.1f} "
            f"{itinerary.duration_days:>5.1f} {value.usable_ratio:>6.0%}  "
            f"{itinerary.route_label()}"
        )
        print(
            f"      cost {value.cost:.2f} | experience {value.experience:.2f} | "
            f"preferences {value.preferences:.2f} | time {value.time:.2f} | "
            f"diversity {value.diversity:.2f}"
        )
        print(f"      {', '.join(f.value for f in itinerary.explanation_factors)}")

    print(
        f"\n  {result.metadata.states_generated} states explored, "
        f"{result.metadata.completed_itineraries} complete itineraries, "
        f"{result.metadata.pareto_kept} on the Pareto frontier, "
        f"in {result.metadata.elapsed_seconds:.3f}s"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--budget",
        type=float,
        default=450.0,
        help=(
            "Total budget in EUR. Note that with accommodation and ground "
            "transfers priced in, 250 buys nothing for two people over five days."
        ),
    )
    args = parser.parse_args()

    print("SYNTHETIC DATA - not real prices, hotels or availability")
    print(
        f"\nKöln, {args.budget:.0f} EUR, 2 travelers, 5 days, "
        "prefers Madrid, avoids Paris, multiple_cities = 0.9"
    )

    planner = TravelPlanner()
    request = build_request(args.budget)
    for profile in (ProfileName.CHEAPEST, ProfileName.BEST_VALUE, ProfileName.ADVENTURE):
        show(planner, request, profile)


if __name__ == "__main__":
    main()
