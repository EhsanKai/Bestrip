"""Usable destination time (V2).

Lives at the package root rather than under ``algorithms/`` because
:mod:`detoura.config` needs the day-window constants, and ``algorithms``
depends on ``config``.

A day of a trip is not a day of sightseeing. Landing at 23:30 buys nothing, and
a 06:00 flight home costs the whole last morning. The planner therefore counts
the minutes that actually fall inside a configurable "usable day" window rather
than treating elapsed time as experience.

The calculation is deterministic and has no notion of jet lag, opening hours or
weather - it is an approximation, chosen to be explainable and testable.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

#: Sightseeing is assumed to be possible between these clock times.
DEFAULT_DAY_START = time(8, 0)
DEFAULT_DAY_END = time(21, 0)


def usable_day_minutes(day_start: time, day_end: time) -> int:
    """Length of one fully usable day, in minutes."""
    start = timedelta(hours=day_start.hour, minutes=day_start.minute)
    end = timedelta(hours=day_end.hour, minutes=day_end.minute)
    return max(int((end - start).total_seconds() // 60), 0)


def usable_minutes(
    arrival: datetime,
    departure: datetime,
    *,
    day_start: time = DEFAULT_DAY_START,
    day_end: time = DEFAULT_DAY_END,
) -> int:
    """Sightseeing minutes available in a city between arrival and departure.

    Each calendar day contributes the overlap between ``[day_start, day_end]``
    and ``[arrival, departure]``. This gives the required behaviour for free:

    * arriving after ``day_end`` contributes zero for the arrival day,
    * departing before ``day_start`` contributes zero for the departure day,
    * every day in between contributes a full :func:`usable_day_minutes`.
    """
    if departure <= arrival:
        return 0
    total = 0
    day = arrival.date()
    last = departure.date()
    while day <= last:
        window_start = max(datetime.combine(day, day_start), arrival)
        window_end = min(datetime.combine(day, day_end), departure)
        if window_end > window_start:
            total += int((window_end - window_start).total_seconds() // 60)
        day += timedelta(days=1)
    return total
