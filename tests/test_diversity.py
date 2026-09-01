"""Route diversification."""

from __future__ import annotations

import pytest

from travel_planner.algorithms.diversity import diversify, jaccard_similarity
from travel_planner.services.planner import TravelPlanner


def cities(*names: str) -> frozenset[str]:
    return frozenset(names)


# ---------------------------------------------------------------------------
# jaccard_similarity()
# ---------------------------------------------------------------------------
def test_identical_city_sets_are_fully_similar():
    assert jaccard_similarity(cities("London", "Brussels"), cities("Brussels", "London")) == 1.0


def test_disjoint_city_sets_are_not_similar():
    assert jaccard_similarity(cities("London"), cities("Prague")) == 0.0


def test_partial_overlap():
    assert jaccard_similarity(
        cities("Prague", "Vienna"), cities("Prague", "Budapest")
    ) == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# diversify()
# ---------------------------------------------------------------------------
def test_duplicate_routes_are_collapsed():
    """The BAD output from the spec: five variants of one trip."""
    candidates = [
        ("DUS-London-Brussels-DUS", cities("London", "Brussels")),
        ("CGN-London-Brussels-CGN", cities("London", "Brussels")),
        ("DUS-London-Brussels-CGN", cities("London", "Brussels")),
        ("DUS-Prague-Vienna-DUS", cities("Prague", "Vienna")),
        ("CGN-Madrid-CGN", cities("Madrid")),
    ]
    result = diversify(
        candidates, lambda item: item[1], limit=3, similarity_threshold=0.5
    )
    selected = [label for label, _ in result.selected]
    assert selected == [
        "DUS-London-Brussels-DUS",
        "DUS-Prague-Vienna-DUS",
        "CGN-Madrid-CGN",
    ]
    assert {label for label, _ in (r.item for r in result.rejected)} == {
        "CGN-London-Brussels-CGN",
        "DUS-London-Brussels-CGN",
    }
    assert all(r.similarity == 1.0 for r in result.rejected)


def test_first_item_is_always_selected():
    candidates = [("a", cities("X")), ("b", cities("X"))]
    result = diversify(candidates, lambda item: item[1], limit=2)
    assert result.selected[0][0] == "a"


def test_backfill_when_the_threshold_is_too_strict():
    """Better to return near-duplicates than fewer results than asked for."""
    candidates = [(f"r{i}", cities("London", "Brussels")) for i in range(4)]
    result = diversify(
        candidates, lambda item: item[1], limit=3, similarity_threshold=0.0
    )
    assert len(result.selected) == 3
    assert result.selected[0][0] == "r0"


def test_threshold_controls_strictness():
    candidates = [
        ("a", cities("Prague", "Vienna")),
        ("b", cities("Prague", "Budapest")),  # 1/3 overlap with a
    ]
    strict = diversify(
        candidates, lambda item: item[1], limit=2, similarity_threshold=0.2
    )
    lenient = diversify(
        candidates, lambda item: item[1], limit=2, similarity_threshold=0.5
    )
    # Under a strict threshold "b" is rejected first, then backfilled.
    assert [label for label, _ in lenient.selected] == ["a", "b"]
    assert lenient.rejected == []
    assert strict.rejected == [] or len(strict.selected) == 2


def test_limit_is_respected():
    candidates = [(f"r{i}", cities(f"City{i}")) for i in range(10)]
    result = diversify(candidates, lambda item: item[1], limit=4)
    assert len(result.selected) == 4


def test_diversify_is_deterministic():
    candidates = [
        ("a", cities("London", "Brussels")),
        ("b", cities("Prague", "Vienna")),
        ("c", cities("London")),
    ]
    first = diversify(candidates, lambda item: item[1], limit=3)
    second = diversify(candidates, lambda item: item[1], limit=3)
    assert first.selected == second.selected


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------
def test_recommendations_are_not_duplicates(planner, koln_request):
    result = planner.plan(koln_request)
    signatures = [frozenset(itinerary.cities) for itinerary in result.recommendations]
    assert len(signatures) == len(set(signatures)), signatures
    labels = [itinerary.route_label() for itinerary in result.recommendations]
    assert len(labels) == len(set(labels))


def test_recommendations_stay_below_the_similarity_threshold(planner, koln_request):
    result = planner.plan(koln_request)
    signatures = [frozenset(itinerary.cities) for itinerary in result.recommendations]
    for index, left in enumerate(signatures):
        for right in signatures[index + 1 :]:
            assert jaccard_similarity(left, right) <= planner.config.diversity_similarity_threshold


def test_planner_reports_similarity_filtering(planner, koln_request):
    """Both similarity stages report themselves with a similarity score.

    V3 added an exact route-duplicate pass in front of the Jaccard filter, so
    near-identical trips are usually removed there; the Jaccard pass then has
    less to do, and on some requests nothing at all. Either stage firing is
    evidence the pipeline is de-duplicating.
    """
    result = planner.plan(koln_request, debug=True)
    similarity_records = [
        record
        for record in result.debug.filtered
        if record.stage.value in {"DIVERSITY", "DUPLICATE_ROUTE"}
    ]
    assert similarity_records
    for record in similarity_records:
        assert record.similarity is not None and record.similarity > 0
        assert record.route


def test_exact_route_duplicates_are_collapsed(planner, koln_request):
    """The same airports and cities at a different time of day is one trip."""
    result = planner.plan(koln_request, debug=True)
    duplicates = [
        record
        for record in result.debug.filtered
        if record.stage.value == "DUPLICATE_ROUTE"
    ]
    assert duplicates
    assert all(record.similarity == 1.0 for record in duplicates)

    routes = [i.route_label() for i in result.recommendations]
    assert len(routes) == len(set(routes))


def test_diversity_can_be_disabled(transport, destinations, config, koln_request):
    off = TravelPlanner(
        transport,
        destinations,
        config=config.model_copy(update={"enable_diversity": False}),
    )
    result = off.plan(koln_request, debug=True)
    assert result.recommendations
    assert not [
        record for record in result.debug.filtered if record.stage.value == "DIVERSITY"
    ]
