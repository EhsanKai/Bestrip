"""Destination experience and preference matching (V3).

V2 knew cities mainly as nodes with five attributes. V3 gives them a twelve-
dimensional profile and asks a sharper question: *for this traveler*, is this
city worth the days it will take?

Three separately observable factors, combined as the spec's conceptual product:

```
ExperienceScore = destination_quality × preference_match × stay_quality
```

A weighted **geometric** mean is used rather than an arithmetic one, because
the product form is the point: a wonderful city you have no time in is not a
wonderful experience, and neither is a perfectly-timed stay somewhere the
traveler actively dislikes. Each factor is floored at :data:`MIN_FACTOR` so one
weak dimension drags the score down hard without annihilating it.

Everything here is deterministic and produces a structured
:class:`DestinationInsight` explaining *why* a city scored the way it did.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import PlannerConfig
from ..data.destinations import canonical_key
from ..models.destination import EXPERIENCE_ATTRIBUTES, Destination
from ..models.trip import TripRequest
from ..providers.destinations import DestinationProvider

#: Weight a listed ``preferred_experiences`` attribute is given.
PREFERRED_WEIGHT = 1.0

#: How hard a listed ``disliked_experiences`` attribute counts against a city.
DISLIKE_PENALTY = 0.5

#: Fraction of its value a city keeps when the traveler has already been there.
REVISIT_FACTOR = 0.75

#: Relative importance of the three experience factors (they need not sum to 1;
#: they are normalized into geometric-mean exponents).
QUALITY_WEIGHT = 0.35
MATCH_WEIGHT = 0.40
STAY_WEIGHT = 0.25

#: No single factor may drive the product below this, so one weak dimension
#: penalizes heavily without erasing every other signal.
MIN_FACTOR = 0.05

#: ``multiple_cities`` at or above this shifts the ideal city count up ...
STRONG_CITY_APPETITE = 0.8
#: ... and at or below this, down.
WEAK_CITY_APPETITE = 0.2

#: An attribute at or above this is worth calling out as a strength ...
STRONG_ATTRIBUTE = 0.75
#: ... and at or below this, as a weakness the traveler asked about.
WEAK_ATTRIBUTE = 0.40


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


@dataclass(frozen=True, slots=True)
class PreferenceContext:
    """The traveler's resolved taste, in a form that can key a cache.

    Resolving the several preference channels is cheap but not free, and the
    beam asks about the same cities thousands of times per run; carrying an
    immutable, hashable context lets every one of those lookups be memoized.
    """

    weights: tuple[tuple[str, float], ...]
    disliked: frozenset[str]
    visited: frozenset[str]

    @property
    def weight_map(self) -> dict[str, float]:
        return dict(self.weights)


@dataclass(frozen=True, slots=True)
class DestinationInsight:
    """Why one city scored the way it did, in structured form."""

    city: str
    quality: float
    preference_match: float
    stay_quality: float
    score: float
    usable_days: float
    strengths: tuple[str, ...] = ()
    """Attributes the traveler wanted and this city delivers."""
    weaknesses: tuple[str, ...] = ()
    """Attributes the traveler wanted and this city lacks."""
    dislikes_present: tuple[str, ...] = ()
    """Attributes the traveler wanted to avoid but this city is strong in."""
    previously_visited: bool = False
    stay_note: str = ""


@dataclass(frozen=True, slots=True)
class ExperienceAssessment:
    """The experience verdict for a whole itinerary."""

    score: float
    quality: float
    preference_match: float
    stay_quality: float
    richness: float
    insights: tuple[DestinationInsight, ...] = field(default=())


class ExperienceEngine:
    """Scores destinations against a traveler, with explanations."""

    def __init__(self, config: PlannerConfig, destinations: DestinationProvider) -> None:
        self.config = config
        self.destinations = destinations
        self._context_cache: dict[tuple, PreferenceContext] = {}
        #: ``(context, city, usable days) -> score``. The hot path: the beam
        #: evaluates the same city at the same stay length over and over.
        self._score_cache: dict[tuple[PreferenceContext, str, float], float] = {}

    # ------------------------------------------------------------------
    # Preference resolution
    # ------------------------------------------------------------------
    def context(self, request: TripRequest) -> PreferenceContext:
        """Turn the request's several preference channels into one weight map.

        Numeric ``preferences`` are the base; ``preferred_experiences`` raises
        those attributes to :data:`PREFERRED_WEIGHT`; ``disliked_experiences``
        are removed from the positive weights and kept separately so they can be
        penalized rather than merely ignored.
        """
        cache_key = (
            tuple(sorted(request.preferences.experience_weights().items())),
            tuple(sorted(request.preferred_experiences)),
            tuple(sorted(request.disliked_experiences)),
            tuple(sorted(request.previously_visited)),
        )
        cached = self._context_cache.get(cache_key)
        if cached is not None:
            return cached

        weights = dict(request.preferences.experience_weights())
        for name in request.preferred_experiences:
            weights[name] = max(weights.get(name, 0.0), PREFERRED_WEIGHT)
        disliked = frozenset(request.disliked_experiences)
        for name in disliked:
            weights.pop(name, None)

        resolved = PreferenceContext(
            weights=tuple(sorted(weights.items())),
            disliked=disliked,
            visited=frozenset(
                canonical_key(city) for city in request.previously_visited
            ),
        )
        self._context_cache[cache_key] = resolved
        return resolved

    def resolve_weights(
        self, request: TripRequest
    ) -> tuple[dict[str, float], frozenset[str]]:
        """Backward-compatible view of :meth:`context`."""
        ctx = self.context(request)
        return ctx.weight_map, ctx.disliked

    # ------------------------------------------------------------------
    # Fast path
    # ------------------------------------------------------------------
    def city_score(
        self, city: str, usable_days: float, context: PreferenceContext
    ) -> float:
        """Just the number, memoized - what beam ranking needs.

        :meth:`assess_city` builds the full explanation object; that is worth
        doing for the handful of itineraries actually returned, not for every
        one of the tens of thousands of states the beam considers.
        """
        rounded = round(usable_days, 2)
        key = (context, city, rounded)
        cached = self._score_cache.get(key)
        if cached is not None:
            return cached

        destination = self.destinations.get(city)
        if destination is None:
            self._score_cache[key] = 0.5
            return 0.5
        weights = context.weight_map
        score = _geometric_blend(
            self.destination_quality(destination),
            self.preference_match(destination, weights, context.disliked),
            self.stay_quality(destination, rounded),
        )
        if canonical_key(city) in context.visited:
            score *= REVISIT_FACTOR
        score = clamp01(score)
        self._score_cache[key] = score
        return score

    def total_score(
        self, stays: list[tuple[str, float]], request: TripRequest
    ) -> float:
        """Mean city score for an itinerary, without building insights."""
        if not stays:
            return 0.0
        context = self.context(request)
        return sum(
            self.city_score(city, days, context) for city, days in stays
        ) / len(stays)

    # ------------------------------------------------------------------
    # The three factors
    # ------------------------------------------------------------------
    def destination_quality(self, destination: Destination) -> float:
        """How much there is to do here, independent of the traveler's taste."""
        return clamp01(destination.richness)

    def preference_match(
        self,
        destination: Destination,
        weights: dict[str, float],
        disliked: frozenset[str],
    ) -> float:
        """Weighted overlap between what the traveler wants and what the city is.

        Disliked attributes subtract: a traveler who said "no nightlife" should
        rank a party city *below* a neutral one, not merely equal to it.
        """
        vector = destination.experience_vector()
        total_weight = sum(weights.values())
        if total_weight <= 0:
            match = 0.5  # the traveler expressed nothing; stay neutral
        else:
            match = sum(weights[name] * vector[name] for name in weights) / total_weight

        if disliked:
            present = sum(vector[name] for name in disliked) / len(disliked)
            match -= DISLIKE_PENALTY * present
        return clamp01(match)

    def stay_quality(self, destination: Destination, usable_days: float) -> float:
        """How well the usable time on the ground fits what the city deserves.

        Below the recommended minimum the score falls off linearly - half the
        recommended days is half the experience. Above the recommended maximum
        it decays more gently, because an extra day somewhere good is a mild
        waste, not a failure.
        """
        low, high = destination.recommended_days()
        if usable_days <= 0:
            return 0.0
        if usable_days < low:
            return clamp01(usable_days / low)
        if usable_days <= high:
            return 1.0
        excess = usable_days - high
        return clamp01(1.0 - excess / (high + self.config.stay_overrun_tolerance_days))

    # ------------------------------------------------------------------
    # Per-city assessment
    # ------------------------------------------------------------------
    def assess_city(
        self,
        city: str,
        usable_days: float,
        request: TripRequest,
        weights: dict[str, float] | None = None,
        disliked: frozenset[str] | None = None,
    ) -> DestinationInsight:
        """Score one city and record why."""
        if weights is None or disliked is None:
            context = self.context(request)
            weights, disliked = context.weight_map, context.disliked

        destination = self.destinations.get(city)
        if destination is None:
            # Unknown city: stay neutral rather than inventing an opinion.
            return DestinationInsight(
                city=city,
                quality=0.5,
                preference_match=0.5,
                stay_quality=0.5,
                score=0.5,
                usable_days=usable_days,
                stay_note="unknown destination",
            )

        quality = self.destination_quality(destination)
        match = self.preference_match(destination, weights, disliked)
        stay = self.stay_quality(destination, usable_days)

        visited = any(
            _same_city(city, seen) for seen in request.previously_visited
        )
        score = _geometric_blend(quality, match, stay)
        if visited:
            score *= REVISIT_FACTOR

        vector = destination.experience_vector()
        wanted = sorted(name for name, weight in weights.items() if weight >= 0.6)
        strengths = tuple(n for n in wanted if vector[n] >= STRONG_ATTRIBUTE)
        weaknesses = tuple(n for n in wanted if vector[n] <= WEAK_ATTRIBUTE)
        present_dislikes = tuple(
            sorted(n for n in disliked if vector[n] >= STRONG_ATTRIBUTE)
        )

        low, high = destination.recommended_days()
        if usable_days < low:
            note = f"{usable_days:.1f} usable days, below the recommended {low:g}"
        elif usable_days > high:
            note = f"{usable_days:.1f} usable days, above the recommended {high:g}"
        else:
            note = f"{usable_days:.1f} usable days is within the recommended {low:g}-{high:g}"

        return DestinationInsight(
            city=city,
            quality=round(quality, 6),
            preference_match=round(match, 6),
            stay_quality=round(stay, 6),
            score=round(clamp01(score), 6),
            usable_days=round(usable_days, 3),
            strengths=strengths,
            weaknesses=weaknesses,
            dislikes_present=present_dislikes,
            previously_visited=visited,
            stay_note=note,
        )

    # ------------------------------------------------------------------
    # Itinerary assessment
    # ------------------------------------------------------------------
    def assess(
        self, stays: list[tuple[str, float]], request: TripRequest
    ) -> ExperienceAssessment:
        """Score every stay in an itinerary as ``(city, usable_days)`` pairs."""
        if not stays:
            return ExperienceAssessment(
                score=0.0, quality=0.0, preference_match=0.0, stay_quality=0.0,
                richness=0.0,
            )
        context = self.context(request)
        weights, disliked = context.weight_map, context.disliked
        insights = tuple(
            self.assess_city(city, days, request, weights, disliked)
            for city, days in stays
        )
        count = len(insights)
        return ExperienceAssessment(
            score=round(sum(i.score for i in insights) / count, 6),
            quality=round(sum(i.quality for i in insights) / count, 6),
            preference_match=round(sum(i.preference_match for i in insights) / count, 6),
            stay_quality=round(sum(i.stay_quality for i in insights) / count, 6),
            richness=round(
                sum(
                    (self.destinations.get(i.city).richness
                     if self.destinations.get(i.city) else 0.5)
                    for i in insights
                )
                / count,
                6,
            ),
            insights=insights,
        )

    def max_supportable_cities(self, usable_days: float) -> int:
        """The most cities this trip length can hold at all.

        Uses the *shortest* recommended stay in the catalog plus the day each
        move costs, so this is a genuine ceiling rather than a preference: no
        appetite and no profile may push the target above it. Without it the
        appetite shift and the profile bias compound, and a five-day trip ends
        up nominally wanting four cities.
        """
        catalog = self.destinations.all()
        if not catalog:
            return 1
        shortest = min(d.recommended_min_days for d in catalog)
        per_city = shortest + self.config.city_change_overhead_days
        return max(1, min(int(usable_days // max(per_city, 0.5)), self.config.max_cities))

    def ideal_city_count(self, request: TripRequest, usable_days: float) -> int:
        """How many cities this trip length can carry.

        Uses the catalog's own recommended minimum stay rather than a hard-coded
        table: if the average city wants two days, five usable days is about two
        or three cities. An explicit ``preferred_city_count`` always wins.
        """
        if request.preferred_city_count is not None:
            return min(request.preferred_city_count, self.config.max_cities)
        # NOTE: an explicit preferred_city_count is honoured above the ceiling;
        # if the traveler insists on four cities in five days, that is their
        # call and the intensity and experience components will price it.
        catalog = self.destinations.all()
        if not catalog:
            return 1
        # Each city costs its own recommended stay *plus* the day spent moving
        # to it. Leaving the hop out is how a planner concludes that five days
        # comfortably holds three cities.
        typical = sum(d.recommended_min_days for d in catalog) / len(catalog)
        per_city = typical + self.config.city_change_overhead_days
        estimate = round(usable_days / max(per_city, 0.5))
        # The V1 appetite knob still has a say: someone who told us they love
        # city-hopping should be aimed one city higher than the arithmetic alone
        # suggests, and someone who wants one place, one lower.
        appetite = request.preferences.multiple_cities
        if appetite >= STRONG_CITY_APPETITE:
            estimate += 1
        elif appetite <= WEAK_CITY_APPETITE:
            estimate -= 1
        ceiling = min(self.max_supportable_cities(usable_days), self.config.max_cities)
        return max(1, min(int(estimate), ceiling))


def _geometric_blend(quality: float, match: float, stay: float) -> float:
    """Weighted geometric mean of the three experience factors."""
    total = QUALITY_WEIGHT + MATCH_WEIGHT + STAY_WEIGHT
    factors = (
        (max(quality, MIN_FACTOR), QUALITY_WEIGHT / total),
        (max(match, MIN_FACTOR), MATCH_WEIGHT / total),
        (max(stay, MIN_FACTOR), STAY_WEIGHT / total),
    )
    product = 1.0
    for value, exponent in factors:
        product *= value**exponent
    return clamp01(product)


def _same_city(left: str, right: str) -> bool:
    return canonical_key(left) == canonical_key(right)


__all__ = [
    "DestinationInsight",
    "PreferenceContext",
    "ExperienceAssessment",
    "ExperienceEngine",
    "EXPERIENCE_ATTRIBUTES",
]
