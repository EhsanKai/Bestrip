"""Travel intensity (V3).

Two trips can cost the same, last the same five days, and feel completely
different:

    Trip A   2 cities,  4 hours in transit
    Trip B   4 cities, 18 hours in transit

Intensity measures how much of the trip is spent *moving*, and how often. It is
not an anti-multi-city penalty: ADVENTURE deliberately weights diversity high
enough to pay for a reasonable amount of it. What intensity stops is travel
that is not buying anything - four airports in four days to see the inside of
four stations.

Three signals, deliberately kept separate so they stay explainable:

* **transit share** - transport time over trip length,
* **movement rate** - legs per day on the ground,
* **airport churn** - how often the trip changes airports, which costs real
  hours that never show up as intercity travel time.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.search import SearchState
from ..models.trip import TravelStyle

#: Transit share at or above which the transit signal scores zero.
#:
#: Calibrated against real itineraries rather than intuition: a calm two-city
#: European trip spends about 3% of its span moving, and the spec's "18 hours
#: over five days" slog spends about 14%. A threshold of 0.30 would have called
#: both of them relaxed, which is why this is 0.15.
MAX_TRANSIT_SHARE = 0.15

#: Legs per trip day at or above which the movement signal scores zero.
MAX_LEGS_PER_DAY = 1.25

#: Airport changes per trip day at or above which churn scores zero.
MAX_AIRPORT_CHANGES_PER_DAY = 0.75

#: Intensity score below which a trip reads as frantic.
HIGH_INTENSITY_SCORE = 0.4

#: How the three signals combine into one intensity score.
TRANSIT_WEIGHT = 0.50
MOVEMENT_WEIGHT = 0.30
CHURN_WEIGHT = 0.20

#: How much intensity each travel style tolerates before it starts to hurt.
#: A relaxed traveler minds a busy schedule more than a packed one does.
STYLE_TOLERANCE: dict[TravelStyle, float] = {
    TravelStyle.RELAXED: 0.75,
    TravelStyle.BALANCED: 1.00,
    TravelStyle.PACKED: 1.40,
}


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


@dataclass(frozen=True, slots=True)
class IntensityAssessment:
    """How hard an itinerary works the traveler."""

    intensity: float
    """Raw transit share: transport minutes over trip minutes. Lower is calmer."""
    transit_share: float
    legs_per_day: float
    airport_changes_per_day: float
    score: float
    """``1`` = relaxed, ``0`` = frantic. This is what Travel Value consumes."""
    transport_minutes: int
    trip_minutes: int

    @property
    def is_high(self) -> bool:
        """Whether the trip is frantic overall, not merely long in transit.

        Judged on the combined score rather than the raw share, so a trip made
        exhausting by five airports rather than by hours also counts.
        """
        return self.score < HIGH_INTENSITY_SCORE


class IntensityScorer:
    """Turns a state's movement pattern into an intensity assessment."""

    def assess(
        self,
        state: SearchState,
        style: TravelStyle = TravelStyle.BALANCED,
    ) -> IntensityAssessment:
        trip_minutes = max(state.trip_span_minutes, 1)
        transport_minutes = state.total_transport_minutes
        trip_days = trip_minutes / (24 * 60)

        transit_share = transport_minutes / trip_minutes
        legs_per_day = state.leg_count / max(trip_days, 1e-9)
        airport_changes_per_day = _airport_changes(state) / max(trip_days, 1e-9)

        tolerance = STYLE_TOLERANCE[style]
        transit_signal = clamp01(1.0 - transit_share / (MAX_TRANSIT_SHARE * tolerance))
        movement_signal = clamp01(1.0 - legs_per_day / (MAX_LEGS_PER_DAY * tolerance))
        churn_signal = clamp01(
            1.0 - airport_changes_per_day / (MAX_AIRPORT_CHANGES_PER_DAY * tolerance)
        )

        score = (
            TRANSIT_WEIGHT * transit_signal
            + MOVEMENT_WEIGHT * movement_signal
            + CHURN_WEIGHT * churn_signal
        )
        return IntensityAssessment(
            intensity=round(transit_share, 6),
            transit_share=round(transit_share, 6),
            legs_per_day=round(legs_per_day, 6),
            airport_changes_per_day=round(airport_changes_per_day, 6),
            score=round(clamp01(score), 6),
            transport_minutes=transport_minutes,
            trip_minutes=trip_minutes,
        )


def _airport_changes(state: SearchState) -> int:
    """Legs that involve a flight, plus the two ground transfers.

    Each is a check-in, a security queue and a transfer the traveler pays for in
    time that intercity duration alone does not capture.
    """
    from ..models.transport import TransportType

    flights = sum(
        1 for leg in state.route if leg.transport_type is TransportType.FLIGHT
    )
    transfers = int(state.outbound_transfer is not None) + int(
        state.return_transfer is not None
    )
    return flights + transfers
