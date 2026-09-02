"""The V4 demonstration: each of the six additions, on real output.

    python examples/v4_capabilities.py              # all of them
    python examples/v4_capabilities.py --beam       # adaptive beam ladder
    python examples/v4_capabilities.py --value      # room value for money
    python examples/v4_capabilities.py --inventory  # availability and sell-outs
    python examples/v4_capabilities.py --learning   # fitting profile weights
    python examples/v4_capabilities.py --provider   # a real API, recorded
    python examples/v4_capabilities.py --llm        # both LLM seams, scripted

All prices, hotels, transfers, inventory and destination metadata are
synthetic. No network call is made: the provider and LLM demonstrations run
against recorded payloads and scripted replies, which is also how they are
tested.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta

from detoura import TravelPlanner, TravelPreferences, TripRequest
from detoura.config import PlannerConfig
from detoura.learning import fit_weights, learned_profile, observations_from_result
from detoura.llm import (
    LlmItineraryExplainer,
    LlmPreferenceParser,
    ScriptedClient,
)
from detoura.profiles import COMPONENTS, PROFILES, ProfileName
from detoura.providers.accommodation import SyntheticAccommodationDataProvider
from detoura.providers.amadeus import AmadeusTransportProvider
from detoura.providers.http import HttpResponse
from detoura.providers.transport import SyntheticTransportDataProvider

RULE = "=" * 92


def request(**overrides) -> TripRequest:
    start = date(2026, 9, 10)
    payload = dict(
        origin="Köln",
        budget=450.0,
        travelers=2,
        duration_days=5,
        date_from=start,
        date_to=start + timedelta(days=5),
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
    payload.update(overrides)
    return TripRequest(**payload)


def header(title: str) -> None:
    print()
    print(RULE)
    print(title)
    print(RULE)


# ---------------------------------------------------------------------------
def show_adaptive_beam() -> None:
    header("ADAPTIVE BEAM - widen while widening still pays")
    req = request()
    print("  V3 could only report that its default beam missed better trips.")
    print()
    for label, config in (
        ("fixed (V3)", PlannerConfig()),
        ("adaptive (V4)", PlannerConfig(adaptive_beam=True)),
    ):
        planner = TravelPlanner(config=config)
        started = time.perf_counter()
        result = planner.plan(req)
        elapsed = time.perf_counter() - started
        best = result.recommendations[0]
        print(
            f"  {label:<14} {elapsed:>5.2f}s  beam {result.metadata.beam_width:<4}"
            f" score {best.score:.4f}  {best.route_label()}"
        )
        for rung in result.metadata.beam_rounds:
            print(
                f"       rung w={rung['beam_width']:<4} best {rung['best_score']:.4f}"
                f"  completed {rung['completed']:<5} states {rung['states_generated']:<7}"
                f"  gain {rung['improvement']:+.4f}"
            )


def show_value_for_money() -> None:
    header("VALUE FOR MONEY - was the room upgrade worth it?")
    print("  V3 scored room quality and left price out, so it could say a room")
    print("  was good but never whether it was worth what it cost.")
    print()
    result = TravelPlanner().plan(request(budget=600, duration_days=7,
                                          date_to=date(2026, 9, 17)))
    for itinerary in result.recommendations[:4]:
        value = itinerary.value_breakdown
        print(
            f"  {itinerary.route_label():<44} vfm {value.accommodation_value_for_money:.2f}"
            f"  premium {value.accommodation_premium:>6.2f} EUR"
        )
        for stay in itinerary.stays:
            if stay.accommodation_cost <= 0:
                continue
            print(
                f"      {stay.city:<10} {stay.accommodation_tier or '-':<9}"
                f" paid {stay.accommodation_cost:>7.2f}"
                f"  cheapest offered {stay.cheapest_alternative_cost:>7.2f}"
                f"  premium {stay.accommodation_premium:>6.2f}"
            )
        verdict = [
            f.value
            for f in itinerary.explanation_factors
            if "room" in f.value or "cheapest" in f.value
        ]
        print(f"      verdict: {', '.join(verdict) or '(too marginal to claim)'}")


def show_inventory() -> None:
    header("INVENTORY - rooms and seats that actually run out")
    print("  V3: 'rooms never sell out and flights never fill'. Now they do,")
    print("  deterministically, and unknown availability still means bookable.")
    print()
    req = request()
    plain = TravelPlanner().plan(req, debug=True)
    scarce_planner = TravelPlanner(
        transport_provider=SyntheticTransportDataProvider(simulate_scarcity=True),
        accommodation_provider=SyntheticAccommodationDataProvider(simulate_scarcity=True),
    )
    scarce = scarce_planner.plan(req, debug=True)

    for label, result in (("no inventory data", plain), ("with inventory", scarce)):
        best = result.recommendations[0]
        print(
            f"  {label:<20} {result.metadata.completed_itineraries:>5} completed"
            f"   #1 {best.route_label()}"
        )
    print()
    counts: dict[str, int] = {}
    for iteration in scarce.debug.iterations:
        for reason, count in iteration.rejection_counts.items():
            counts[reason.value] = counts.get(reason.value, 0) + count
    print("  rejections once inventory is real:")
    for reason, count in sorted(counts.items(), key=lambda kv: -kv[1])[:6]:
        print(f"      {reason:<28} {count}")
    print()
    best = scarce.recommendations[0]
    for stay in best.stays:
        if stay.accommodation_cost > 0:
            print(
                f"      {stay.city:<10} {stay.rooms_available} rooms left,"
                f" refundable={stay.free_cancellation}"
            )
    print(f"      factors: {', '.join(f.value for f in best.explanation_factors[-4:])}")


def show_learning() -> None:
    header("LEARNED WEIGHTS - stop asserting the profile, fit it")
    print("  V3: 'profile weights are tuned against the synthetic dataset.")
    print("  They are a starting point, not a claim about real travelers.'")
    print()
    planner = TravelPlanner()
    observations = []
    for budget in (400, 450, 500, 550, 600, 700, 800):
        result = planner.plan(request(budget=float(budget)))
        if len(result.recommendations) < 2:
            continue
        # A cohort that always books the cheapest thing they were shown.
        cheapest = min(result.recommendations, key=lambda i: i.total_cost)
        observations.append(observations_from_result(result, cheapest.rank))

    report = fit_weights(observations)
    print(report.render())
    print()
    prior = PROFILES[ProfileName.BEST_VALUE].weights.normalized()
    fitted = report.weights.normalized()
    print(f"  cost weight: {prior['cost']:.3f} (BEST_VALUE) -> {fitted['cost']:.3f} (learned)")

    profile = learned_profile(report)
    learned = planner.plan(request(), profile=profile)
    default = planner.plan(request())
    print()
    print(f"  default profile #1: {default.recommendations[0].total_cost:>7.2f} EUR"
          f"  {default.recommendations[0].route_label()}")
    print(f"  learned profile #1: {learned.recommendations[0].total_cost:>7.2f} EUR"
          f"  {learned.recommendations[0].route_label()}")


def show_provider() -> None:
    header("A REAL PROVIDER - recorded, not invented")
    print("  V3 shipped a stub that raised NotImplementedError. This is the")
    print("  integration; the only thing missing is a credential.")
    print()

    class Recorded:
        """Replays one token response and one search page."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
            self.calls.append(f"{method} {url.rsplit('/', 1)[-1]}")
            if url.endswith("token"):
                return HttpResponse(200, json.dumps({"access_token": "t", "expires_in": 1799}))
            return HttpResponse(200, json.dumps({"data": [
                {
                    "id": "1",
                    "itineraries": [{"duration": "PT1H25M", "segments": [{
                        "departure": {"iataCode": "CGN", "at": "2026-09-10T08:40:00"},
                        "arrival": {"iataCode": "VIE", "at": "2026-09-10T10:05:00"},
                        "carrierCode": "LH", "transportMode": "FLIGHT",
                    }]}],
                    "price": {"currency": "EUR", "grandTotal": "118.40"},
                    "travelerPricings": [{"travelerId": "1"}, {"travelerId": "2"}],
                    "numberOfBookableSeats": 4,
                }
            ]}))

    http = Recorded()
    provider = AmadeusTransportProvider(
        client_id="demo", client_secret="demo", http_client=http
    )
    for option in provider.search("CGN", "VIE", date(2026, 9, 10)):
        print(
            f"  {option.origin} -> {option.destination}  {option.departure:%H:%M}"
            f"-{option.arrival:%H:%M}  {option.price_per_person:.2f} EUR/person"
            f"  {option.operator}  {option.seats_available} seats left"
        )
    print(f"  upstream calls: {http.calls}")
    print(f"  admissible bound: {provider.min_price('CGN', 'VIE', date(2026, 9, 10))} EUR/person")


def show_llm() -> None:
    header("THE LLM SEAMS - it restates, it never computes")
    result = TravelPlanner().plan(request())
    itinerary = result.recommendations[0]

    print("  Explainer, given a faithful reply:")
    faithful = (
        f"You fly to {' and then '.join(itinerary.cities)} over "
        f"{itinerary.duration_days:.1f} days for {itinerary.total_cost:.2f} EUR all in, "
        f"with {itinerary.usable_destination_minutes // 60} hours actually on the ground."
    )
    explainer = LlmItineraryExplainer(ScriptedClient(replies=[faithful]))
    print(f"      {explainer.explain(itinerary)}")

    print()
    print("  Explainer, given a reply with an invented price:")
    invented = "A wonderful trip, and only 199.99 EUR for the two of you."
    guarded = LlmItineraryExplainer(ScriptedClient(replies=[invented]))
    output = guarded.explain(itinerary)
    print(f"      model said : {invented}")
    print(f"      rejected   : {bool(guarded.rejections)} (199.99 was never computed)")
    print(f"      shipped    : {output.splitlines()[0]}")

    print()
    print("  Parser, given a hallucinated experience, then corrected:")
    client = ScriptedClient(
        replies=[
            json.dumps({"budget": 600, "travelers": 2, "duration_days": 5,
                        "preferred_experiences": ["teleportation"]}),
            json.dumps({"budget": 600, "travelers": 2, "duration_days": 5,
                        "preferred_experiences": ["culture", "food"]}),
        ]
    )
    parser = LlmPreferenceParser(client)
    parsed = parser.parse("two of us, 600 euros, five days, we like culture and food")
    print(f"      attempt 1 rejected: {parser.attempts[0].error.splitlines()[0][:70]}")
    print(f"      accepted request  : {parsed.budget:.0f} EUR, "
          f"{parsed.travelers} travelers, {parsed.preferred_experiences}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("beam", "value", "inventory", "learning", "provider", "llm"):
        parser.add_argument(f"--{flag}", action="store_true")
    args = parser.parse_args()

    print("SYNTHETIC DATA - no network call is made anywhere in this script")
    sections = {
        "beam": show_adaptive_beam,
        "value": show_value_for_money,
        "inventory": show_inventory,
        "learning": show_learning,
        "provider": show_provider,
        "llm": show_llm,
    }
    chosen = [name for name in sections if getattr(args, name)]
    for name in chosen or sections:
        sections[name]()


if __name__ == "__main__":
    main()
