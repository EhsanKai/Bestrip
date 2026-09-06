"""How much of the traveler's trip an edited trip kept (V7 Phase 2).

Editing is not discovery. When somebody removes one city from a trip they
chose, they are not asking what the best trip in Europe is - they are asking
for their trip, minus that city. An engine that answers the first question when
asked the second is technically optimising and practically ignoring them.

So re-optimization ranks by Travel Value *and* by how much of the original
survived. This module computes the second half.

**It must never enter discovery ranking.** `TravelValueScorer` is untouched and
no profile gains a weight. A fresh search that quietly preferred trips
resembling some earlier trip would not be discovering anything, and it would
invalidate every golden signature in the repository. Similarity is a selection
concern belonging to one code path.

One thing similarity deliberately does *not* do: protect locked cities. Those
are hard constraints on the derived request, so a candidate missing a locked
city is invalid and never reaches this module. Similarity chooses between
candidates that are all already legal - Hamburg before Berlin or after, this
hotel or that one - which is why its influence is bounded and safe to tune.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.itinerary import Itinerary
from ..models.search import SearchState

#: Component weights. Deliberately not tuned to a benchmark yet: §29's rule
#: about not hard-coding a ratio without measuring applies here too. City
#: preservation dominates because it is what people mean by "keep my trip";
#: the tail terms break ties between candidates that are otherwise the same
#: shape.
WEIGHTS: dict[str, float] = {
    "city_preservation": 0.30,
    "city_order": 0.15,
    "stay_duration": 0.10,
    "transport": 0.10,
    "accommodation": 0.10,
    "airports": 0.10,
    "dates": 0.05,
    "price": 0.05,
    "duration": 0.05,
}

#: Price difference, as a share of the original, at which price proximity
#: reaches zero. Half again as expensive is a different trip, not a variation.
PRICE_TOLERANCE = 0.5
#: Nights of difference at which stay-duration similarity reaches zero.
STAY_TOLERANCE_NIGHTS = 3.0
#: Days of difference at which duration proximity reaches zero.
DURATION_TOLERANCE_DAYS = 3.0
#: Days of departure-date drift at which date similarity reaches zero.
DATE_TOLERANCE_DAYS = 7.0


@dataclass(frozen=True, slots=True)
class TripSimilarity:
    """Per-component similarity to the trip the traveler started from."""

    city_preservation: float
    city_order: float
    stay_duration: float
    transport: float
    accommodation: float
    airports: float
    dates: float
    price: float
    duration: float
    total: float

    def as_dict(self) -> dict[str, float]:
        return {
            "city_preservation": self.city_preservation,
            "city_order": self.city_order,
            "stay_duration": self.stay_duration,
            "transport": self.transport,
            "accommodation": self.accommodation,
            "airports": self.airports,
            "dates": self.dates,
            "price": self.price,
            "duration": self.duration,
            "total": self.total,
        }


def _fold(name: str) -> str:
    return name.casefold()


def _leg_key(leg) -> tuple[str, str, object]:
    """Identity of a journey as a traveler would recognise it."""
    return (leg.origin, leg.destination, leg.departure)


def _decay(difference: float, tolerance: float) -> float:
    """1.0 at no difference, 0.0 at or beyond ``tolerance``."""
    if tolerance <= 0:
        return 1.0 if difference == 0 else 0.0
    return max(0.0, 1.0 - abs(difference) / tolerance)


def _longest_common_subsequence(left: list[str], right: list[str]) -> int:
    """Length of the longest order-preserving common subsequence.

    Order is compared as a subsequence rather than by position because
    inserting one city should not be read as having reordered every city after
    it. Köln->Hamburg->Berlin and Köln->Hamburg->Prague->Berlin preserve the
    traveler's ordering perfectly; a positional comparison would score the
    second as badly scrambled.
    """
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for a in left:
        current = [0]
        for index, b in enumerate(right):
            if a == b:
                current.append(previous[index] + 1)
            else:
                current.append(max(previous[index + 1], current[index]))
        previous = current
    return previous[-1]


def compare(original: Itinerary, candidate: SearchState) -> TripSimilarity:
    """How much of ``original`` survives in ``candidate``.

    Computed against a :class:`SearchState` rather than a finished
    :class:`Itinerary` so selection can rank thousands of candidates without
    paying to score and assemble every one of them.
    """
    original_cities = [_fold(city) for city in original.cities]
    candidate_cities = [_fold(city) for city in candidate.cities]
    original_set, candidate_set = set(original_cities), set(candidate_cities)

    kept = original_set & candidate_set
    city_preservation = len(kept) / len(original_set) if original_set else 1.0

    # Order is measured over the cities both trips share: a city that was
    # removed on purpose must not also be counted as an ordering failure.
    shared_original = [c for c in original_cities if c in kept]
    shared_candidate = [c for c in candidate_cities if c in kept]
    if len(shared_original) <= 1:
        city_order = 1.0
    else:
        city_order = _longest_common_subsequence(
            shared_original, shared_candidate
        ) / len(shared_original)

    original_nights = {_fold(s.city): s.nights for s in original.stays}
    candidate_nights = {_fold(s.city): s.nights for s in candidate.stays}
    if kept:
        stay_scores = [
            _decay(
                candidate_nights.get(city, 0) - original_nights.get(city, 0),
                STAY_TOLERANCE_NIGHTS,
            )
            for city in sorted(kept)
        ]
        stay_duration = sum(stay_scores) / len(stay_scores)
    else:
        stay_duration = 0.0

    # Legs are matched on (origin, destination, departure) rather than on the
    # provider's id. The id is internal and never reaches a client, so a trip
    # sent back to be edited could not be compared by it - and the natural key
    # is what `recheck` already uses to re-find a saved leg.
    original_legs = {_leg_key(leg) for leg in original.legs}
    candidate_legs = {_leg_key(leg) for leg in candidate.route}
    transport = (
        len(original_legs & candidate_legs) / len(original_legs)
        if original_legs
        else 1.0
    )

    original_rooms = {
        _fold(s.city): s.accommodation_name
        for s in original.stays
        if s.accommodation_name
    }
    if original_rooms:
        candidate_rooms = {
            _fold(s.city): (s.accommodation.name if s.accommodation else None)
            for s in candidate.stays
        }
        matches = sum(
            1
            for city, name in original_rooms.items()
            if candidate_rooms.get(city) == name
        )
        accommodation = matches / len(original_rooms)
    else:
        # No rooms to preserve is not a failure to preserve them.
        accommodation = 1.0

    airports = (
        0.5 * float(candidate.origin_airport == original.origin_airport)
        + 0.5 * float(candidate.current_location == original.return_airport)
    )

    if candidate.route and original.legs:
        drift = (
            candidate.route[0].departure.date() - original.departure.date()
        ).days
        dates = _decay(drift, DATE_TOLERANCE_DAYS)
    else:
        dates = 0.0

    price = (
        _decay(
            (candidate.total_cost - original.total_cost) / original.total_cost,
            PRICE_TOLERANCE,
        )
        if original.total_cost > 0
        else 1.0
    )

    candidate_days = candidate.trip_span_minutes / (24 * 60)
    duration = _decay(
        candidate_days - original.duration_days, DURATION_TOLERANCE_DAYS
    )

    components = {
        "city_preservation": city_preservation,
        "city_order": city_order,
        "stay_duration": stay_duration,
        "transport": transport,
        "accommodation": accommodation,
        "airports": airports,
        "dates": dates,
        "price": price,
        "duration": duration,
    }
    total = sum(components[name] * weight for name, weight in WEIGHTS.items())
    return TripSimilarity(
        **{name: round(value, 6) for name, value in components.items()},
        total=round(total, 6),
    )
