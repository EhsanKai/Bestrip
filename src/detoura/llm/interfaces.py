"""Seams for a future LLM layer.

An LLM may sit on either side of the optimizer:

* **in front**, turning free text into a :class:`TripRequest`, and
* **behind**, turning a finished :class:`Itinerary` into prose.

It must never sit *inside*: prices, routes, availability, constraints and
scores are computed deterministically. The protocols below mark exactly where a
model is allowed to plug in, and the local implementations keep the MVP free of
any external API dependency.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Protocol, runtime_checkable

from ..models.itinerary import Itinerary
from ..models.trip import TravelPreferences, TripRequest


@runtime_checkable
class PreferenceParser(Protocol):
    """Natural language -> structured request."""

    def parse(self, text: str) -> TripRequest: ...


@runtime_checkable
class ItineraryExplainer(Protocol):
    """Structured itinerary -> natural language."""

    def explain(self, itinerary: Itinerary) -> str: ...


#: Keyword hints used by the local parser. Deliberately small: this is a
#: stand-in so the pipeline is runnable end to end, not an NLU system.
_INTEREST_KEYWORDS: dict[str, tuple[str, ...]] = {
    "history": ("history", "historic", "historical", "museum", "ruins", "ancient"),
    "nature": ("nature", "hiking", "mountains", "outdoors", "lakes", "parks"),
    "nightlife": ("nightlife", "clubs", "clubbing", "bars", "party", "partying"),
    "culture": ("culture", "cultural", "art", "galleries", "theatre", "theater"),
    "food": ("food", "cuisine", "restaurants", "eating", "culinary", "foodie"),
    "multiple_cities": ("multiple cities", "several cities", "city hop", "road trip"),
}


class KeywordPreferenceParser:
    """A dependency-free stand-in for an LLM preference parser.

    Extracts a budget, party size, duration and interest weights from simple
    English. Anything it cannot find falls back to the supplied defaults, so the
    result is always a valid :class:`TripRequest`.
    """

    def __init__(
        self,
        *,
        default_origin: str = "Köln",
        default_budget: float = 500.0,
        default_duration_days: int = 5,
        default_date_from: date | None = None,
    ) -> None:
        self.default_origin = default_origin
        self.default_budget = default_budget
        self.default_duration_days = default_duration_days
        self.default_date_from = default_date_from or date(2026, 9, 10)

    def parse(self, text: str) -> TripRequest:
        lowered = text.casefold()

        budget = self.default_budget
        budget_match = re.search(r"(?:€|eur\s*)?(\d+(?:[.,]\d+)?)\s*(?:€|eur|euros?)", lowered)
        if budget_match:
            budget = float(budget_match.group(1).replace(",", "."))

        travelers = 1
        travelers_match = re.search(r"(\d+)\s*(?:people|persons?|travell?ers|adults)", lowered)
        if travelers_match:
            travelers = max(1, int(travelers_match.group(1)))
        elif "couple" in lowered or "my partner" in lowered:
            travelers = 2

        duration = self.default_duration_days
        duration_match = re.search(r"(\d+)\s*(?:days?|nights?)", lowered)
        if duration_match:
            duration = max(1, int(duration_match.group(1)))

        weights = {name: 0.5 for name in TravelPreferences.model_fields}
        for field, keywords in _INTEREST_KEYWORDS.items():
            if any(keyword in lowered for keyword in keywords):
                weights[field] = 0.9

        return TripRequest(
            origin=self.default_origin,
            budget=budget,
            travelers=travelers,
            duration_days=duration,
            date_from=self.default_date_from,
            date_to=self.default_date_from + timedelta(days=duration),
            date_flexible="flexible" in lowered,
            preferences=TravelPreferences(**weights),
        )


class TemplateItineraryExplainer:
    """Renders an itinerary as prose from its own structured fields.

    Every number it prints comes from the itinerary, so the explanation cannot
    drift from what the optimizer actually computed - the failure mode a
    generative explainer has to be guarded against.
    """

    def explain(self, itinerary: Itinerary) -> str:
        cities = ", ".join(itinerary.cities) if itinerary.cities else "no destinations"
        lines = [
            f"#{itinerary.rank}: {itinerary.route_label()}",
            f"{len(itinerary.cities)} city/cities ({cities}) over "
            f"{itinerary.duration_days:.1f} days for "
            f"{itinerary.total_cost:.2f} {itinerary.currency} total, "
            f"{itinerary.total_travel_minutes} minutes in transit.",
        ]
        if itinerary.score_breakdown:
            top = sorted(
                itinerary.score_breakdown.model_dump(
                    include={
                        "budget",
                        "preference",
                        "destination",
                        "convenience",
                        "time",
                        "diversity",
                    }
                ).items(),
                key=lambda kv: -kv[1],
            )[:3]
            lines.append(
                "Strongest factors: "
                + ", ".join(f"{name} {value:.2f}" for name, value in top)
                + "."
            )
        comparison = itinerary.baseline_comparison
        if comparison:
            if comparison.money_saved > 0:
                lines.append(
                    f"That is {comparison.money_saved:.2f} {itinerary.currency} less than the "
                    f"{comparison.baseline_destination} baseline "
                    f"({comparison.baseline_cost:.2f} {itinerary.currency}), "
                    f"with {comparison.additional_cities} extra city/cities."
                )
            else:
                lines.append(
                    f"That is {abs(comparison.money_saved):.2f} {itinerary.currency} more than the "
                    f"{comparison.baseline_destination} baseline."
                )
        return "\n".join(lines)
