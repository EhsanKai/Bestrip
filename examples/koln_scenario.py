"""Run the spec's Köln scenario against the synthetic network and print it.

    python examples/koln_scenario.py [--debug]

All prices, schedules and availability are synthetic.
"""

from __future__ import annotations

import sys
from datetime import date

from travel_planner import TravelPlanner, TravelPreferences, TripRequest
from travel_planner.llm import TemplateItineraryExplainer

REQUEST = TripRequest(
    origin="Köln",
    budget=250,
    travelers=2,
    duration_days=5,
    date_from=date(2026, 9, 10),
    date_to=date(2026, 9, 15),
    date_flexible=True,
    transport_preferences=["flight", "train"],
    must_visit=[],
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


def main() -> None:
    verbose = "--debug" in sys.argv
    result = TravelPlanner().plan(REQUEST, debug=verbose)
    explainer = TemplateItineraryExplainer()

    print("SYNTHETIC DATA - not real prices or availability\n")
    print(f"Origin {REQUEST.origin!r} -> departure airports "
          f"{', '.join(result.metadata.origin_airports)}")
    print(f"Candidate start dates: {', '.join(result.metadata.start_dates)}\n")

    baseline = result.baseline
    if baseline:
        print("BASELINE (what a single-destination search would return)")
        print(f"  {baseline.legs[0].origin} -> {baseline.destination} -> "
              f"{baseline.legs[-1].destination}")
        print(f"  {baseline.total_cost:.2f} {baseline.currency}, "
              f"{baseline.duration_days:.1f} days, 1 city\n")
    else:
        print("BASELINE: none (no preferred destination given)\n")

    print("RECOMMENDATIONS")
    for itinerary in result.recommendations:
        print()
        for line in explainer.explain(itinerary).splitlines():
            print(f"  {line}")
        for option in itinerary.legs:
            print(
                f"    {option.departure:%a %d %b %H:%M} "
                f"{option.origin:>10} -> {option.destination:<10} "
                f"{option.transport_type.value:<6} "
                f"{option.price_per_person:6.2f}/pp  "
                f"{option.duration_minutes:>4} min"
            )

    print()
    metadata = result.metadata
    print(
        f"Explored {metadata.states_generated} states "
        f"({metadata.states_rejected} rejected), found "
        f"{metadata.completed_itineraries} complete itineraries, "
        f"{metadata.pareto_kept} on the Pareto frontier, "
        f"returned {metadata.returned} in {metadata.elapsed_seconds:.3f}s."
    )
    for warning in metadata.warnings:
        print(f"WARNING: {warning}")

    if verbose and result.debug:
        print()
        print(result.debug.render())


if __name__ == "__main__":
    main()
