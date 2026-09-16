"""Origin Intelligence: turning free-text origin input into places and
candidate departure airports (V9 Post-Phase-6, Search Integration + Origin
Intelligence Slice 1).

The product problem this exists to fix: origin resolution was a closed,
7-key hardcoded table clustered around Cologne/Düsseldorf
(``data/destinations.py::ORIGIN_DISTANCES_KM``) - anything else was a hard
422. This module makes origin resolution work over the *entire* ~203-city
discovery catalog instead, using the coordinates and IATA codes every
catalog entry already carries (see ``models/destination.py``) rather than a
parallel geography system.

Two operations, deliberately kept separate:

* :func:`resolve_origin_exact` - what actually decides which place a search
  runs from. Only an airport code, an exact/normalized name or a known alias
  may resolve directly. **Never** a fuzzy match - a typo must never silently
  redirect someone's trip.
* :func:`suggest_origins` - ranked candidates for an autocomplete box,
  including fuzzy/prefix matches. A suggestion is not a resolution; the
  caller (or the traveler) still has to pick one.

Nearby-airport discovery (:func:`nearby_airports`) is the separate backend
seam a future "include nearby departure airports" UX and browser-geolocation
flow will call - see the module docstring on why raw coordinates are never
persisted here.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

from ..data.destinations import DESTINATIONS, canonical_key, normalize_key
from ..models.destination import Destination
from .geo import haversine_km

#: Bounds shared with the API layer (api/contracts.py) - kept here too so a
#: caller that reaches this module directly (not only through the API) gets
#: the same protection against a pathological query.
MAX_QUERY_LENGTH = 120
MIN_FUZZY_QUERY_LENGTH = 3
#: SequenceMatcher ratio below which a candidate is not offered even as a
#: fuzzy suggestion - low enough to catch a plausible typo ("Dusseldrof"),
#: high enough that an unrelated word never appears.
FUZZY_THRESHOLD = 0.72
DEFAULT_SUGGESTION_LIMIT = 8
MAX_SUGGESTION_LIMIT = 25


class MatchType(str, Enum):
    """Why a candidate was offered, ordered strongest-evidence first.

    Only :attr:`AIRPORT_CODE`, :attr:`EXACT_NAME`, :attr:`NORMALIZED_NAME`
    and :attr:`ALIAS` are strong enough for :func:`resolve_origin_exact` to
    resolve on their own. :attr:`PREFIX` and :attr:`FUZZY` are suggestion-only.
    """

    AIRPORT_CODE = "AIRPORT_CODE"
    EXACT_NAME = "EXACT_NAME"
    NORMALIZED_NAME = "NORMALIZED_NAME"
    ALIAS = "ALIAS"
    PREFIX = "PREFIX"
    FUZZY = "FUZZY"


#: Match types strong enough to resolve a search origin directly, without
#: the traveler confirming a suggestion.
_EXACT_MATCH_TYPES = frozenset(
    {MatchType.AIRPORT_CODE, MatchType.EXACT_NAME, MatchType.NORMALIZED_NAME, MatchType.ALIAS}
)

#: Rank used to order suggestions when several match types tie on a place -
#: lower is better. Mirrors the ranking §6 asks for: airport code, exact/
#: local name, normalized name, prefix, alias, fuzzy.
_MATCH_TYPE_RANK: dict[MatchType, int] = {
    MatchType.AIRPORT_CODE: 0,
    MatchType.EXACT_NAME: 1,
    MatchType.NORMALIZED_NAME: 2,
    MatchType.PREFIX: 3,
    MatchType.ALIAS: 4,
    MatchType.FUZZY: 5,
}


class InvalidOriginQuery(ValueError):
    """The query itself is unusable (empty, too long, malformed Unicode) -
    distinct from a query that is well-formed but matches nothing."""


@dataclass(frozen=True, slots=True)
class OriginPlace:
    """A canonical place a search may start from, with enough information to
    both resolve it and offer it as a departure airport.

    Deliberately reuses the existing ~203-city ``Destination`` catalog
    (:mod:`detoura.data.destinations`) rather than a parallel geography
    model (§4: "If an existing destination/location model can safely serve
    this purpose, extend/reuse it"). A place a traveler may *visit* and a
    place a traveler may *depart from* are the same kind of fact.
    """

    id: str
    canonical_name: str
    country: str
    country_code: str | None
    latitude: float
    longitude: float
    primary_airport: str


def _place_from_destination(d: Destination) -> OriginPlace | None:
    if d.latitude is None or d.longitude is None or not d.primary_airport:
        return None
    return OriginPlace(
        id=d.id,
        canonical_name=d.name,
        country=d.country,
        country_code=d.country_code,
        latitude=d.latitude,
        longitude=d.longitude,
        primary_airport=d.primary_airport,
    )


def catalog_places() -> tuple[OriginPlace, ...]:
    """Every catalog destination usable as an origin: has coordinates and a
    primary airport. Computed fresh (not cached at import time) so a test
    that builds a custom catalog subset sees consistent behaviour; the
    catalog itself is a small, static, in-memory tuple, so this is cheap."""
    places = [_place_from_destination(d) for d in DESTINATIONS]
    return tuple(p for p in places if p is not None)


#: The default, whole-catalog place set every public function in this module
#: uses unless a caller passes its own (tests do, to keep fixtures small and
#: deterministic without depending on the real catalog's exact contents).
_DEFAULT_PLACES: tuple[OriginPlace, ...] = catalog_places()

#: ``airport code -> place`` for exact IATA lookups. Built once from the
#: default catalog; a caller with a custom place list gets its own index via
#: :func:`_airport_index`.
_AIRPORT_INDEX: dict[str, OriginPlace] = {p.primary_airport: p for p in _DEFAULT_PLACES}


def _airport_index(places: tuple[OriginPlace, ...]) -> dict[str, OriginPlace]:
    if places is _DEFAULT_PLACES:
        return _AIRPORT_INDEX
    return {p.primary_airport: p for p in places}


def _validate_query(query: str) -> str:
    """Bounded, defensive query validation (§16/§19).

    Raises :class:`InvalidOriginQuery` for anything that cannot be a real
    origin query at all - empty, absurdly long, or Unicode that cannot be
    normalized. Never raises for a merely *unmatched* query; that is a
    ``0``-result answer, not an error.
    """
    if not isinstance(query, str):
        raise InvalidOriginQuery("query must be a string")
    if len(query) > MAX_QUERY_LENGTH:
        raise InvalidOriginQuery(
            f"query is too long ({len(query)} > {MAX_QUERY_LENGTH} characters)"
        )
    try:
        unicodedata.normalize("NFKD", query)
    except (ValueError, TypeError) as error:
        raise InvalidOriginQuery("query contains malformed Unicode") from error
    stripped = query.strip()
    if not stripped:
        raise InvalidOriginQuery("query must not be empty")
    return stripped


def resolve_origin_exact(
    query: str, *, places: tuple[OriginPlace, ...] | None = None,
) -> OriginPlace | None:
    """Resolve ``query`` to exactly one place, or ``None``.

    Only ever returns on strong evidence - an airport code, an exact name,
    a diacritic/case-normalized name, or a known alias (see
    ``data/destinations.py::ALIASES``). **Never a fuzzy or prefix match** -
    "Dusseldrof" must come back as a suggestion, never as a silent
    resolution to Düsseldorf (§5: "TYPO MATCHING MUST NOT SILENTLY CHANGE
    THE USER'S ORIGIN").
    """
    catalog = places if places is not None else _DEFAULT_PLACES
    try:
        stripped = _validate_query(query)
    except InvalidOriginQuery:
        return None

    upper = stripped.upper()
    if len(upper) == 3 and upper.isalpha():
        hit = _airport_index(catalog).get(upper)
        if hit is not None:
            return hit
        # Fall through: a 3-letter string that is not a known code might
        # still be an exact/normalized city name (rare, but cheap to allow).

    key = normalize_key(stripped)
    canon = canonical_key(stripped)
    for place in catalog:
        if normalize_key(place.canonical_name) == key:
            return place
    for place in catalog:
        # ALIASES maps a spelling variant to a *canonical key* (e.g.
        # "koeln"/"koln" -> "koln"), which is not necessarily the catalog
        # entry's own normalized name (the catalog names this place
        # "Cologne", not "Köln") - so the comparison must canonicalize the
        # place's own name too, the same way ``DESTINATION_INDEX`` already
        # does in ``data/destinations.py``.
        if canonical_key(place.canonical_name) == canon or canon == canonical_key(place.id):
            return place
    return None


def _match_place(query_key: str, query_canon: str, query_raw_lower: str, place: OriginPlace) -> tuple[MatchType, float] | None:
    """The single best match type/score for one place against a query, or
    ``None`` if it does not qualify as a suggestion at all."""
    name_key = normalize_key(place.canonical_name)
    if place.primary_airport.lower() == query_raw_lower:
        return MatchType.AIRPORT_CODE, 1.0
    if place.canonical_name.strip().casefold() == query_raw_lower:
        return MatchType.EXACT_NAME, 1.0
    if name_key == query_key:
        return MatchType.NORMALIZED_NAME, 0.98
    place_canon = canonical_key(place.canonical_name)
    if place_canon == query_canon or query_canon == canonical_key(place.id):
        return MatchType.ALIAS, 0.95
    if query_key and name_key.startswith(query_key):
        # Prefix strength scales gently with how much of the name the query
        # already covers, so "Duss" ranks above "D" among prefix matches.
        coverage = len(query_key) / max(len(name_key), 1)
        return MatchType.PREFIX, 0.80 + 0.15 * min(1.0, coverage)
    if len(query_key) >= MIN_FUZZY_QUERY_LENGTH:
        ratio = SequenceMatcher(None, query_key, name_key).ratio()
        if ratio >= FUZZY_THRESHOLD:
            return MatchType.FUZZY, ratio
    return None


@dataclass(frozen=True, slots=True)
class OriginSuggestion:
    place: OriginPlace
    match_type: MatchType
    score: float


def suggest_origins(
    query: str,
    *,
    places: tuple[OriginPlace, ...] | None = None,
    limit: int = DEFAULT_SUGGESTION_LIMIT,
) -> list[OriginSuggestion]:
    """Ranked origin suggestions for an autocomplete box (§6).

    Deterministic: identical inputs (including the catalog) always produce
    identical output in identical order (§17) - ties are broken by
    ``(match_type rank, -score, place.id)`` so ordering never depends on
    dict/set iteration order.

    Raises :class:`InvalidOriginQuery` for an empty/oversized/malformed
    query; returns an empty list (not an error) for a well-formed query that
    matches nothing.
    """
    catalog = places if places is not None else _DEFAULT_PLACES
    bounded_limit = max(1, min(int(limit), MAX_SUGGESTION_LIMIT))
    stripped = _validate_query(query)

    query_key = normalize_key(stripped)
    query_canon = canonical_key(stripped)
    query_lower = stripped.casefold()

    scored: list[OriginSuggestion] = []
    for place in catalog:
        match = _match_place(query_key, query_canon, query_lower, place)
        if match is not None:
            match_type, score = match
            scored.append(OriginSuggestion(place=place, match_type=match_type, score=score))

    scored.sort(key=lambda s: (_MATCH_TYPE_RANK[s.match_type], -s.score, s.place.id))
    return scored[:bounded_limit]


@dataclass(frozen=True, slots=True)
class NearbyAirportPolicy:
    """Bounded, explainable nearby-airport expansion (§11).

    Deliberately conservative defaults: a real product decision (how far is
    "nearby") stays a policy value here, not a constant folded into the
    algorithm, so it can be revisited without touching the search code
    (§11: "Keep policy configurable/domain-level where reasonable")."""

    max_radius_km: float = 150.0
    max_candidates: int = 5


DEFAULT_NEARBY_POLICY = NearbyAirportPolicy()


@dataclass(frozen=True, slots=True)
class NearbyAirport:
    code: str
    name: str
    city: str
    country: str
    distance_km: float


def _validate_coordinate(latitude: float, longitude: float) -> None:
    for label, value, low, high in (
        ("latitude", latitude, -90.0, 90.0),
        ("longitude", longitude, -180.0, 180.0),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{label} must be a number")
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"{label} must be a finite number")
        if not (low <= value <= high):
            raise ValueError(f"{label} {value} is out of range [{low}, {high}]")


def nearby_airports(
    latitude: float,
    longitude: float,
    *,
    places: tuple[OriginPlace, ...] | None = None,
    policy: NearbyAirportPolicy | None = None,
) -> list[NearbyAirport]:
    """Eligible departure airports near ``(latitude, longitude)`` (§9/§10/§11).

    Stateless: this function never persists the coordinates it is called
    with (§14) - the caller decides what, if anything, to do with the
    result. Real great-circle distance (:func:`~detoura.services.geo.haversine_km`),
    never a hardcoded display value (§10). Deterministic, bounded, sorted
    nearest-first with an explicit tie-break on airport code (§17).

    Raises ``ValueError`` for a non-finite or out-of-range coordinate - the
    API layer turns this into a 422, never a silent clamp or a 500 (§16).
    """
    _validate_coordinate(latitude, longitude)
    catalog = places if places is not None else _DEFAULT_PLACES
    pol = policy or DEFAULT_NEARBY_POLICY
    max_radius = max(0.0, float(pol.max_radius_km))
    max_candidates = max(1, int(pol.max_candidates))

    best_by_airport: dict[str, NearbyAirport] = {}
    for place in catalog:
        distance = haversine_km(latitude, longitude, place.latitude, place.longitude)
        if distance > max_radius:
            continue
        existing = best_by_airport.get(place.primary_airport)
        candidate = NearbyAirport(
            code=place.primary_airport,
            name=place.canonical_name,
            city=place.canonical_name,
            country=place.country,
            distance_km=round(distance, 2),
        )
        if existing is None or candidate.distance_km < existing.distance_km:
            best_by_airport[place.primary_airport] = candidate

    ranked = sorted(best_by_airport.values(), key=lambda a: (a.distance_km, a.code))
    return ranked[:max_candidates]
