"""Structured explanation factors (V2).

The optimizer states facts; it never writes prose. A future LLM explainer turns
these flags plus the numbers already on the itinerary into a sentence, and
because every flag is derived deterministically from the itinerary it cannot
drift from what was actually computed.
"""

from __future__ import annotations

from ..config import PlannerConfig
from ..data.destinations import canonical_key
from ..models.itinerary import BaselineResult, ExplanationFactor, Itinerary
from ..models.trip import TripRequest

#: Component score at or above which a factor is worth mentioning.
STRONG = 0.7
#: ... and below which the opposite is worth mentioning.
WEAK = 0.4

#: Budget share below which a trip is "leaving money on the table".
UNDERSPEND = 0.35
#: Share of the requested days that counts as using the window well.
GOOD_WINDOW_USE = 0.6
#: Accommodation share of total cost below which the stay was a bargain.
CHEAP_STAY_SHARE = 0.35


def explanation_factors(
    itinerary: Itinerary,
    request: TripRequest,
    config: PlannerConfig,
    baseline: BaselineResult | None = None,
) -> list[ExplanationFactor]:
    """Derive the reasons this itinerary is worth showing, in a stable order."""
    factors: list[ExplanationFactor] = [ExplanationFactor.FITS_BUDGET]
    value = itinerary.value_breakdown

    # --- money -------------------------------------------------------
    utilization = itinerary.total_cost / request.budget if request.budget else 0.0
    if utilization < UNDERSPEND:
        factors.append(ExplanationFactor.LEAVES_BUDGET_UNUSED)
    elif utilization <= 1.0:
        factors.append(ExplanationFactor.GOOD_BUDGET_USAGE)
    if baseline is not None and itinerary.total_cost < baseline.total_cost:
        factors.append(ExplanationFactor.CHEAPER_THAN_BASELINE)
    if itinerary.total_cost > 0:
        stay_share = itinerary.cost_breakdown.accommodation / itinerary.total_cost
        if 0 < stay_share < CHEAP_STAY_SHARE:
            factors.append(ExplanationFactor.LOW_ACCOMMODATION_COST)

    # --- taste and places --------------------------------------------
    if value is not None:
        if value.preferences >= STRONG:
            factors.append(ExplanationFactor.STRONG_PREFERENCE_MATCH)
        if value.experience >= STRONG:
            factors.append(ExplanationFactor.HIGH_DESTINATION_QUALITY)

    count = len(itinerary.cities)
    if count == 1:
        factors.append(ExplanationFactor.SINGLE_CITY)
    elif count == 2:
        factors.append(ExplanationFactor.TWO_CITIES)
    else:
        factors.append(ExplanationFactor.MULTI_CITY)

    visited = {canonical_key(city) for city in itinerary.cities}
    if visited & {canonical_key(name) for name in request.preferred_destinations}:
        factors.append(ExplanationFactor.VISITS_PREFERRED_DESTINATION)
    if request.must_visit and visited >= {
        canonical_key(name) for name in request.must_visit
    }:
        factors.append(ExplanationFactor.VISITS_MANDATORY_DESTINATION)

    # --- time ---------------------------------------------------------
    elapsed = max((itinerary.arrival - itinerary.departure).total_seconds() / 60, 1)
    if itinerary.total_transport_minutes / elapsed <= config.max_travel_time_fraction:
        factors.append(ExplanationFactor.REASONABLE_TRAVEL_TIME)
    else:
        factors.append(ExplanationFactor.HEAVY_TRAVEL_TIME)

    if value is not None:
        if value.usable_ratio >= GOOD_WINDOW_USE:
            factors.append(ExplanationFactor.GOOD_USE_OF_WINDOW)
        elif value.usable_ratio < WEAK:
            factors.append(ExplanationFactor.SHORT_TRIP)

    # A stay that begins after the usable day is over, or ends before it
    # starts, is worth flagging: the calendar days flatter it.
    for stay in itinerary.stays:
        if stay.arrival.time() >= config.usable_day_end:
            factors.append(ExplanationFactor.LATE_ARRIVAL)
            break
    for stay in itinerary.stays:
        if stay.departure.time() <= config.usable_day_start:
            factors.append(ExplanationFactor.EARLY_DEPARTURE)
            break

    # Preserve first-seen order while removing duplicates.
    seen: set[ExplanationFactor] = set()
    ordered: list[ExplanationFactor] = []
    for factor in factors:
        if factor not in seen:
            seen.add(factor)
            ordered.append(factor)
    return ordered
