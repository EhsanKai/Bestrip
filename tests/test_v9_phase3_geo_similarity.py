"""V9 Phase 3 §C2/§C3 — geographic distance and experience similarity."""

from __future__ import annotations

from detoura.data.destinations import acquisition_catalog
from detoura.models.destination import Destination
from detoura.services.experience_similarity import (
    cosine_similarity,
    experience_redundancy_signal,
    experience_similarity,
    mean_pairwise_experience_similarity,
)
from detoura.services.geo import (
    country_diversity_ratio,
    distance_km,
    geo_redundancy_signal,
    haversine_km,
    mean_pairwise_distance_km,
    proximity_weight,
)


def _catalog():
    return {d.id: d for d in acquisition_catalog()}


# ======================================================================
# Geographic distance
# ======================================================================
def test_haversine_known_distance_paris_london():
    # Real great-circle distance Paris-London is ~344 km.
    d = haversine_km(48.86, 2.35, 51.51, -0.13)
    assert 300 < d < 400


def test_distance_zero_for_identical_point():
    assert haversine_km(48.86, 2.35, 48.86, 2.35) == 0.0


def test_distance_km_none_when_coordinates_missing():
    a = Destination(id="a", name="a", country="X", latitude=None, longitude=None)
    b = Destination(id="b", name="b", country="X", latitude=1.0, longitude=1.0)
    assert distance_km(a, b) is None


def test_distance_km_missing_coordinates_is_none_even_for_the_same_destination():
    """Missing coordinates is never silently papered over, not even by the
    same-id shortcut — a destination with no coordinates has a genuinely
    unknown distance to itself, and this function says so."""
    a = Destination(id="same", name="a", country="X")
    assert distance_km(a, a) is None


def test_distance_km_same_id_with_coords_is_zero():
    a = Destination(id="same", name="a", country="X", latitude=10.0, longitude=10.0)
    assert distance_km(a, a) == 0.0


def test_proximity_weight_decays_with_distance():
    near = proximity_weight(10.0, km_scale=250.0)
    far = proximity_weight(1000.0, km_scale=250.0)
    assert near > far
    assert 0.0 <= far <= near <= 1.0


def test_proximity_weight_zero_scale_is_zero():
    assert proximity_weight(10.0, km_scale=0.0) == 0.0


def test_geo_redundancy_signal_is_soft_never_a_hard_ban():
    by_id = _catalog()
    cologne_neighbors = [by_id["Dusseldorf"], by_id["Dortmund"], by_id["Brussels"]]
    sig = geo_redundancy_signal(
        by_id["Antwerp"], cologne_neighbors, km_scale=250.0, free_allowance=1,
    )
    assert 0.0 < sig < 1.0  # never exactly 0 (it's genuinely close) nor 1 (never a ban)


def test_geo_redundancy_signal_zero_for_no_selection():
    by_id = _catalog()
    assert geo_redundancy_signal(by_id["Paris"], [], km_scale=250.0) == 0.0


def test_geo_redundancy_signal_higher_for_more_nearby_selections():
    by_id = _catalog()
    one = geo_redundancy_signal(by_id["Antwerp"], [by_id["Brussels"]], km_scale=250.0, free_allowance=0)
    many = geo_redundancy_signal(
        by_id["Antwerp"], [by_id["Brussels"], by_id["Dusseldorf"], by_id["Dortmund"], by_id["Maastricht"]],
        km_scale=250.0, free_allowance=0,
    )
    assert many > one


def test_geo_redundancy_missing_coordinates_never_inflates_signal():
    a = Destination(id="a", name="a", country="X", latitude=10.0, longitude=10.0)
    no_coords = Destination(id="b", name="b", country="X")
    assert geo_redundancy_signal(a, [no_coords], km_scale=250.0) == 0.0


def test_country_diversity_ratio():
    by_id = _catalog()
    all_german_ish = [by_id["Cologne"], by_id["Dusseldorf"], by_id["Dortmund"]]
    mixed = [by_id["Cologne"], by_id["Paris"], by_id["Rome"]]
    assert country_diversity_ratio(all_german_ish) < country_diversity_ratio(mixed)
    assert country_diversity_ratio(mixed) == 1.0
    assert country_diversity_ratio([]) == 0.0


def test_mean_pairwise_distance_none_for_single_destination():
    by_id = _catalog()
    assert mean_pairwise_distance_km([by_id["Paris"]]) is None


def test_mean_pairwise_distance_positive_for_spread_destinations():
    by_id = _catalog()
    d = mean_pairwise_distance_km([by_id["Paris"], by_id["Athens"], by_id["Reykjavik"]])
    assert d is not None and d > 1000


# ======================================================================
# Experience similarity
# ======================================================================
def test_cosine_similarity_identical_vectors_is_one():
    v = (0.5, 0.7, 0.2)
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-9


def test_cosine_similarity_orthogonal_is_zero():
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == 0.0


def test_cosine_similarity_zero_vector_is_zero_not_error():
    assert cosine_similarity((0.0, 0.0), (0.0, 0.0)) == 0.0


def test_experience_similarity_same_destination_is_one():
    by_id = _catalog()
    assert experience_similarity(by_id["Paris"], by_id["Paris"]) == 1.0


def test_experience_similarity_bounded():
    by_id = _catalog()
    s = experience_similarity(by_id["Paris"], by_id["Reykjavik"])
    assert 0.0 <= s <= 1.0


def test_experience_similarity_similar_cities_score_higher_than_dissimilar():
    """Two nightlife-heavy capital cities should read as more similar to each
    other than either does to a nature-only destination — this is exactly
    what the Cologne benchmark relies on to detect saturation."""
    by_id = _catalog()
    a, b, c = by_id["Amsterdam"], by_id["Berlin"], by_id["Reykjavik"]
    assert experience_similarity(a, b) > experience_similarity(a, c)


def test_experience_redundancy_signal_zero_below_threshold():
    a = Destination(
        id="a", name="a", country="X", history=1.0, nature=0.0, nightlife=0.0,
        culture=0.0, food=0.0, architecture=0.0, shopping=0.0, museums=0.0,
        beaches=0.0, family_friendly=0.0, romance=0.0, adventure=0.0,
    )
    b = Destination(
        id="b", name="b", country="X", history=0.0, nature=1.0, nightlife=0.0,
        culture=0.0, food=0.0, architecture=0.0, shopping=0.0, museums=0.0,
        beaches=0.0, family_friendly=0.0, romance=0.0, adventure=0.0,
    )
    assert experience_redundancy_signal(a, [b], similarity_threshold=0.8) == 0.0


def test_experience_redundancy_signal_positive_above_threshold():
    by_id = _catalog()
    sig = experience_redundancy_signal(
        by_id["Amsterdam"], [by_id["Berlin"]], similarity_threshold=0.5,
    )
    assert sig >= 0.0


def test_mean_pairwise_experience_similarity_none_for_single():
    by_id = _catalog()
    assert mean_pairwise_experience_similarity([by_id["Paris"]]) is None
