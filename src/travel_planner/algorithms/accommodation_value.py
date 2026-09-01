"""Accommodation quality scoring (V3).

V2 always booked the cheapest sufficient room, so "pay €20 more for a much
better hotel" was not a decision the optimizer could make. V3 lets the search
branch across tiers and scores what each one buys.

**Price is deliberately not in this score.** Cost already has its own
component; putting it here too would double-count it, and the trade-off the
spec asks for - "the extra €60 must be justified by increased overall itinerary
value" - is exactly a weighted trade between AccommodationQuality going up and
BudgetEfficiency going down. Scoring price twice would bias that trade rather
than model it.

The result is that a €120 4.8-star room does *not* automatically beat a €60
4.4-star one: it wins only when the quality gain outweighs what the extra €60
costs on the budget axis, which is the correct semantics.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.accommodation import AccommodationOption, AccommodationType
from ..models.search import SearchState
from ..models.trip import AccommodationPreference

#: How the quality of one room is composed.
RATING_WEIGHT = 0.50
LOCATION_WEIGHT = 0.35
TYPE_WEIGHT = 0.15

#: How well each accommodation type suits each stated preference.
#: A hostel is a fine answer for someone optimizing for price and a poor one
#: for someone who asked for quality.
TYPE_FIT: dict[AccommodationPreference, dict[AccommodationType, float]] = {
    AccommodationPreference.CHEAPEST: {
        AccommodationType.HOSTEL: 1.00,
        AccommodationType.APARTMENT: 0.80,
        AccommodationType.HOTEL: 0.60,
        AccommodationType.BOUTIQUE: 0.35,
    },
    AccommodationPreference.BALANCED: {
        AccommodationType.HOSTEL: 0.55,
        AccommodationType.APARTMENT: 0.80,
        AccommodationType.HOTEL: 0.90,
        AccommodationType.BOUTIQUE: 0.85,
    },
    AccommodationPreference.QUALITY: {
        AccommodationType.HOSTEL: 0.20,
        AccommodationType.APARTMENT: 0.60,
        AccommodationType.HOTEL: 0.85,
        AccommodationType.BOUTIQUE: 1.00,
    },
}

#: Small bonus for a refundable booking, which is real value even though it
#: costs nothing extra in this dataset.
CANCELLATION_BONUS = 0.05

#: Score used when a stay has no accommodation at all (V1 economics, or a
#: same-day hop). Neutral: absent data must not read as "bad".
NO_ACCOMMODATION_SCORE = 0.5


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


@dataclass(frozen=True, slots=True)
class AccommodationAssessment:
    """The accommodation verdict for a whole itinerary."""

    score: float
    rating: float
    location: float
    type_fit: float
    nights: int
    booked_stays: int
    """How many stays actually had a room booked."""


class AccommodationScorer:
    """Scores the rooms an itinerary books, ignoring their price."""

    def score_option(
        self,
        option: AccommodationOption,
        preference: AccommodationPreference,
    ) -> float:
        """Quality of one room for a traveler with this preference."""
        fit = TYPE_FIT[preference].get(option.accommodation_type, 0.7)
        score = (
            RATING_WEIGHT * option.rating
            + LOCATION_WEIGHT * option.location_score
            + TYPE_WEIGHT * fit
        )
        if option.free_cancellation:
            score += CANCELLATION_BONUS
        return clamp01(score)

    def assess(
        self, state: SearchState, preference: AccommodationPreference
    ) -> AccommodationAssessment:
        """Weight each stay's room quality by how many nights are spent in it."""
        if not state.stays:
            return AccommodationAssessment(
                score=NO_ACCOMMODATION_SCORE, rating=0.0, location=0.0,
                type_fit=0.0, nights=0, booked_stays=0,
            )

        weighted = rating = location = fit = 0.0
        nights = booked = 0
        for stay in state.stays:
            if stay.accommodation is None:
                continue
            room = stay.accommodation
            weight = max(stay.nights, 1)
            weighted += self.score_option(room, preference) * weight
            rating += room.rating * weight
            location += room.location_score * weight
            fit += TYPE_FIT[preference].get(room.accommodation_type, 0.7) * weight
            nights += weight
            booked += 1

        if nights == 0:
            return AccommodationAssessment(
                score=NO_ACCOMMODATION_SCORE, rating=0.0, location=0.0,
                type_fit=0.0, nights=0, booked_stays=0,
            )
        return AccommodationAssessment(
            score=round(clamp01(weighted / nights), 6),
            rating=round(rating / nights, 6),
            location=round(location / nights, 6),
            type_fit=round(fit / nights, 6),
            nights=nights,
            booked_stays=booked,
        )
