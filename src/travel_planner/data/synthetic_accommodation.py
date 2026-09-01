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

from ..models.accommodation import AccommodationOption, AccommodationTier

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

#: ``(tier, price multiplier, room capacity, rating)``.
TIERS: tuple[tuple[AccommodationTier, float, int, float], ...] = (
    (AccommodationTier.BUDGET, 0.65, 2, 0.45),
    (AccommodationTier.STANDARD, 1.00, 2, 0.70),
    (AccommodationTier.COMFORT, 1.55, 3, 0.90),
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


def build_options(
    city: str,
    check_in: date,
    check_out: date,
    *,
    base_rate: float | None = None,
    date_variation: bool = True,
) -> list[AccommodationOption]:
    """Materialize every tier available in ``city`` for the given nights.

    ``base_rate`` overrides the built-in standard-tier price, which is how test
    fixtures pin exact accommodation economics.
    """
    if check_out <= check_in:
        return []
    rate = nightly_rate(city) if base_rate is None else base_rate
    base = rate * (date_factor(check_in, check_out) if date_variation else 1.0)
    options: list[AccommodationOption] = []
    for tier, multiplier, capacity, rating in TIERS:
        options.append(
            AccommodationOption(
                id=(
                    f"{city}-{tier.value}-{check_in.isoformat()}-{check_out.isoformat()}"
                ),
                city=city,
                name=f"{city} {tier.value} stay",
                check_in=check_in,
                check_out=check_out,
                price_per_night=round(base * multiplier, 2),
                capacity=capacity,
                tier=tier,
                rating=rating,
            )
        )
    return options
