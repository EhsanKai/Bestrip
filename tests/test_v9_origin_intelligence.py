"""V9 Post-Phase-6 Search Integration + Origin Intelligence Slice 1 — the
origin resolution/suggestion/nearby-airport layer.

Covers §19 of the slice spec: exact/normalized/IATA/prefix/typo/whitespace/
case resolution, ambiguity safety, unknown/empty/oversized/malformed-Unicode
input, deterministic ordering and result bounds, and nearby-airport lookup
(valid/invalid/NaN/Infinity coordinates, radius/candidate bounds, distance
ordering, cross-border candidates, the no-result case).
"""

from __future__ import annotations

import pytest

from detoura.services.origin_intelligence import (
    DEFAULT_NEARBY_POLICY,
    MAX_QUERY_LENGTH,
    InvalidOriginQuery,
    MatchType,
    NearbyAirportPolicy,
    OriginPlace,
    catalog_places,
    nearby_airports,
    resolve_origin_exact,
    suggest_origins,
)

# ---------------------------------------------------------------------------
# A small, fixed, deterministic fixture catalog - independent of the real
# ~203-city catalog's exact contents, so these tests do not silently break
# if a future slice adds/removes/renames a catalog destination.
# ---------------------------------------------------------------------------

_DUSSELDORF = OriginPlace(
    id="Dusseldorf", canonical_name="Dusseldorf", country="Germany",
    country_code="DE", latitude=51.23, longitude=6.78, primary_airport="DUS",
)
_COLOGNE = OriginPlace(
    id="Cologne", canonical_name="Cologne", country="Germany",
    country_code="DE", latitude=50.94, longitude=6.96, primary_airport="CGN",
)
_MAASTRICHT = OriginPlace(
    id="Maastricht", canonical_name="Maastricht", country="Netherlands",
    country_code="NL", latitude=50.85, longitude=5.69, primary_airport="MST",
)
_MADRID = OriginPlace(
    id="Madrid", canonical_name="Madrid", country="Spain",
    country_code="ES", latitude=40.42, longitude=-3.70, primary_airport="MAD",
)
_DUBLIN = OriginPlace(
    id="Dublin", canonical_name="Dublin", country="Ireland",
    country_code="IE", latitude=53.35, longitude=-6.26, primary_airport="DUB",
)

FIXTURE_PLACES = (_DUSSELDORF, _COLOGNE, _MAASTRICHT, _MADRID, _DUBLIN)


# ---------------------------------------------------------------------------
# Group A — exact resolution (never fuzzy/prefix)
# ---------------------------------------------------------------------------

def test_exact_canonical_name_resolves():
    assert resolve_origin_exact("Dusseldorf", places=FIXTURE_PLACES) == _DUSSELDORF


def test_diacritic_insensitive_resolution():
    assert resolve_origin_exact("Düsseldorf", places=FIXTURE_PLACES) == _DUSSELDORF


def test_iata_code_resolves_case_insensitively():
    for code in ("DUS", "dus", "Dus"):
        assert resolve_origin_exact(code, places=FIXTURE_PLACES) == _DUSSELDORF


def test_whitespace_is_normalized():
    assert resolve_origin_exact("  Düsseldorf  ", places=FIXTURE_PLACES) == _DUSSELDORF


def test_case_insensitive_name_resolution():
    assert resolve_origin_exact("DÜSSELDORF", places=FIXTURE_PLACES) == _DUSSELDORF
    assert resolve_origin_exact("düsseldorf", places=FIXTURE_PLACES) == _DUSSELDORF


def test_alias_resolves_to_the_catalog_entrys_own_name():
    """"Köln" is not the catalog's own spelling ("Cologne") - the alias table
    must bridge the two, matching the pre-existing DESTINATION_INDEX
    behaviour for destinations."""
    assert resolve_origin_exact("Köln", places=FIXTURE_PLACES) == _COLOGNE
    assert resolve_origin_exact("Koeln", places=FIXTURE_PLACES) == _COLOGNE
    assert resolve_origin_exact("cologne", places=FIXTURE_PLACES) == _COLOGNE


def test_prefix_input_does_not_resolve_exactly():
    """§5: a prefix is a suggestion, never an automatic resolution."""
    assert resolve_origin_exact("Duss", places=FIXTURE_PLACES) is None


def test_typo_does_not_silently_resolve():
    """§5, the headline invariant: 'Dusseldrof' must never silently become
    Düsseldorf via resolution - only via an explicit suggestion pick."""
    assert resolve_origin_exact("Dusseldrof", places=FIXTURE_PLACES) is None


def test_unknown_place_does_not_resolve():
    assert resolve_origin_exact("Atlantis", places=FIXTURE_PLACES) is None


def test_empty_input_does_not_resolve():
    assert resolve_origin_exact("", places=FIXTURE_PLACES) is None
    assert resolve_origin_exact("   ", places=FIXTURE_PLACES) is None


def test_very_long_input_does_not_resolve_and_does_not_raise():
    assert resolve_origin_exact("x" * (MAX_QUERY_LENGTH + 1), places=FIXTURE_PLACES) is None


def test_malformed_unicode_is_handled_without_raising():
    # A lone unpaired surrogate is not valid Unicode text; whether or not
    # normalization itself raises for it, resolve_origin_exact must degrade
    # to "no match", never propagate an exception to the caller.
    assert resolve_origin_exact("\udc80\udc81", places=FIXTURE_PLACES) is None


# ---------------------------------------------------------------------------
# Group B — suggestions (§6/§7)
# ---------------------------------------------------------------------------

def test_prefix_suggestion_for_duss():
    suggestions = suggest_origins("Duss", places=FIXTURE_PLACES)
    assert suggestions
    assert suggestions[0].place == _DUSSELDORF
    assert suggestions[0].match_type == MatchType.PREFIX


def test_typo_tolerance_suggests_the_intended_city():
    suggestions = suggest_origins("Dusseldrof", places=FIXTURE_PLACES)
    assert suggestions
    assert suggestions[0].place == _DUSSELDORF
    assert suggestions[0].match_type == MatchType.FUZZY


def test_unrelated_typo_input_does_not_suggest_an_unrelated_city():
    """A bounded fuzzy threshold: nonsense must not match anything, and
    especially must not match a place that merely shares a couple of
    letters."""
    suggestions = suggest_origins("zzzzqqqqxxxx", places=FIXTURE_PLACES)
    assert suggestions == []


def test_ranking_prefers_stronger_evidence_first():
    """An airport-code/exact match always outranks a fuzzy/prefix match on
    a different place, even if both are present."""
    suggestions = suggest_origins("DUS", places=FIXTURE_PLACES)
    assert suggestions[0].match_type == MatchType.AIRPORT_CODE
    assert suggestions[0].place == _DUSSELDORF


def test_suggestions_are_deterministic_across_repeated_calls():
    first = suggest_origins("Du", places=FIXTURE_PLACES, limit=10)
    second = suggest_origins("Du", places=FIXTURE_PLACES, limit=10)
    assert first == second


def test_suggestion_result_count_is_bounded():
    suggestions = suggest_origins("a", places=FIXTURE_PLACES, limit=2)
    assert len(suggestions) <= 2


def test_empty_query_raises_invalid_origin_query():
    with pytest.raises(InvalidOriginQuery):
        suggest_origins("", places=FIXTURE_PLACES)
    with pytest.raises(InvalidOriginQuery):
        suggest_origins("   ", places=FIXTURE_PLACES)


def test_oversized_query_raises_invalid_origin_query():
    with pytest.raises(InvalidOriginQuery):
        suggest_origins("x" * (MAX_QUERY_LENGTH + 1), places=FIXTURE_PLACES)


def test_malformed_unicode_query_degrades_gracefully_not_a_crash():
    """A lone surrogate is not valid Unicode text, but CPython's own
    ``unicodedata.normalize`` happens to pass it through rather than
    raising - the requirement here is "no crash, no pathological match",
    not a specific exception type."""
    assert suggest_origins("\udc80\udc81", places=FIXTURE_PLACES) == []


# ---------------------------------------------------------------------------
# Group C — nearby airport discovery (§9-§11/§19)
# ---------------------------------------------------------------------------

def test_nearby_finds_the_place_itself_and_a_close_neighbour():
    results = nearby_airports(
        _DUSSELDORF.latitude, _DUSSELDORF.longitude, places=FIXTURE_PLACES,
        policy=NearbyAirportPolicy(max_radius_km=150.0, max_candidates=5),
    )
    codes = [a.code for a in results]
    assert codes[0] == "DUS"
    assert results[0].distance_km == 0.0
    assert "CGN" in codes  # Cologne is genuinely close to Düsseldorf


def test_nearby_respects_the_radius_bound():
    """Madrid is nowhere near Düsseldorf - a tight radius must exclude it."""
    results = nearby_airports(
        _DUSSELDORF.latitude, _DUSSELDORF.longitude, places=FIXTURE_PLACES,
        policy=NearbyAirportPolicy(max_radius_km=100.0, max_candidates=10),
    )
    assert "MAD" not in [a.code for a in results]


def test_nearby_respects_the_candidate_count_bound():
    results = nearby_airports(
        _DUSSELDORF.latitude, _DUSSELDORF.longitude, places=FIXTURE_PLACES,
        policy=NearbyAirportPolicy(max_radius_km=10_000.0, max_candidates=2),
    )
    assert len(results) <= 2


def test_nearby_cross_border_candidate_is_included_when_close_enough():
    """Maastricht (Netherlands) is close enough to Düsseldorf (Germany) that
    a country border must not silently exclude it (§11: "country borders
    should not automatically exclude useful airports")."""
    results = nearby_airports(
        _DUSSELDORF.latitude, _DUSSELDORF.longitude, places=FIXTURE_PLACES,
        policy=NearbyAirportPolicy(max_radius_km=150.0, max_candidates=10),
    )
    assert "MST" in [a.code for a in results]


def test_nearby_is_sorted_by_distance_then_code():
    results = nearby_airports(
        _DUSSELDORF.latitude, _DUSSELDORF.longitude, places=FIXTURE_PLACES,
        policy=NearbyAirportPolicy(max_radius_km=10_000.0, max_candidates=10),
    )
    distances = [a.distance_km for a in results]
    assert distances == sorted(distances)


def test_nearby_no_result_case():
    """A radius small enough that nothing at all qualifies - not even the
    origin's own airport, if none is exactly at that coordinate."""
    results = nearby_airports(
        0.0, 0.0, places=FIXTURE_PLACES,
        policy=NearbyAirportPolicy(max_radius_km=1.0, max_candidates=5),
    )
    assert results == []


@pytest.mark.parametrize("lat,lon", [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0)])
def test_nearby_rejects_out_of_range_coordinates(lat, lon):
    with pytest.raises(ValueError):
        nearby_airports(lat, lon, places=FIXTURE_PLACES)


def test_nearby_rejects_nan_coordinates():
    with pytest.raises(ValueError):
        nearby_airports(float("nan"), 0.0, places=FIXTURE_PLACES)
    with pytest.raises(ValueError):
        nearby_airports(0.0, float("nan"), places=FIXTURE_PLACES)


def test_nearby_rejects_infinite_coordinates():
    with pytest.raises(ValueError):
        nearby_airports(float("inf"), 0.0, places=FIXTURE_PLACES)
    with pytest.raises(ValueError):
        nearby_airports(0.0, float("-inf"), places=FIXTURE_PLACES)


def test_nearby_deduplicates_by_airport_code():
    """Two catalog entries sharing an airport code must not produce two
    rows for the same code - the nearer one wins."""
    duplicate_code_place = OriginPlace(
        id="DusseldorfSuburb", canonical_name="Dusseldorf Suburb",
        country="Germany", country_code="DE", latitude=51.24, longitude=6.80,
        primary_airport="DUS",
    )
    places = FIXTURE_PLACES + (duplicate_code_place,)
    results = nearby_airports(
        _DUSSELDORF.latitude, _DUSSELDORF.longitude, places=places,
        policy=NearbyAirportPolicy(max_radius_km=150.0, max_candidates=10),
    )
    dus_rows = [a for a in results if a.code == "DUS"]
    assert len(dus_rows) == 1
    assert dus_rows[0].distance_km == 0.0


# ---------------------------------------------------------------------------
# Group D — sanity against the real catalog (not just the fixture)
# ---------------------------------------------------------------------------

def test_real_catalog_has_places():
    places = catalog_places()
    assert len(places) > 100  # the ~203-city catalog, minus any with no airport/coords


def test_real_catalog_dusseldorf_scenario():
    """The product spec's own worked example, against the real catalog."""
    place = resolve_origin_exact("Dusseldorf")
    assert place is not None
    assert place.primary_airport == "DUS"
    nearby = nearby_airports(place.latitude, place.longitude)
    codes = [a.code for a in nearby]
    assert "DUS" in codes
    assert "CGN" in codes


def test_real_catalog_koln_alias_resolves():
    place = resolve_origin_exact("Köln")
    assert place is not None
    assert place.primary_airport == "CGN"


def test_default_nearby_policy_is_bounded():
    assert DEFAULT_NEARBY_POLICY.max_radius_km > 0
    assert DEFAULT_NEARBY_POLICY.max_candidates >= 1
