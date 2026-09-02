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

#: Extra bonus a refundable booking earns when the room is nearly gone (V4).
#:
#: V3 carried ``free_cancellation`` and paid a flat bonus for it, which misses
#: what the option is actually worth: being able to cancel a room you could
#: re-book tomorrow is worth little, and being able to cancel the last room in
#: town is worth a lot. The bonus now scales with scarcity, up to this much on
#: top - and stays flat when the provider reports no inventory, so a feed
#: without counts behaves exactly as it did in V3.
SCARCITY_CANCELLATION_BONUS = 0.05

#: Score used when a stay has no accommodation at all (V1 economics, or a
#: same-day hop). Neutral: absent data must not read as "bad".
NO_ACCOMMODATION_SCORE = 0.5

#: Quality points a euro of nightly premium is expected to buy (V4).
#:
#: Measured, not guessed: this is the rate at which the *whole* tier ladder
#: trades quality for money for a BALANCED traveler on a typical EUR 60 base
#: rate - budget to comfort is +EUR 54/night and +0.358 of quality, so 0.0066.
#: Rounding to 0.0065 makes that full-ladder upgrade land on neutral by
#: construction, and the two half-steps then say something useful:
#:
#:   budget   -> standard   +EUR 21/night, +0.242 quality -> rate 0.0115 -> 0.89
#:   standard -> comfort    +EUR 33/night, +0.116 quality -> rate 0.0035 -> 0.27
#:
#: which is the real shape of the ladder: the first step up is good value and
#: the second is not. Re-derive this constant if the tier table changes.
VALUE_REFERENCE_RATE = 0.0065

#: Value-for-money reported when nothing was traded - the booked room *is* the
#: cheapest one offered, so there is no premium to judge.
NEUTRAL_VALUE_FOR_MONEY = 0.5


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

    value_for_money: float = NEUTRAL_VALUE_FOR_MONEY
    """Did the premium over the cheapest fetched room earn its keep? (V4)

    ``0.5`` means nothing was traded or the premium bought exactly the expected
    rate of quality; above means it bought more, below means it bought less.
    A **diagnostic**, never a Travel Value component - see the module docstring.
    """

    premium: float = 0.0
    """Party total paid above the cheapest rooms that were on offer."""

    premium_quality_gain: float = 0.0
    """Night-weighted quality bought by that premium, in score points."""


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
            # V4: what the option is worth depends on how hard the room would
            # be to replace. With no inventory data ``scarcity`` is 0.0 and
            # this is exactly V3's flat bonus.
            score += CANCELLATION_BONUS + SCARCITY_CANCELLATION_BONUS * option.scarcity
        return clamp01(score)

    def value_for_money(
        self,
        state: SearchState,
        preference: AccommodationPreference,
    ) -> tuple[float, float, float]:
        """``(score, premium, quality_gain)`` for the rooms this trip booked.

        Answers the one question the accommodation component deliberately
        cannot: *was the upgrade worth it?* Quality and price are compared
        against the cheapest room the provider actually offered for the same
        stay, because that is the alternative the traveler really had.

        A trip that took the cheapest room everywhere pays no premium and
        scores :data:`NEUTRAL_VALUE_FOR_MONEY` - it made no trade, so there is
        nothing to praise or criticise.
        """
        premium = 0.0
        gain = 0.0
        nights = 0
        for stay in state.stays:
            room, baseline = stay.accommodation, stay.cheapest_alternative
            if room is None or baseline is None:
                continue
            weight = max(stay.nights, 1)
            nights += weight
            premium += stay.accommodation_premium
            gain += (
                self.score_option(room, preference)
                - self.score_option(baseline, preference)
            ) * weight

        if nights == 0 or premium <= 0.0:
            return NEUTRAL_VALUE_FOR_MONEY, round(max(premium, 0.0), 2), round(gain, 6)

        # Quality bought per euro per night, against the expected rate. Halved
        # so that "exactly the expected rate" lands on the neutral 0.5 and a
        # premium that buys twice the expected quality reaches 1.0.
        rate = (gain / nights) / (premium / nights)
        score = clamp01(0.5 * rate / VALUE_REFERENCE_RATE)
        return round(score, 6), round(premium, 2), round(gain, 6)

    def assess(
        self, state: SearchState, preference: AccommodationPreference
    ) -> AccommodationAssessment:
        """Weight each stay's room quality by how many nights are spent in it."""
        empty = AccommodationAssessment(
            score=NO_ACCOMMODATION_SCORE, rating=0.0, location=0.0,
            type_fit=0.0, nights=0, booked_stays=0,
        )
        if not state.stays:
            return empty

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
            return empty
        value, premium, gain = self.value_for_money(state, preference)
        return AccommodationAssessment(
            score=round(clamp01(weighted / nights), 6),
            rating=round(rating / nights, 6),
            location=round(location / nights, 6),
            type_fit=round(fit / nights, 6),
            nights=nights,
            booked_stays=booked,
            value_for_money=value,
            premium=premium,
            premium_quality_gain=gain,
        )
