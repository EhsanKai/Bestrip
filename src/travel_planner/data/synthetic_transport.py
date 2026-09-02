"""The synthetic European transport network.

Everything here is fabricated. Prices, durations and schedules are invented for
the MVP and deliberately shaped to exercise the optimizer - in particular they
contain the case required by the spec, where the *cheapest first leg* leads to
the *most expensive complete itinerary*:

* ``DUS -> London`` is the cheapest way out of the Rhineland (35/pp), but every
  return leg from London is punitive (78-88/pp).
* ``DUS -> Prague`` costs more up front (55/pp), yet ``Prague -> Vienna`` (12/pp
  by bus) and ``Vienna -> DUS`` (25/pp) make the complete round trip far
  cheaper.

A greedy "cheapest next hop" search therefore commits to London and loses;
a search over complete itineraries finds Prague + Vienna.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..models.transport import TransportOption, TransportType


@dataclass(frozen=True, slots=True)
class Connection:
    """A directed, scheduled connection template between two network nodes."""

    origin: str
    destination: str
    transport_type: TransportType
    price_per_person: float
    duration_minutes: int
    departure_times: tuple[str, ...]


#: Default daily departure times per transport mode.
DEFAULT_TIMES: dict[TransportType, tuple[str, ...]] = {
    TransportType.FLIGHT: ("07:30", "13:10", "18:45"),
    TransportType.TRAIN: ("06:40", "11:20", "16:50"),
    TransportType.BUS: ("08:15", "21:30"),
}

#: Deterministic per-date price multipliers, indexed by ``date.toordinal() % 7``.
#: Real providers vary prices by demand; this keeps that shape without any
#: randomness, so two identical runs always produce identical results.
DATE_PRICE_FACTORS: tuple[float, ...] = (1.00, 1.08, 0.92, 1.12, 0.88, 1.05, 0.96)

#: Multiplier per departure slot: the first departure of the day is cheapest.
SLOT_PRICE_FACTORS: tuple[float, ...] = (0.95, 1.00, 1.10)


def _link(
    a: str,
    b: str,
    transport_type: TransportType,
    price_ab: float,
    price_ba: float,
    duration_minutes: int,
    times: tuple[str, ...] | None = None,
) -> tuple[Connection, Connection]:
    """Build the two directed connections of a bidirectional link.

    Outbound and return prices are given separately on purpose: asymmetric
    pricing is what makes a "cheap first leg" trap possible.
    """
    slots = times or DEFAULT_TIMES[transport_type]
    return (
        Connection(a, b, transport_type, price_ab, duration_minutes, slots),
        Connection(b, a, transport_type, price_ba, duration_minutes, slots),
    )


FLIGHT = TransportType.FLIGHT
TRAIN = TransportType.TRAIN
BUS = TransportType.BUS

_LINKS: tuple[tuple[Connection, Connection], ...] = (
    # --- Düsseldorf -------------------------------------------------------
    # The trap: cheapest departure of the whole network, brutal return.
    _link("DUS", "London", FLIGHT, 35.0, 85.0, 90),
    _link("DUS", "Prague", FLIGHT, 55.0, 48.0, 75),
    _link("DUS", "Vienna", FLIGHT, 60.0, 25.0, 95),
    _link("DUS", "Madrid", FLIGHT, 55.0, 58.0, 170),
    _link("DUS", "Barcelona", FLIGHT, 52.0, 60.0, 150),
    _link("DUS", "Milan", FLIGHT, 48.0, 52.0, 105),
    _link("DUS", "Rome", FLIGHT, 58.0, 62.0, 140),
    _link("DUS", "Dublin", FLIGHT, 45.0, 70.0, 105),
    _link("DUS", "Copenhagen", FLIGHT, 50.0, 46.0, 85),
    _link("DUS", "Budapest", FLIGHT, 54.0, 40.0, 100),
    _link("DUS", "Berlin", FLIGHT, 40.0, 38.0, 70),
    _link("DUS", "Munich", FLIGHT, 42.0, 44.0, 65),
    _link("DUS", "Zurich", FLIGHT, 46.0, 48.0, 70),
    _link("DUS", "Paris", TRAIN, 30.0, 30.0, 280),
    _link("DUS", "Brussels", TRAIN, 30.0, 32.0, 170),
    _link("DUS", "Amsterdam", TRAIN, 26.0, 28.0, 145),
    # --- Köln/Bonn --------------------------------------------------------
    _link("CGN", "London", FLIGHT, 42.0, 78.0, 95),
    _link("CGN", "Prague", FLIGHT, 58.0, 52.0, 80),
    _link("CGN", "Vienna", FLIGHT, 62.0, 34.0, 100),
    _link("CGN", "Madrid", FLIGHT, 48.0, 52.0, 175),
    _link("CGN", "Barcelona", FLIGHT, 55.0, 58.0, 155),
    _link("CGN", "Milan", FLIGHT, 50.0, 54.0, 110),
    _link("CGN", "Rome", FLIGHT, 60.0, 64.0, 145),
    _link("CGN", "Berlin", FLIGHT, 38.0, 40.0, 75),
    _link("CGN", "Munich", FLIGHT, 40.0, 42.0, 70),
    _link("CGN", "Copenhagen", FLIGHT, 52.0, 50.0, 90),
    _link("CGN", "Paris", TRAIN, 25.0, 25.0, 290),
    _link("CGN", "Brussels", TRAIN, 28.0, 30.0, 175),
    _link("CGN", "Amsterdam", TRAIN, 28.0, 30.0, 155),
    _link("CGN", "Zurich", TRAIN, 55.0, 58.0, 300),
    # --- Frankfurt --------------------------------------------------------
    _link("FRA", "London", FLIGHT, 48.0, 82.0, 100),
    _link("FRA", "Prague", FLIGHT, 50.0, 46.0, 70),
    _link("FRA", "Vienna", FLIGHT, 55.0, 38.0, 90),
    _link("FRA", "Madrid", FLIGHT, 60.0, 64.0, 165),
    _link("FRA", "Barcelona", FLIGHT, 58.0, 60.0, 145),
    _link("FRA", "Rome", FLIGHT, 55.0, 58.0, 130),
    _link("FRA", "Milan", FLIGHT, 52.0, 55.0, 95),
    _link("FRA", "Budapest", FLIGHT, 50.0, 44.0, 95),
    _link("FRA", "Berlin", FLIGHT, 42.0, 44.0, 70),
    _link("FRA", "Copenhagen", FLIGHT, 55.0, 52.0, 95),
    _link("FRA", "Dublin", FLIGHT, 52.0, 72.0, 120),
    _link("FRA", "Munich", TRAIN, 45.0, 45.0, 190),
    _link("FRA", "Zurich", TRAIN, 48.0, 50.0, 235),
    # --- Eindhoven --------------------------------------------------------
    _link("EIN", "London", FLIGHT, 32.0, 88.0, 80),
    _link("EIN", "Barcelona", FLIGHT, 40.0, 62.0, 145),
    _link("EIN", "Milan", FLIGHT, 42.0, 58.0, 100),
    _link("EIN", "Budapest", FLIGHT, 44.0, 46.0, 105),
    _link("EIN", "Copenhagen", FLIGHT, 46.0, 48.0, 90),
    _link("EIN", "Dublin", FLIGHT, 38.0, 74.0, 95),
    _link("EIN", "Vienna", FLIGHT, 50.0, 36.0, 100),
    _link("EIN", "Prague", FLIGHT, 48.0, 44.0, 85),
    _link("EIN", "Madrid", FLIGHT, 45.0, 66.0, 165),
    _link("EIN", "Rome", FLIGHT, 46.0, 60.0, 135),
    _link("EIN", "Berlin", FLIGHT, 36.0, 42.0, 80),
    # --- Amsterdam Schiphol ----------------------------------------------
    _link("AMS", "London", FLIGHT, 36.0, 80.0, 75),
    _link("AMS", "Madrid", FLIGHT, 50.0, 55.0, 160),
    _link("AMS", "Barcelona", FLIGHT, 48.0, 54.0, 140),
    _link("AMS", "Prague", FLIGHT, 46.0, 44.0, 90),
    _link("AMS", "Rome", FLIGHT, 50.0, 55.0, 145),
    _link("AMS", "Copenhagen", FLIGHT, 44.0, 42.0, 80),
    _link("AMS", "Berlin", FLIGHT, 34.0, 36.0, 80),
    _link("AMS", "Vienna", FLIGHT, 52.0, 40.0, 105),
    # --- City-to-city hops ------------------------------------------------
    _link("London", "Brussels", TRAIN, 45.0, 48.0, 140),
    _link("London", "Brussels", BUS, 22.0, 24.0, 330),
    _link("London", "Paris", TRAIN, 40.0, 42.0, 150),
    _link("London", "Paris", BUS, 25.0, 26.0, 420),
    _link("London", "Dublin", FLIGHT, 30.0, 32.0, 85),
    _link("London", "Amsterdam", FLIGHT, 34.0, 36.0, 80),
    _link("Brussels", "Amsterdam", TRAIN, 22.0, 22.0, 110),
    _link("Brussels", "Paris", TRAIN, 20.0, 20.0, 85),
    _link("Paris", "Barcelona", TRAIN, 45.0, 48.0, 390),
    _link("Paris", "Barcelona", FLIGHT, 42.0, 44.0, 105),
    _link("Paris", "Milan", TRAIN, 48.0, 50.0, 420),
    _link("Barcelona", "Madrid", TRAIN, 35.0, 35.0, 170),
    _link("Barcelona", "Madrid", BUS, 18.0, 18.0, 450),
    _link("Barcelona", "Milan", FLIGHT, 30.0, 32.0, 95),
    _link("Madrid", "Rome", FLIGHT, 45.0, 48.0, 150),
    _link("Milan", "Rome", TRAIN, 28.0, 28.0, 185),
    _link("Milan", "Zurich", TRAIN, 30.0, 30.0, 200),
    _link("Milan", "Vienna", FLIGHT, 42.0, 44.0, 95),
    _link("Zurich", "Munich", TRAIN, 32.0, 32.0, 210),
    _link("Munich", "Vienna", TRAIN, 30.0, 28.0, 255),
    _link("Munich", "Vienna", BUS, 16.0, 16.0, 330),
    _link("Munich", "Prague", BUS, 18.0, 18.0, 300),
    _link("Munich", "Prague", TRAIN, 32.0, 32.0, 330),
    # The cheap hop that rescues the "expensive" Prague departure.
    _link("Prague", "Vienna", TRAIN, 20.0, 20.0, 240),
    _link("Prague", "Vienna", BUS, 12.0, 12.0, 270),
    _link("Vienna", "Budapest", TRAIN, 22.0, 22.0, 160),
    _link("Vienna", "Budapest", BUS, 14.0, 14.0, 200),
    _link("Budapest", "Prague", TRAIN, 30.0, 30.0, 420),
    _link("Berlin", "Prague", BUS, 16.0, 16.0, 270),
    _link("Berlin", "Prague", TRAIN, 30.0, 30.0, 255),
    _link("Berlin", "Copenhagen", TRAIN, 42.0, 44.0, 420),
    _link("Berlin", "Copenhagen", FLIGHT, 38.0, 40.0, 75),
    _link("Berlin", "Munich", TRAIN, 38.0, 38.0, 240),
    _link("Berlin", "Amsterdam", TRAIN, 40.0, 42.0, 380),
    _link("Copenhagen", "Amsterdam", FLIGHT, 40.0, 42.0, 85),
    _link("Rome", "Vienna", FLIGHT, 44.0, 46.0, 110),
    _link("Dublin", "Amsterdam", FLIGHT, 32.0, 34.0, 95),
)

#: Every directed connection template in the synthetic network.
CONNECTIONS: tuple[Connection, ...] = tuple(
    connection for pair in _LINKS for connection in pair
)

#: The date range the synthetic timetable covers.
NETWORK_START = date(2026, 9, 1)
NETWORK_END = date(2026, 9, 30)


def _parse_time(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


#: Seats notionally on sale per departure before any are taken (V4).
INVENTORY_PER_DEPARTURE = 6


def seats_left(connection: "Connection", departure_date: date, slot: int) -> int:
    """Seats remaining on one departure, deterministically (V4).

    A fixed function of route, date and slot rather than anything random: the
    engine's determinism guarantee is worth more than a plausible-looking
    simulation. The shape is the realistic part - the cheap early slots on
    popular routes are the ones that go.
    """
    pressure = (
        sum(ord(c) for c in connection.origin + connection.destination)
        + departure_date.toordinal()
        + slot * 3
    ) % 8
    return max(INVENTORY_PER_DEPARTURE - pressure, 0)


def build_options(
    connection: Connection,
    departure_date: date,
    *,
    price_variation: bool = True,
    simulate_scarcity: bool = False,
) -> list[TransportOption]:
    """Materialize the timetable of ``connection`` on ``departure_date``.

    ``simulate_scarcity`` (V4) attaches seat counts; without it every fare
    reports availability as unknown, which is what a feed with no inventory
    data looks like.
    """
    options: list[TransportOption] = []
    date_factor = (
        DATE_PRICE_FACTORS[departure_date.toordinal() % len(DATE_PRICE_FACTORS)]
        if price_variation
        else 1.0
    )
    for slot, clock in enumerate(connection.departure_times):
        slot_factor = (
            SLOT_PRICE_FACTORS[slot % len(SLOT_PRICE_FACTORS)] if price_variation else 1.0
        )
        departure = datetime.combine(departure_date, _parse_time(clock))
        arrival = departure + timedelta(minutes=connection.duration_minutes)
        price = round(connection.price_per_person * date_factor * slot_factor, 2)
        options.append(
            TransportOption(
                id=(
                    f"{connection.origin}-{connection.destination}-"
                    f"{connection.transport_type.value}-"
                    f"{departure_date.isoformat()}-{slot}"
                ),
                origin=connection.origin,
                destination=connection.destination,
                departure=departure,
                arrival=arrival,
                price_per_person=price,
                transport_type=connection.transport_type,
                duration_minutes=connection.duration_minutes,
                operator=f"synthetic-{connection.transport_type.value}",
                seats_available=(
                    seats_left(connection, departure_date, slot)
                    if simulate_scarcity
                    else None
                ),
            )
        )
    return options
