"""Your idea versus ours (V7, Phase 1).

Detoura's whole claim is that there is a better trip than the one you had in
mind. A claim like that is only worth anything if it can come out false, so
this module is built to be able to say "your original idea is better" - and to
say it in the same breath, with the same numbers, as it says the opposite.

Two design rules make that structural rather than aspirational.

**One scorer.** Every metric compared here was produced by the same
:class:`~detoura.algorithms.travel_value.TravelValueScorer` running over a real
:class:`~detoura.models.search.SearchState`, for both sides. There is no
independent "baseline maths". Two code paths would drift, and drift in a
comparison always flatters the side the author was rooting for.

**One direction table.** Whether a metric favours us or the traveler is read
from :data:`DIRECTIONS`, not decided per metric at the call site. Adding a
metric that happens to make Detoura look good therefore takes the same one line
as adding one that does not.

Nothing here is prose invented about a number. The summary is assembled from
the structured comparison, which is why it can be tested.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ..models.itinerary import BaselineResult, Itinerary

#: Currency symbols worth printing. Anything else falls back to its code, which
#: is ugly but never wrong.
_SYMBOLS: dict[str, str] = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF "}

#: Below these differences the two trips are the same trip on that axis.
#:
#: They exist so a rounding artefact cannot become a sales pitch. A euro and a
#: half of difference is not a reason to change your holiday, and reporting it
#: as one is how a comparison stops being trusted. Each is set at roughly the
#: smallest difference a traveler would actually act on.
PRICE_EPSILON = 5.0
"""Euros. Less than a round of coffee."""
HOURS_EPSILON = 1.0
"""Hours of usable destination time."""
TRANSIT_EPSILON = 0.5
"""Hours in transit. Tighter than usable time: transit is felt more sharply."""
SCORE_EPSILON = 0.02
"""Normalized score points, on 0..1. Two percent of the scale."""


class Favours(str, Enum):
    """Who a single metric favours."""

    DETOURA = "DETOURA"
    ORIGINAL = "ORIGINAL"
    NEITHER = "NEITHER"
    UNKNOWN = "UNKNOWN"
    """The data to decide does not exist. Never collapses into NEITHER:
    "we don't know" and "they're equal" are different answers, and only one of
    them is honest about baggage before Phase 3."""


class ComparisonVerdict(str, Enum):
    """The overall answer, including the ones we would rather not give."""

    DETOURA_BETTER = "DETOURA_BETTER"
    """Wins on something material, loses on nothing material."""
    ORIGINAL_BETTER = "ORIGINAL_BETTER"
    """Loses on something material, wins on nothing material."""
    MIXED = "MIXED"
    """Real gains and real costs. The most common honest answer."""
    EQUIVALENT = "EQUIVALENT"
    """Nothing material separates them."""


#: ``+1`` means a higher value is better for the traveler, ``-1`` lower.
DIRECTIONS: dict[str, int] = {
    "price": -1,
    "city_count": +1,
    "usable_hours": +1,
    "transit_hours": -1,
    "experience": +1,
    "preference_match": +1,
    "accommodation": +1,
}

#: Materiality threshold per metric, in that metric's own units.
EPSILONS: dict[str, float] = {
    "price": PRICE_EPSILON,
    "city_count": 0.5,
    "usable_hours": HOURS_EPSILON,
    "transit_hours": TRANSIT_EPSILON,
    "experience": SCORE_EPSILON,
    "preference_match": SCORE_EPSILON,
    "accommodation": SCORE_EPSILON,
}

#: How each metric is described to a person.
_LABELS: dict[str, str] = {
    "price": "price",
    "city_count": "cities",
    "usable_hours": "usable destination time",
    "transit_hours": "time in transit",
    "experience": "destination experience",
    "preference_match": "match to your interests",
    "accommodation": "accommodation quality",
}


class MetricComparison(BaseModel):
    """One axis, compared."""

    model_config = ConfigDict(frozen=True)

    metric: str
    label: str
    original: float
    detoura: float
    delta: float
    """Always ``detoura - original``, whatever direction is better."""
    favours: Favours
    material: bool
    """Whether the difference is big enough to mean anything."""


class TripComparison(BaseModel):
    """The traveler's own idea against one Detoura recommendation."""

    model_config = ConfigDict(frozen=True)

    original_destination: str
    original_price: float
    detoura_price: float
    currency: str = "EUR"

    price_delta: float
    """Positive means the Detoura trip costs more."""
    city_count_delta: int
    usable_time_delta: float
    """Hours. Positive means the Detoura trip buys more time in destinations."""
    transit_time_delta: float
    """Hours. Positive means the Detoura trip spends longer travelling."""
    experience_delta: float
    preference_match_delta: float
    accommodation_delta: float
    baggage_delta: float | None = None
    """``None`` until a baggage model exists (Phase 3). Never ``0.0``: nobody
    has established that baggage costs the same on both trips."""

    original_duration_days: float = 0.0
    detoura_duration_days: float = 0.0
    duration_days_delta: float = 0.0
    """Positive means the Detoura trip is the longer holiday.

    Not scored as an advantage. A longer trip buying more usable hours is not
    a better trip, it is a different one, and presenting those extra hours as a
    win without saying the trip is longer is the comparison flattering itself.
    So this is disclosed rather than credited.
    """

    verdict: ComparisonVerdict
    metrics: list[MetricComparison] = Field(default_factory=list)
    advantages: list[str] = Field(default_factory=list)
    """What the Detoura trip materially wins on."""
    tradeoffs: list[str] = Field(default_factory=list)
    """What it materially costs. Never empty when :attr:`verdict` is MIXED."""
    added_cities: list[str] = Field(default_factory=list)
    dropped_original_destination: bool = False
    """True when the Detoura trip does not visit the traveler's own destination.

    Counted as a material loss in :attr:`verdict` and stated in
    :attr:`tradeoffs`. Not delivering the destination somebody asked for is a
    cost, and a flag nothing reads is a cost that never reaches the reader.
    """
    summary: str = ""
    unknowns: list[str] = Field(default_factory=list)
    """Things this comparison could not establish, stated rather than omitted."""


def _hours(minutes: float) -> float:
    return round(minutes / 60.0, 1)


def _money(amount: float, currency: str) -> str:
    symbol = _SYMBOLS.get(currency, f"{currency} ")
    return f"{symbol}{abs(amount):,.0f}"


def _compare_metric(
    metric: str, original: float, detoura: float
) -> MetricComparison:
    """One axis, judged by the direction table rather than by opinion."""
    # Materiality is judged on the raw difference and only then rounded for
    # display. Rounding first lets an artefact cross the line the epsilon
    # exists to defend: 27 minutes apart became "0.5h" once each side had been
    # rounded to a tenth of an hour, and 4.9999995 euros rounded up into a
    # material price gap. The epsilons exist so rounding cannot become a sales
    # pitch, which only works if rounding happens last.
    raw = detoura - original
    epsilon = EPSILONS[metric]
    material = abs(raw) >= epsilon
    delta = round(raw, 6)
    if not material:
        favours = Favours.NEITHER
    elif raw * DIRECTIONS[metric] > 0:
        favours = Favours.DETOURA
    else:
        favours = Favours.ORIGINAL
    return MetricComparison(
        metric=metric,
        label=_LABELS[metric],
        original=round(original, 6),
        detoura=round(detoura, 6),
        delta=delta,
        favours=favours,
        material=material,
    )


def _phrase(metric: MetricComparison, currency: str, *, invert: bool = False) -> str:
    """A readable fragment for one material difference, from its own numbers.

    ``invert`` flips the point of view. A delta is always ``detoura -
    original``, so describing what the *traveler's* trip does better means
    negating it first. Without this the same number gets reported as "269 more"
    in a sentence whose subject is the trip that is 269 cheaper - which is not
    a wording slip, it is a false statement about the price.
    """
    delta = -metric.delta if invert else metric.delta
    if metric.metric == "price":
        return f"{_money(delta, currency)} {'more expensive' if delta > 0 else 'cheaper'}"
    if metric.metric == "city_count":
        count = abs(int(round(delta)))
        word = "city" if count == 1 else "cities"
        return f"{'+' if delta > 0 else '-'}{count} {word}"
    if metric.metric == "usable_hours":
        return f"{abs(delta):.1f}h {'more' if delta > 0 else 'less'} in destinations"
    if metric.metric == "transit_hours":
        return f"{abs(delta):.1f}h {'more' if delta > 0 else 'less'} in transit"
    # The three normalized scores read as percentage points of the 0..1 scale.
    return f"{abs(delta) * 100:.0f}% {'better' if delta > 0 else 'worse'} {metric.label}"


#: Difference in trip length, in days, worth telling the reader about.
DURATION_EPSILON = 1.0


def _summarize(
    comparison_metrics: list[MetricComparison],
    verdict: ComparisonVerdict,
    *,
    currency: str,
    original_destination: str,
    added_cities: list[str],
    unknowns: list[str],
    dropped: bool = False,
    duration_delta: float = 0.0,
    original_duration: float = 0.0,
    detoura_duration: float = 0.0,
) -> str:
    """Deterministic prose. Both sides always, and the traveler's side first.

    Leading with what their own idea does better is deliberate. A comparison
    that opens with our advantages and buries theirs in a trailing clause is
    technically complete and practically a sales pitch.

    The fragments are joined as an explicit "wins on:" list rather than woven
    into a sentence. Seven metrics in different units do not compose into
    English reliably, and a comparison that reads well but parses wrong is
    worse than one that reads like a scoreboard.
    """
    wins = [m for m in comparison_metrics if m.favours is Favours.DETOURA]
    losses = [m for m in comparison_metrics if m.favours is Favours.ORIGINAL]

    if verdict is ComparisonVerdict.EQUIVALENT:
        return _append_unknowns(
            f"This is effectively the same trip as your {original_destination} "
            "idea - nothing separates them by enough to matter.",
            unknowns,
        )

    parts: list[str] = []

    # Length first, because it conditions how every other number reads. More
    # usable hours on a trip that is two days longer is arithmetic, not an
    # improvement, and the reader is entitled to know which they are looking at
    # before they are told who won.
    if abs(duration_delta) >= DURATION_EPSILON:
        longer = "longer" if duration_delta > 0 else "shorter"
        parts.append(
            f"These are different-length trips: yours runs "
            f"{original_duration:.1f} days, this one {detoura_duration:.1f} - "
            f"{abs(duration_delta):.1f} days {longer}."
        )

    if losses:
        parts.append(
            f"Your original {original_destination} trip wins on: "
            + _join([_phrase(m, currency, invert=True) for m in losses])
            + "."
        )
    if wins or dropped:
        # A trip that replaces the traveler's destination has not "added"
        # anything - it swapped. Calling a substitution an addition is the
        # single most flattering thing this prose could do, so it is spelled
        # out before any advantage is claimed.
        if dropped and added_cities:
            lead = (
                f"The Detoura alternative does not visit {original_destination} "
                f"at all - it goes to {_join(added_cities)} instead"
            )
        elif dropped:
            lead = (
                f"The Detoura alternative does not visit {original_destination}"
            )
        elif added_cities:
            lead = f"The Detoura alternative adds {_join(added_cities)}"
        else:
            lead = "The Detoura alternative"
        if wins:
            parts.append(
                f"{lead}, and wins on: {_join([_phrase(m, currency) for m in wins])}."
            )
        else:
            parts.append(f"{lead}.")
    return _append_unknowns(" ".join(parts), unknowns)


def _append_unknowns(text: str, unknowns: list[str]) -> str:
    if not unknowns:
        return text
    return f"{text} Not included in this comparison: {_join(unknowns)}."


def _join(items: list[str]) -> str:
    """``"a"`` / ``"a and b"`` / ``"a, b and c"``."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def values_of_itinerary(itinerary: Itinerary) -> dict[str, float]:
    """The seven comparable quantities, read off a finished itinerary.

    Extracted so re-optimization's change diff (V7 Phase 2) measures a trip the
    same way this module measures one. Two extraction sites would be two
    chances to read transit off `total_travel_minutes` on one side and
    `total_transport_minutes` on the other, which is exactly the defect Phase 1
    shipped and had to fix.
    """
    return {
        "price": itinerary.total_cost,
        "city_count": float(len(itinerary.cities)),
        "usable_hours": itinerary.usable_destination_minutes / 60.0,
        "transit_hours": itinerary.total_transport_minutes / 60.0,
        "experience": itinerary.experience_score,
        "preference_match": itinerary.preference_score,
        "accommodation": itinerary.accommodation_score,
    }


def values_of_baseline(baseline: BaselineResult) -> dict[str, float]:
    """The same seven quantities, read off the traveler's own idea."""
    return {
        "price": baseline.total_cost,
        "city_count": float(baseline.city_count),
        "usable_hours": baseline.usable_destination_minutes / 60.0,
        "transit_hours": baseline.total_transport_minutes / 60.0,
        "experience": baseline.experience_score,
        "preference_match": baseline.preference_match,
        "accommodation": baseline.accommodation_score,
    }


def build_metrics(
    before: dict[str, float], after: dict[str, float]
) -> list[MetricComparison]:
    """Compare every axis in :data:`DIRECTIONS`, in a fixed order.

    Iterating the direction table rather than a hand-written list is what makes
    adding a metric that flatters Detoura exactly as much work as adding one
    that does not.
    """
    return [_compare_metric(name, before[name], after[name]) for name in DIRECTIONS]


def compare_trips(
    itinerary: Itinerary, baseline: BaselineResult | None
) -> TripComparison | None:
    """Compare one recommendation against the traveler's own idea.

    Returns ``None`` when there is nothing to compare against - the traveler
    named no destination, or no version of their idea was bookable inside the
    budget, window and duration. ``None`` is the honest answer there: without a
    priced original, any claim that we improved on it would be invented.

    Also returns ``None`` for an unscored baseline. A baseline computed without
    a scorer carries zeros in its score fields, and zeros compared against real
    scores would manufacture a landslide out of a missing argument.
    """
    if baseline is None or not baseline.scored:
        return None
    if itinerary.value_breakdown is None:
        # The mirror of the unscored-baseline guard, and it was missing.
        # `Itinerary.experience_score` and friends return a hard 0.0 when there
        # is no value breakdown, so comparing an unscored itinerary against a
        # scored baseline manufactured a three-axis landslide for the traveler
        # out of an absent argument - the same fabrication, pointed the other
        # way. Refusing on either side is the only consistent rule.
        return None

    # Door to door on both sides. `total_travel_minutes` is intercity legs
    # only, so reading it against a transfer-inclusive figure compared two
    # different quantities and reported an identical route as a transit gap.
    metrics = build_metrics(
        values_of_baseline(baseline), values_of_itinerary(itinerary)
    )
    by_name = {m.metric: m for m in metrics}

    wins = [m for m in metrics if m.favours is Favours.DETOURA]
    losses = [m for m in metrics if m.favours is Favours.ORIGINAL]

    # The traveler's own destination is compared case-insensitively: it came
    # from their typing, and "berlin" is Berlin.
    original_id = baseline.destination.casefold()
    visited = {city.casefold() for city in itinerary.cities}
    added = [city for city in itinerary.cities if city.casefold() != original_id]
    dropped = original_id not in visited

    # Losing the destination somebody asked for is a material cost, so it
    # counts towards the verdict like any other. Without this a trip that
    # silently substitutes Prague for Berlin could still be announced as
    # DETOURA_BETTER on the strength of the metrics alone.
    tradeoffs = [_phrase(m, itinerary.currency) for m in losses]
    if dropped:
        tradeoffs.append(f"does not visit {baseline.destination}")

    has_loss = bool(losses) or dropped
    if not wins and not has_loss:
        verdict = ComparisonVerdict.EQUIVALENT
    elif wins and not has_loss:
        verdict = ComparisonVerdict.DETOURA_BETTER
    elif has_loss and not wins:
        verdict = ComparisonVerdict.ORIGINAL_BETTER
    else:
        verdict = ComparisonVerdict.MIXED

    duration_delta = round(itinerary.duration_days - baseline.duration_days, 2)

    # Stated on every comparison until Phase 3 gives baggage a real number.
    # Silence here would let a reader assume baggage was already priced in.
    unknowns = ["baggage"]

    return TripComparison(
        original_destination=baseline.destination,
        original_price=baseline.total_cost,
        detoura_price=itinerary.total_cost,
        currency=itinerary.currency,
        price_delta=by_name["price"].delta,
        city_count_delta=int(by_name["city_count"].delta),
        usable_time_delta=by_name["usable_hours"].delta,
        transit_time_delta=by_name["transit_hours"].delta,
        experience_delta=by_name["experience"].delta,
        preference_match_delta=by_name["preference_match"].delta,
        accommodation_delta=by_name["accommodation"].delta,
        baggage_delta=None,
        original_duration_days=baseline.duration_days,
        detoura_duration_days=itinerary.duration_days,
        duration_days_delta=duration_delta,
        verdict=verdict,
        metrics=metrics,
        advantages=[_phrase(m, itinerary.currency) for m in wins],
        tradeoffs=tradeoffs,
        added_cities=added,
        dropped_original_destination=dropped,
        summary=_summarize(
            metrics,
            verdict,
            currency=itinerary.currency,
            original_destination=baseline.destination,
            added_cities=added,
            unknowns=unknowns,
            dropped=dropped,
            duration_delta=duration_delta,
            original_duration=baseline.duration_days,
            detoura_duration=itinerary.duration_days,
        ),
        unknowns=unknowns,
    )
