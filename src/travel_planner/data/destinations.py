"""Synthetic destination catalog and origin-airport dataset.

All metadata here is *synthetic*. The values are plausible but they are not
sourced from any real travel dataset and must not be presented as such.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from ..models.destination import Destination


@lru_cache(maxsize=4096)
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


@lru_cache(maxsize=4096)
def canonical_key(value: str) -> str:
    """Normalize *and* resolve aliases.

    Memoized: the search resolves the same handful of city names tens of
    thousands of times per run, and the underlying Unicode normalization is not
    free.
    """
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
    *,
    history: float,
    nature: float,
    nightlife: float,
    culture: float,
    food: float,
    architecture: float,
    shopping: float,
    museums: float,
    beaches: float,
    family_friendly: float,
    romance: float,
    adventure: float,
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
        architecture=architecture,
        shopping=shopping,
        museums=museums,
        beaches=beaches,
        family_friendly=family_friendly,
        romance=romance,
        adventure=adventure,
        recommended_min_days=min_days,
        recommended_max_days=max_days,
    )


#: The synthetic destination catalog.
#:
#: Twelve normalized attributes per city. The values are invented but not
#: arbitrary: they are shaped so that cities are genuinely *different* from each
#: other, because an experience model over identical cities cannot demonstrate
#: anything. Zurich is the nature/quality outlier, Berlin the nightlife/culture
#: one, Rome and Prague the history ones, Barcelona the only real beach city.
DESTINATIONS: tuple[Destination, ...] = (
    _destination(
        "London", "United Kingdom",
        history=0.85, nature=0.35, nightlife=0.90, culture=0.95, food=0.75,
        architecture=0.80, shopping=0.95, museums=0.98, beaches=0.00,
        family_friendly=0.80, romance=0.60, adventure=0.45,
        min_days=2.0, max_days=5.0,
    ),
    _destination(
        "Brussels", "Belgium",
        history=0.65, nature=0.30, nightlife=0.55, culture=0.70, food=0.85,
        architecture=0.75, shopping=0.60, museums=0.70, beaches=0.00,
        family_friendly=0.65, romance=0.50, adventure=0.30,
        min_days=1.0, max_days=3.0,
    ),
    _destination(
        "Paris", "France",
        history=0.90, nature=0.35, nightlife=0.75, culture=0.98, food=0.95,
        architecture=0.95, shopping=0.95, museums=1.00, beaches=0.00,
        family_friendly=0.70, romance=1.00, adventure=0.35,
        min_days=2.0, max_days=5.0,
    ),
    _destination(
        "Amsterdam", "Netherlands",
        history=0.70, nature=0.55, nightlife=0.85, culture=0.80, food=0.65,
        architecture=0.85, shopping=0.70, museums=0.90, beaches=0.20,
        family_friendly=0.70, romance=0.75, adventure=0.45,
        min_days=2.0, max_days=4.0,
    ),
    _destination(
        "Prague", "Czechia",
        history=0.95, nature=0.40, nightlife=0.85, culture=0.85, food=0.70,
        architecture=0.95, shopping=0.50, museums=0.70, beaches=0.00,
        family_friendly=0.65, romance=0.85, adventure=0.40,
        min_days=2.0, max_days=4.0,
    ),
    _destination(
        "Vienna", "Austria",
        history=0.92, nature=0.45, nightlife=0.60, culture=0.95, food=0.80,
        architecture=0.92, shopping=0.70, museums=0.95, beaches=0.00,
        family_friendly=0.75, romance=0.85, adventure=0.35,
        min_days=2.0, max_days=4.0,
    ),
    _destination(
        "Madrid", "Spain",
        history=0.80, nature=0.35, nightlife=0.90, culture=0.88, food=0.90,
        architecture=0.78, shopping=0.85, museums=0.92, beaches=0.00,
        family_friendly=0.70, romance=0.70, adventure=0.40,
        min_days=2.0, max_days=4.0,
    ),
    _destination(
        "Barcelona", "Spain",
        history=0.75, nature=0.70, nightlife=0.92, culture=0.90, food=0.88,
        architecture=0.95, shopping=0.85, museums=0.80, beaches=0.90,
        family_friendly=0.80, romance=0.80, adventure=0.65,
        min_days=2.0, max_days=5.0,
    ),
    _destination(
        "Milan", "Italy",
        history=0.70, nature=0.40, nightlife=0.70, culture=0.80, food=0.85,
        architecture=0.80, shopping=1.00, museums=0.75, beaches=0.00,
        family_friendly=0.55, romance=0.60, adventure=0.35,
        min_days=1.0, max_days=3.0,
    ),
    _destination(
        "Rome", "Italy",
        history=1.00, nature=0.35, nightlife=0.65, culture=0.95, food=0.92,
        architecture=1.00, shopping=0.75, museums=0.95, beaches=0.15,
        family_friendly=0.70, romance=0.90, adventure=0.40,
        min_days=3.0, max_days=5.0,
    ),
    _destination(
        "Dublin", "Ireland",
        history=0.65, nature=0.65, nightlife=0.88, culture=0.70, food=0.60,
        architecture=0.55, shopping=0.55, museums=0.60, beaches=0.25,
        family_friendly=0.65, romance=0.55, adventure=0.60,
        min_days=2.0, max_days=4.0,
    ),
    _destination(
        "Copenhagen", "Denmark",
        history=0.60, nature=0.60, nightlife=0.70, culture=0.78, food=0.85,
        architecture=0.75, shopping=0.70, museums=0.72, beaches=0.35,
        family_friendly=0.90, romance=0.70, adventure=0.45,
        min_days=2.0, max_days=4.0,
    ),
    _destination(
        "Budapest", "Hungary",
        history=0.85, nature=0.50, nightlife=0.90, culture=0.80, food=0.72,
        architecture=0.90, shopping=0.50, museums=0.70, beaches=0.00,
        family_friendly=0.60, romance=0.80, adventure=0.50,
        min_days=2.0, max_days=4.0,
    ),
    _destination(
        "Berlin", "Germany",
        history=0.90, nature=0.45, nightlife=0.95, culture=0.88, food=0.70,
        architecture=0.70, shopping=0.75, museums=0.95, beaches=0.10,
        family_friendly=0.65, romance=0.45, adventure=0.50,
        min_days=2.0, max_days=5.0,
    ),
    _destination(
        "Munich", "Germany",
        history=0.70, nature=0.75, nightlife=0.65, culture=0.75, food=0.80,
        architecture=0.70, shopping=0.70, museums=0.75, beaches=0.00,
        family_friendly=0.85, romance=0.55, adventure=0.70,
        min_days=1.0, max_days=3.0,
    ),
    _destination(
        "Zurich", "Switzerland",
        history=0.50, nature=0.90, nightlife=0.45, culture=0.65, food=0.70,
        architecture=0.55, shopping=0.80, museums=0.60, beaches=0.30,
        family_friendly=0.80, romance=0.65, adventure=0.85,
        min_days=1.0, max_days=3.0,
    ),
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
