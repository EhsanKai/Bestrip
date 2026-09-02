"""Synthetic accommodation prices.

Fabricated, but shaped so the numbers matter: a cheap flight into an expensive
city can lose to a dearer flight into a cheap one, which is the whole point of
adding accommodation to the model.

Prices are per room per night. Three tiers are offered everywhere; the search
takes the cheapest sufficient option by default
(``PlannerConfig.accommodation_options_per_stay``).
"""

from __future__ import annotations

from datetime import date, timedelta

from dataclasses import dataclass

from ..models.accommodation import (
    AccommodationOption,
    AccommodationTier,
    AccommodationType,
    rating_from_stars,
)

#: Standard-tier price for a double room, per night, per city.
CITY_NIGHTLY_RATES: dict[str, float] = {
    "London": 70.0,
    "Brussels": 55.0,
    "Paris": 68.0,
    "Amsterdam": 80.0,
    "Prague": 45.0,
    "Vienna": 60.0,
    "Madrid": 65.0,
    "Barcelona": 70.0,
    "Milan": 75.0,
    "Rome": 65.0,
    "Dublin": 80.0,
    "Copenhagen": 90.0,
    "Budapest": 40.0,
    "Berlin": 55.0,
    "Munich": 72.0,
    "Zurich": 95.0,
}

#: Fallback for a city with no explicit rate.
DEFAULT_NIGHTLY_RATE = 60.0

@dataclass(frozen=True, slots=True)
class Tier:
    """One quality band, and what it buys.

    Price rises faster than quality on purpose: the premium tier costs 2.4x the
    budget one but is not 2.4x better, so "pay more" is a real trade-off the
    optimizer has to justify rather than a free upgrade.
    """

    tier: AccommodationTier
    price_multiplier: float
    capacity: int
    rating: float
    location_score: float
    accommodation_type: AccommodationType
    free_cancellation: bool


#: The three bands offered in every city.
TIERS: tuple[Tier, ...] = (
    Tier(AccommodationTier.BUDGET, 0.65, 2, rating_from_stars(3.3), 0.55,
         AccommodationType.HOSTEL, False),
    Tier(AccommodationTier.STANDARD, 1.00, 2, rating_from_stars(4.1), 0.72,
         AccommodationType.HOTEL, True),
    Tier(AccommodationTier.COMFORT, 1.55, 3, rating_from_stars(4.7), 0.90,
         AccommodationType.BOUTIQUE, True),
)

#: Friday and Saturday nights cost more. Deterministic, no randomness.
WEEKEND_SURCHARGE = 1.15
WEEKEND_WEEKDAYS = frozenset({4, 5})


def nightly_rate(city: str) -> float:
    return CITY_NIGHTLY_RATES.get(city, DEFAULT_NIGHTLY_RATE)


def date_factor(check_in: date, check_out: date) -> float:
    """Mean per-night surcharge across the nights actually booked.

    Keeps the price sensitive to *which* nights are booked without making the
    model per-night, which would complicate the state for little benefit.
    """
    nights = (check_out - check_in).days
    if nights <= 0:
        return 1.0
    total = 0.0
    for offset in range(nights):
        night = check_in + timedelta(days=offset)
        total += WEEKEND_SURCHARGE if night.weekday() in WEEKEND_WEEKDAYS else 1.0
    return total / nights


#: Rooms notionally on sale per tier before any are taken (V4).
INVENTORY_PER_TIER = 12


def rooms_left(city: str, check_in: date, tier_index: int) -> int:
    """How many rooms of one tier remain, deterministically (V4).

    Real inventory is the one thing a cached, deterministic optimizer cannot
    honestly fake, so this does not try to be plausible - it is a fixed
    function of city, date and tier that produces a realistic *shape*: the
    cheapest rooms go first, and some dates are much tighter than others.
    Randomness would break the determinism guarantee for no gain.
    """
    pressure = (sum(ord(c) for c in city) + check_in.toordinal()) % 7
    # The budget tier sells out soonest - which is what makes scarcity
    # interesting rather than uniform, since it is what most trips want.
    taken = pressure * 2 + (2 - tier_index) * 2
    return max(INVENTORY_PER_TIER - taken, 0)


def build_options(
    city: str,
    check_in: date,
    check_out: date,
    *,
    base_rate: float | None = None,
    date_variation: bool = True,
    simulate_scarcity: bool = False,
) -> list[AccommodationOption]:
    """Materialize every tier available in ``city`` for the given nights.

    ``base_rate`` overrides the built-in standard-tier price, which is how test
    fixtures pin exact accommodation economics. ``simulate_scarcity`` (V4)
    attaches room counts; without it every option reports availability as
    unknown, which is what a feed with no inventory data looks like.
    """
    if check_out <= check_in:
        return []
    rate = nightly_rate(city) if base_rate is None else base_rate
    base = rate * (date_factor(check_in, check_out) if date_variation else 1.0)
    options: list[AccommodationOption] = []
    for index, spec in enumerate(TIERS):
        options.append(
            AccommodationOption(
                id=(
                    f"{city}-{spec.tier.value}-"
                    f"{check_in.isoformat()}-{check_out.isoformat()}"
                ),
                city=city,
                name=f"{city} {spec.tier.value} stay",
                check_in=check_in,
                check_out=check_out,
                price_per_night=round(base * spec.price_multiplier, 2),
                capacity=spec.capacity,
                tier=spec.tier,
                accommodation_type=spec.accommodation_type,
                rating=spec.rating,
                location_score=spec.location_score,
                free_cancellation=spec.free_cancellation,
                rooms_available=(
                    rooms_left(city, check_in, index) if simulate_scarcity else None
                ),
            )
        )
    return options
