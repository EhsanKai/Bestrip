"""Recommendation confidence (V5.8).

The spec is unambiguous about what this must not be: *"Do NOT present fake
statistical certainty."* There is no probability here, because there is no
probability to be had - nothing in this system knows the likelihood that a
traveler will enjoy a trip.

What it *does* know is how much work went into the answer and how solid the
answer looked while that work was happening. That is a real thing to report,
and it is what the levels mean:

    HIGH     several independent signals agree this is a well-founded answer
    GOOD     the usual case: a sound search with ordinary caveats
    LIMITED  something specific undermines it, and we say which

Every level comes with its reasons, positive and negative, in the traveler's
language. "Strong recommendation" on its own is marketing; "Strong
recommendation, because it stayed on top through a deeper search and the price
was checked a minute ago" is information.

The signals are all legitimate in the sense the spec asks for - each is a fact
about this search, not a proxy for one:

* **ranking stability** - did widening the search change the winner?
* **search depth** - how hard did we actually look?
* **Pareto strength** - is this trip un-dominated, or a near-miss that survived?
* **provider completeness** - did every provider answer?
* **price freshness** - how old are the numbers?
* **availability confidence** - do we know it can be booked?
* **alternative diversity** - was there a real field to win against?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..models.freshness import PriceFreshness
from ..models.itinerary import Itinerary


class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"
    GOOD = "GOOD"
    LIMITED = "LIMITED"

    @property
    def label(self) -> str:
        return {
            "HIGH": "Strong recommendation",
            "GOOD": "Good confidence",
            "LIMITED": "Limited confidence",
        }[self.value]


@dataclass(frozen=True, slots=True)
class ConfidenceReason:
    """One thing that made the answer stronger or weaker."""

    label: str
    positive: bool


@dataclass(frozen=True, slots=True)
class RecommendationConfidence:
    level: ConfidenceLevel
    reasons: list[ConfidenceReason] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "level": self.level.value,
            "label": self.level.label,
            "reasons": [
                {"label": reason.label, "positive": reason.positive}
                for reason in self.reasons
            ],
        }


@dataclass(frozen=True, slots=True)
class SearchQuality:
    """What the run as a whole can say about itself.

    Computed once per search and shared by every recommendation in it, because
    these are properties of the search, not of any one trip.
    """

    rounds: int = 1
    """Beam rounds run. More than one means the answer survived widening."""

    winner_stable: bool = True
    """Whether the top itinerary survived every widening.

    The strongest single signal available: a recommendation that stays on top
    as the search gets harder is one the search is confident about, and that is
    exactly the claim "we looked harder and still think this" makes.
    """

    frontier_size: int = 0
    completed: int = 0
    alternatives_returned: int = 0
    degraded: bool = False
    """True when a provider failed and the search ran on partial data."""

    deep: bool = False
    """Whether the traveler asked for a deep search."""


def assess(
    itinerary: Itinerary,
    quality: SearchQuality,
    *,
    freshness: PriceFreshness = PriceFreshness.UNKNOWN,
    dominated: bool = False,
) -> RecommendationConfidence:
    """Grade one recommendation, with its reasons.

    The arithmetic is deliberately crude - count the good signals, count the
    disqualifying ones - because anything more elaborate would imply a
    precision that is not there. The value is in *which* signals fired, and
    those are reported individually.
    """
    positives: list[ConfidenceReason] = []
    negatives: list[ConfidenceReason] = []

    # --- how hard we looked, and whether it held up --------------------
    if quality.deep and quality.rounds > 1:
        if quality.winner_stable:
            positives.append(
                ConfidenceReason("Stayed on top through a deeper search", True)
            )
        else:
            # Not a negative: a deeper search that *changed* the winner has
            # produced a better answer, not a shakier one. It simply does not
            # earn the stability point.
            positives.append(
                ConfidenceReason("Found by searching deeper than usual", True)
            )
    elif quality.deep:
        positives.append(ConfidenceReason("Deep search", True))

    # --- was there a real field to win against? ------------------------
    if quality.completed >= 200:
        positives.append(
            ConfidenceReason(
                f"Compared against {quality.completed:,} possible trips", True
            )
        )
    elif quality.completed < 25:
        negatives.append(
            ConfidenceReason("Few trips matched your constraints", False)
        )

    if quality.alternatives_returned >= 3:
        positives.append(ConfidenceReason("Several strong alternatives evaluated", True))

    # --- Pareto position ----------------------------------------------
    if not dominated:
        positives.append(
            ConfidenceReason("No other trip beats it on every measure", True)
        )
    else:
        negatives.append(
            ConfidenceReason("Another trip matches or beats it on every measure", False)
        )

    # --- the data underneath -------------------------------------------
    if freshness is PriceFreshness.FRESH:
        positives.append(ConfidenceReason("Price checked just now", True))
    elif freshness is PriceFreshness.RECENT:
        positives.append(ConfidenceReason("Price checked recently", True))
    elif freshness is PriceFreshness.STALE:
        negatives.append(ConfidenceReason("Price may have changed since we checked", False))
    else:
        negatives.append(ConfidenceReason("Prices are estimates, not live quotes", False))

    if quality.degraded:
        negatives.append(
            ConfidenceReason("Some providers did not respond, so this search was partial", False)
        )

    # --- availability ---------------------------------------------------
    counted = [
        stay.rooms_available
        for stay in itinerary.stays
        if stay.rooms_available is not None
    ]
    if counted and min(counted) <= 2:
        negatives.append(ConfidenceReason("Very limited rooms left", False))
    elif counted:
        positives.append(ConfidenceReason("Rooms confirmed available", True))

    # --- the verdict ----------------------------------------------------
    # A degraded search or a dominated trip can never be HIGH however many
    # positives it collects: those two facts are about whether the answer is
    # trustworthy at all, not about how much evidence supports it.
    blocking = quality.degraded or dominated
    if not blocking and len(positives) >= 4 and not negatives:
        level = ConfidenceLevel.HIGH
    elif len(negatives) >= 2 or blocking:
        level = ConfidenceLevel.LIMITED
    else:
        level = ConfidenceLevel.GOOD

    return RecommendationConfidence(level=level, reasons=positives + negatives)
