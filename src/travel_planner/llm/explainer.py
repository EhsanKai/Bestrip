"""An LLM explainer that cannot contradict the optimizer (V4).

This is the low-risk seam, and it is deliberately first: the explainer
*restates*, it never computes. V3 built the material it consumes - typed
factors, per-city insights, the nine-component breakdown - precisely so that a
generative layer would have nothing left to invent.

**The guard.** "It only restates" is a claim, and an unchecked claim about a
language model is worth nothing. So every number in the generated prose is
checked against the numbers that were actually put in the prompt, and a reply
containing a figure the optimizer never computed is **rejected**, not shipped.
The template explainer then answers instead. A slightly duller sentence beats a
confidently wrong price.

That check is cheap and total: an explanation is a handful of sentences over a
known set of figures, so grounding is decidable here in a way it is not in
general.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models.itinerary import Itinerary
from .client import DEFAULT_MAX_TOKENS, LlmClient, LlmError
from .interfaces import TemplateItineraryExplainer

SYSTEM_PROMPT = """You write one short paragraph explaining a travel \
recommendation to the traveler who asked for it.

You are given the finished itinerary as structured facts. Your job is to say \
what it is and why it was chosen, in plain language.

Rules:
- Use ONLY the numbers given to you. Never compute, estimate, round \
differently, convert, or infer a figure that is not in the facts.
- If a number is not in the facts, do not mention it at all.
- Do not add advice, opinions about the destinations, or anything you were not \
told.
- Two to four sentences. No headings, no bullet points, no markdown.
- Write in the second person, plainly, without marketing language."""

#: Numbers appearing in prose that carry no claim about the itinerary. Ordinals
#: and small counts ("your 2 cities", "#1") would otherwise force a rejection
#: for saying something true.
ALWAYS_ALLOWED = frozenset({"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"})

#: Grouped thousands first, so "1,016.28" is read as one number rather than as
#: "1,016" followed by a stray "28" that nothing grounds.
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _canonical(text: str) -> str:
    """Normalize a number so 1,234.50 / 1234.5 / 1234.50 compare equal."""
    cleaned = text.replace(",", "")
    if "." in cleaned:
        cleaned = cleaned.rstrip("0").rstrip(".")
    return cleaned or "0"


@dataclass(frozen=True, slots=True)
class GroundedFacts:
    """The numbers an explanation is allowed to use, and the prompt carrying them."""

    prompt: str
    allowed: frozenset[str]

    def ungrounded(self, prose: str) -> list[str]:
        """Numbers in ``prose`` that were never given to the model."""
        return [
            match.group()
            for match in _NUMBER.finditer(prose)
            if _canonical(match.group()) not in self.allowed
        ]


def _add(values: set[str], *numbers: float | int | None) -> None:
    """Allow every reasonable rendering of a number the model was given.

    A figure can honestly be written to two decimals, to one, rounded, or
    truncated - "57 hours" for 57.8 is good prose, not an invention. The guard
    exists to catch numbers that came from nowhere, and one that is too strict
    fires on the writing instead of on the facts, which would make it useless
    exactly when it matters.
    """
    for number in numbers:
        if number is None:
            continue
        value = float(number)
        values.update(
            {
                _canonical(f"{value:.2f}"),
                _canonical(f"{value:.1f}"),
                _canonical(f"{value:.0f}"),
                _canonical(str(int(value))),  # truncated
            }
        )


def facts_for(itinerary: Itinerary) -> GroundedFacts:
    """Render an itinerary as facts, and record every number they contain.

    The allowed set is built from the same values that go into the prompt, so
    the guard can never be stricter than what the model was told - only exactly
    as strict.
    """
    lines: list[str] = [
        f"Route: {itinerary.route_label()}",
        f"Cities: {', '.join(itinerary.cities) or 'none'}",
        f"Total cost: {itinerary.total_cost:.2f} {itinerary.currency} for the whole party",
        f"  transport {itinerary.cost_breakdown.transport:.2f}, "
        f"accommodation {itinerary.cost_breakdown.accommodation:.2f}, "
        f"airport transfers {itinerary.cost_breakdown.ground_transfer:.2f}",
        f"Trip length: {itinerary.duration_days:.1f} days",
        f"Time in transit: {itinerary.total_transport_minutes} minutes",
        f"Usable time at the destinations: "
        f"{itinerary.usable_destination_minutes} minutes",
    ]
    allowed: set[str] = set(ALWAYS_ALLOWED)
    _add(
        allowed,
        itinerary.total_cost,
        itinerary.cost_breakdown.transport,
        itinerary.cost_breakdown.accommodation,
        itinerary.cost_breakdown.ground_transfer,
        itinerary.duration_days,
        itinerary.total_transport_minutes,
        itinerary.usable_destination_minutes,
        itinerary.rank,
        len(itinerary.cities),
    )
    # Minutes are usually read back as hours, and refusing that would make the
    # guard fight good writing rather than bad facts.
    _add(allowed, itinerary.total_transport_minutes / 60)
    _add(allowed, itinerary.usable_destination_minutes / 60)

    for stay in itinerary.stays:
        lines.append(
            f"Stay: {stay.city}, {stay.nights} nights, "
            f"room {stay.accommodation_cost:.2f} {itinerary.currency}"
            + (f" ({stay.accommodation_tier})" if stay.accommodation_tier else "")
        )
        _add(allowed, stay.nights, stay.accommodation_cost)

    for insight in itinerary.destination_insights:
        if insight.strengths:
            lines.append(
                f"{insight.city} is strong on: {', '.join(insight.strengths)}"
            )

    comparison = itinerary.baseline_comparison
    if comparison is not None:
        direction = "less" if comparison.money_saved > 0 else "more"
        lines.append(
            f"Compared with a conventional trip to {comparison.baseline_destination} "
            f"({comparison.baseline_cost:.2f} {itinerary.currency}), this costs "
            f"{abs(comparison.money_saved):.2f} {direction} and visits "
            f"{comparison.additional_cities} more cities."
        )
        _add(
            allowed,
            comparison.baseline_cost,
            abs(comparison.money_saved),
            comparison.additional_cities,
        )

    if itinerary.explanation_factors:
        lines.append(
            "Reasons this was recommended: "
            + ", ".join(factor.value.replace("_", " ") for factor in itinerary.explanation_factors)
        )
    return GroundedFacts(prompt="\n".join(lines), allowed=frozenset(allowed))


class LlmItineraryExplainer:
    """An :class:`~travel_planner.llm.interfaces.ItineraryExplainer` using Claude.

    Falls back to the template explainer whenever the model is unreachable or
    its answer fails the grounding check, so this is always safe to install:
    the worst case is V3's output.
    """

    def __init__(
        self,
        client: LlmClient,
        *,
        fallback=None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        strict: bool = False,
    ) -> None:
        self.client = client
        self.fallback = fallback or TemplateItineraryExplainer()
        self.max_tokens = max_tokens
        self.strict = strict
        """Raise instead of falling back. For tests and for a caller who would
        rather see the failure than a quietly different answer."""
        self.rejections: list[str] = []
        """Ungrounded replies, kept so the guard's firing rate is observable
        rather than invisible."""

    def explain(self, itinerary: Itinerary) -> str:
        facts = facts_for(itinerary)
        try:
            prose = self.client.complete(
                system=SYSTEM_PROMPT,
                user=facts.prompt,
                max_tokens=self.max_tokens,
            ).strip()
        except LlmError:
            if self.strict:
                raise
            return self.fallback.explain(itinerary)

        invented = facts.ungrounded(prose)
        if invented:
            self.rejections.append(prose)
            if self.strict:
                raise LlmError(
                    "the explanation contains numbers the optimizer never "
                    f"computed: {invented}"
                )
            return self.fallback.explain(itinerary)
        return prose
