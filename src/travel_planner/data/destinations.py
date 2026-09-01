"""Synthetic destination catalog and origin-airport dataset.

All metadata here is *synthetic*. The values are plausible but they are not
sourced from any real travel dataset and must not be presented as such.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from ..models.destination import Destination


def normalize_key(value: str) -> str:
    """Fold a user-supplied place name into a lookup key.

    Strips accents, casing and punctuation so ``"Köln"``, ``"koln"`` and
    ``"KÖLN "`` all collapse to ``"koln"``.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in stripped.casefold() if ch.isalnum())


#: Common spellings that should resolve to a canonical destination/origin key.
ALIASES: dict[str, str] = {
    "koeln": "koln",
    "cologne": "koln",
    "dusseldorf": "dusseldorf",
    "duesseldorf": "dusseldorf",
    "wien": "vienna",
    "prag": "prague",
    "praha": "prague",
    "muenchen": "munich",
    "munchen": "munich",
    "roma": "rome",
    "milano": "milan",
    "lisboa": "lisbon",
    "kobenhavn": "copenhagen",
    "kopenhagen": "copenhagen",
    "bruxelles": "brussels",
    "brussel": "brussels",
    "bruessel": "brussels",
    "wenen": "vienna",
    "zuerich": "zurich",
    "londres": "london",
    "parijs": "paris",
    "budapest": "budapest",
    "frankfurtammain": "frankfurt",
}


def canonical_key(value: str) -> str:
    """Normalize *and* resolve aliases."""
    key = normalize_key(value)
    return ALIASES.get(key, key)


@dataclass(frozen=True, slots=True)
class OriginAirport:
    """A departure node the trip may start from and must return to."""

    code: str
    name: str
    city: str
    country: str


#: Departure nodes in the synthetic network. These are modelled as their own
#: graph nodes, distinct from the destination cities, so that "flying out of
#: AMS" never silently counts as "visiting Amsterdam".
ORIGIN_AIRPORTS: tuple[OriginAirport, ...] = (
    OriginAirport("CGN", "Köln/Bonn", "Köln", "Germany"),
    OriginAirport("DUS", "Düsseldorf", "Düsseldorf", "Germany"),
    OriginAirport("FRA", "Frankfurt am Main", "Frankfurt", "Germany"),
    OriginAirport("EIN", "Eindhoven", "Eindhoven", "Netherlands"),
    OriginAirport("AMS", "Amsterdam Schiphol", "Amsterdam", "Netherlands"),
)

#: Road distance in km from a home city to each candidate departure airport.
#: A real implementation would replace this with a geocoding/routing service;
#: :class:`~travel_planner.services.origin_resolver.OriginResolver` only ever
#: sees the resulting distances.
ORIGIN_DISTANCES_KM: dict[str, dict[str, float]] = {
    "koln": {"CGN": 15.0, "DUS": 40.0, "FRA": 155.0, "EIN": 170.0, "AMS": 260.0},
    "dusseldorf": {"DUS": 10.0, "CGN": 40.0, "EIN": 105.0, "AMS": 225.0, "FRA": 190.0},
    "frankfurt": {"FRA": 15.0, "CGN": 155.0, "DUS": 190.0, "EIN": 290.0, "AMS": 370.0},
    "eindhoven": {"EIN": 10.0, "AMS": 125.0, "DUS": 105.0, "CGN": 170.0, "FRA": 290.0},
    "amsterdam": {"AMS": 15.0, "EIN": 125.0, "DUS": 225.0, "CGN": 260.0, "FRA": 370.0},
    "bonn": {"CGN": 30.0, "DUS": 70.0, "FRA": 145.0, "EIN": 195.0, "AMS": 285.0},
    "aachen": {"CGN": 75.0, "DUS": 90.0, "EIN": 100.0, "FRA": 225.0, "AMS": 250.0},
}


def _destination(
    id_: str,
    country: str,
    history: float,
    nature: float,
    nightlife: float,
    culture: float,
    food: float,
    min_days: float,
    max_days: float,
) -> Destination:
    return Destination(
        id=id_,
        name=id_,
        country=country,
        history=history,
        nature=nature,
        nightlife=nightlife,
        culture=culture,
        food=food,
        recommended_min_days=min_days,
        recommended_max_days=max_days,
    )


#: The synthetic destination catalog.
DESTINATIONS: tuple[Destination, ...] = (
    #            id            country          hist  nat   night cult  food  min  max
    _destination("London", "United Kingdom", 0.85, 0.35, 0.90, 0.95, 0.75, 2.0, 5.0),
    _destination("Brussels", "Belgium", 0.65, 0.30, 0.55, 0.70, 0.85, 1.0, 3.0),
    _destination("Paris", "France", 0.90, 0.35, 0.75, 0.98, 0.95, 2.0, 5.0),
    _destination("Amsterdam", "Netherlands", 0.70, 0.55, 0.85, 0.80, 0.65, 2.0, 4.0),
    _destination("Prague", "Czechia", 0.95, 0.40, 0.85, 0.85, 0.70, 2.0, 4.0),
    _destination("Vienna", "Austria", 0.92, 0.45, 0.60, 0.95, 0.80, 2.0, 4.0),
    _destination("Madrid", "Spain", 0.80, 0.35, 0.90, 0.88, 0.90, 2.0, 4.0),
    _destination("Barcelona", "Spain", 0.75, 0.70, 0.92, 0.90, 0.88, 2.0, 5.0),
    _destination("Milan", "Italy", 0.70, 0.40, 0.70, 0.80, 0.85, 1.0, 3.0),
    _destination("Rome", "Italy", 1.00, 0.35, 0.65, 0.95, 0.92, 3.0, 5.0),
    _destination("Dublin", "Ireland", 0.65, 0.65, 0.88, 0.70, 0.60, 2.0, 4.0),
    _destination("Copenhagen", "Denmark", 0.60, 0.60, 0.70, 0.78, 0.85, 2.0, 4.0),
    _destination("Budapest", "Hungary", 0.85, 0.50, 0.90, 0.80, 0.72, 2.0, 4.0),
    _destination("Berlin", "Germany", 0.80, 0.45, 0.95, 0.88, 0.70, 2.0, 5.0),
    _destination("Munich", "Germany", 0.70, 0.75, 0.65, 0.75, 0.80, 1.0, 3.0),
    _destination("Zurich", "Switzerland", 0.50, 0.90, 0.45, 0.65, 0.70, 1.0, 3.0),
)

#: ``canonical key -> destination id`` lookup, used to resolve user input.
DESTINATION_INDEX: dict[str, str] = {canonical_key(d.id): d.id for d in DESTINATIONS}

#: ``canonical key -> airport code`` lookup for origin airports.
AIRPORT_INDEX: dict[str, str] = {
    **{canonical_key(a.code): a.code for a in ORIGIN_AIRPORTS},
    **{canonical_key(a.city): a.code for a in ORIGIN_AIRPORTS},
}

#: Every node id in the synthetic transport graph.
ALL_NODES: tuple[str, ...] = tuple(a.code for a in ORIGIN_AIRPORTS) + tuple(
    d.id for d in DESTINATIONS
)
