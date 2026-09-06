"""V7 Phase 2: editing a chosen trip.

The product requirement these tests exist to defend is one sentence long:
re-optimization must preserve what the traveler explicitly asked to keep. Most
of what follows tries to make it fail to.

The important structural claim is that locks and exclusions are *hard*, not
preferred. So the headline adversarial case - an unrelated, globally
higher-scoring trip must not win an edit - is asserted here as infeasibility
rather than as a ranking outcome. A ranking can be retuned by accident; a
constraint cannot.
"""

from __future__ import annotations

from datetime import date

import pytest

from detoura.models.patch import TripPatch
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.services.planner import TravelPlanner
from detoura.services.reoptimizer import (
    EditConflict,
    derive_request,
    rank_candidates,
    reoptimize,
)

WINDOW_FROM = date(2026, 9, 10)
WINDOW_TO = date(2026, 9, 24)


def a_request(**kw) -> TripRequest:
    fields = dict(
        origin="Köln", budget=1200.0, travelers=2, duration_days=7,
        date_from=WINDOW_FROM, date_to=WINDOW_TO,
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
    )
    fields.update(kw)
    return TripRequest(**fields)


def patch_of(*operations) -> TripPatch:
    return TripPatch.model_validate({"operations": list(operations)})


def lock(city):    return {"op": "lock_city", "city": city}
def unlock(city):  return {"op": "unlock_city", "city": city}
def remove(city):  return {"op": "remove_city", "city": city}
def exclude(city): return {"op": "exclude_city", "city": city}
def add(city):     return {"op": "add_city", "city": city}


@pytest.fixture(scope="module")
def planned():
    """One real search, reused: the planner caches providers across edits."""
    planner = TravelPlanner()
    request = a_request()
    result = planner.plan(request)
    assert result.recommendations, "fixture needs a real multi-city trip"
    original = next(
        (r for r in result.recommendations if len(r.cities) >= 3),
        result.recommendations[0],
    )
    return planner, request, original


# ---------------------------------------------------------------------------
# Locks (§23)
# ---------------------------------------------------------------------------
def test_lock_one_city(planned):
    planner, request, original = planned
    kept = original.cities[0]
    out = reoptimize(planner, request, original, patch_of(lock(kept)))
    assert out.trip is not None
    assert kept in out.trip.cities


def test_lock_multiple_cities(planned):
    planner, request, original = planned
    kept = list(original.cities[:2])
    out = reoptimize(planner, request, original, patch_of(*[lock(c) for c in kept]))
    assert out.trip is not None
    for city in kept:
        assert city in out.trip.cities


def test_every_ranked_candidate_honours_the_locks(planned):
    """Not just the winner. A lock that only the top result respects is luck."""
    planner, request, original = planned
    kept = list(original.cities[:2])
    patch = patch_of(*[lock(c) for c in kept])
    derived = derive_request(request, original, patch)
    exploration = planner.explore(derived.request)
    assert exploration.completed
    for state in exploration.completed:
        for city in kept:
            assert city in state.cities


# ---------------------------------------------------------------------------
# Removal and exclusion (§23)
# ---------------------------------------------------------------------------
def test_remove_final_city(planned):
    planner, request, original = planned
    dropped = original.cities[-1]
    out = reoptimize(planner, request, original, patch_of(remove(dropped)))
    assert out.trip is not None
    assert dropped not in out.trip.cities


def test_remove_middle_city(planned):
    planner, request, original = planned
    if len(original.cities) < 3:
        pytest.skip("needs a three-city original")
    dropped = original.cities[1]
    out = reoptimize(planner, request, original, patch_of(remove(dropped)))
    assert out.trip is not None
    assert dropped not in out.trip.cities


def test_excluded_city_never_reappears_at_any_depth(planned):
    """Enforced on every state, not filtered at the end.

    `_validate_common` rejects a forbidden city the moment it enters
    `visited_cities`, so this holds for partial states too - a deep branch
    cannot smuggle one through and be caught only at completion.
    """
    planner, request, original = planned
    banned = original.cities[0]
    derived = derive_request(request, original, patch_of(exclude(banned)))
    exploration = planner.explore(derived.request)
    assert exploration.completed
    for state in exploration.completed:
        assert banned not in state.cities
        assert banned not in state.visited_cities


def test_saying_both_things_about_one_city_is_refused_in_either_order(planned):
    """Order disambiguates a sequence; it does not disambiguate a contradiction.

    "Remove Berlin, then add Berlin" could mean either thing. Taking the later
    instruction would be a coin toss dressed as a rule, so both orders are
    refused and the traveler is told.
    """
    _, request, original = planned
    city = original.cities[0]
    for operations in ((remove(city), add(city)), (add(city), remove(city))):
        with pytest.raises(EditConflict, match="both kept and removed"):
            derive_request(request, original, patch_of(*operations))


def test_later_value_operations_win(planned):
    """Ordering does resolve non-contradictory repeats."""
    _, request, original = planned
    derived = derive_request(
        request, original,
        patch_of({"op": "change_budget", "budget": 900.0},
                 {"op": "change_budget", "budget": 1100.0}),
    )
    assert derived.request.budget == 1100.0


# ---------------------------------------------------------------------------
# Unlock is not removal
# ---------------------------------------------------------------------------
def test_unlock_does_not_exclude(planned):
    _, request, original = planned
    city = original.cities[0]
    derived = derive_request(request, original, patch_of(lock(city), unlock(city)))
    assert city not in derived.locked
    assert city not in derived.excluded


# ---------------------------------------------------------------------------
# The headline case (§22): preservation is a constraint, not a preference
# ---------------------------------------------------------------------------
def test_an_unrelated_better_scoring_trip_cannot_win_an_edit(planned):
    """The V7 brief's candidate B is infeasible, not merely out-ranked.

    With Prague and Vienna locked, a trip through Brussels and Paris is
    rejected by the validator however good its Travel Value is. Asserted here
    against the whole candidate set, and separately against a similarity
    weight of zero - if preservation only held because of a tuned weight, this
    is where that would show.
    """
    planner, request, original = planned
    kept = list(original.cities[:2])
    patch = patch_of(*[lock(c) for c in kept])
    derived = derive_request(request, original, patch)
    exploration = planner.explore(derived.request)

    # Pure Travel Value, no preservation term at all.
    ranked = rank_candidates(exploration.completed, original, similarity_weight=0.0)
    assert ranked
    for candidate in ranked:
        for city in kept:
            assert city in candidate.state.cities


def test_similarity_prefers_the_closer_of_two_valid_edits(planned):
    """Where both candidates are legal, preservation decides."""
    planner, request, original = planned
    kept = list(original.cities[:2])
    patch = patch_of(*[lock(c) for c in kept], remove(original.cities[-1]))
    generic = reoptimize(planner, request, original, patch, similarity_weight=0.0)
    preserving = reoptimize(planner, request, original, patch, similarity_weight=0.9)
    assert generic.trip is not None and preserving.trip is not None
    assert preserving.similarity.total >= generic.similarity.total


# ---------------------------------------------------------------------------
# Impossible and contradictory edits
# ---------------------------------------------------------------------------
def test_locking_and_removing_the_same_city_is_refused(planned):
    """Guessing which instruction was meant is worse than saying so."""
    _, request, original = planned
    city = original.cities[0]
    with pytest.raises(EditConflict, match="both kept and removed"):
        derive_request(request, original, patch_of(lock(city), remove(city)))


def test_an_impossible_lock_combination_reports_rather_than_drops(planned):
    """No trip can hold six locked cities under max_cities=4.

    The contract is that it says so. Silently returning a trip missing two of
    them would be the single worst failure this feature could have.
    """
    planner, request, original = planned
    many = ["Berlin", "Prague", "Vienna", "Rome", "Madrid", "Dublin"]
    out = reoptimize(planner, request, original, patch_of(*[lock(c) for c in many]))
    assert out.trip is None
    assert out.considered == 0
    assert any("no itinerary satisfies" in w for w in out.warnings)


def test_a_locked_city_that_cannot_be_reached_fails_loudly(planned):
    """Not a quiet substitution."""
    planner, request, original = planned
    out = reoptimize(
        planner, request, original,
        patch_of(lock("Berlin"), {"op": "change_budget", "budget": 60.0}),
    )
    assert out.trip is None
    assert out.warnings


def test_an_edit_that_is_not_a_valid_trip_is_refused(planned):
    _, request, original = planned
    with pytest.raises(EditConflict, match="does not describe a valid trip"):
        derive_request(
            request, original,
            patch_of({"op": "change_trip_duration", "duration_days": 30}),
        )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_repeated_reoptimization_is_identical(planned):
    planner, request, original = planned
    patch = patch_of(lock(original.cities[0]), remove(original.cities[-1]))
    first = reoptimize(planner, request, original, patch)
    second = reoptimize(planner, request, original, patch)
    assert first.trip.route_label() == second.trip.route_label()
    assert first.trip.total_cost == second.trip.total_cost
    assert first.similarity.as_dict() == second.similarity.as_dict()
    assert first.diff.model_dump() == second.diff.model_dump()


# ---------------------------------------------------------------------------
# The structured diff (§24)
# ---------------------------------------------------------------------------
def test_diff_names_the_request_the_change_and_both_directions(planned):
    planner, request, original = planned
    dropped = original.cities[-1]
    out = reoptimize(planner, request, original, patch_of(remove(dropped)))
    diff = out.diff
    assert f"remove {dropped}" in diff.requested
    assert dropped in diff.cities_removed
    assert f"remove {dropped}" in diff.summary
    # Improvements and costs are separate lists so a UI cannot show one alone
    # by accident; every material metric lands in exactly one of them.
    material = [m for m in diff.metrics if m.material]
    assert len(material) == len(diff.improvements) + len(diff.costs)


def test_diff_costs_are_never_hidden(planned):
    """If the edit made something worse, the summary says so."""
    planner, request, original = planned
    out = reoptimize(planner, request, original, patch_of(remove(original.cities[-1])))
    if out.diff.costs:
        assert "Worse:" in out.diff.summary


def test_order_preservation_is_measured_over_surviving_cities(planned):
    """A city removed on purpose must not read as a reordering."""
    planner, request, original = planned
    out = reoptimize(planner, request, original, patch_of(remove(original.cities[-1])))
    assert out.diff.order_preserved is True


# ---------------------------------------------------------------------------
# Unsupported operations are reported, not swallowed
# ---------------------------------------------------------------------------
def test_unsupported_operations_are_named(planned):
    _, request, original = planned
    derived = derive_request(
        request, original,
        patch_of(lock(original.cities[0]),
                 {"op": "change_baggage_requirement", "baggage": "cabin_bag"}),
    )
    assert "change_baggage_requirement" in derived.unsupported


# ---------------------------------------------------------------------------
# Phase 2 must not disturb discovery
# ---------------------------------------------------------------------------
def test_similarity_is_absent_from_normal_search():
    """A fresh search must not prefer trips resembling some earlier trip.

    Checked as an import edge rather than a word search: `profiles.py` has
    carried `diversity_similarity_threshold` since V2, and grepping for the
    word would fail on a name that has nothing to do with trip editing.
    """
    import inspect

    from detoura import profiles
    from detoura.algorithms import travel_value

    for module in (travel_value, profiles):
        source = inspect.getsource(module)
        assert "services.similarity" not in source
        assert "TripSimilarity" not in source
        assert "reoptimizer" not in source


# ---------------------------------------------------------------------------
# Through the product API
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from detoura.api.app import create_app

    return TestClient(create_app())


SEARCH = {
    "origin": "Köln", "date_from": "2026-09-10", "date_to": "2026-09-24",
    "duration_days": 7, "travelers": 2, "budget": 1200.0,
    "interests": ["history", "food"], "search_mode": "SMART",
}


def _selected(trip: dict) -> dict:
    """The wire form of a chosen trip, built only from what a client is given.

    Deliberately assembled from the search response's own fields: if this
    cannot be written without inventing data, the round trip is broken and the
    test should say so rather than reaching into the engine.
    """
    return {
        "cities": trip["cities"],
        "origin_airport": trip["origin_airport"],
        "return_airport": trip["return_airport"],
        "departure": trip["departure"], "arrival": trip["arrival"],
        "duration_days": trip["duration_days"], "total_price": trip["total_price"],
        "intercity_minutes": trip["intercity_minutes"],
        "transfer_minutes": trip["transfer_minutes"],
        "usable_minutes": trip["usable_minutes"],
        "experience_score": trip["experience_score"],
        "preference_match": trip["preference_match"],
        "accommodation_score": trip["accommodation_score"],
        "legs": [
            {"from": leg["from"], "to": leg["to"], "departure": leg["departure"],
             "operator": leg["operator"], "price_per_person": leg["price_per_person"]}
            for leg in trip["legs"]
        ],
        "stays": [
            {"city": s["city"], "arrival": s["arrival"], "departure": s["departure"],
             "cost": s["cost"], "name": s["name"]} for s in trip["stays"]
        ],
    }


@pytest.fixture
def searched(client):
    response = client.post("/api/v1/search", json=SEARCH)
    assert response.status_code == 200
    trips = response.json()["recommendations"]
    trip = next((t for t in trips if len(t["cities"]) >= 3), trips[0])
    return client, trip


def test_exact_minutes_survive_the_round_trip(searched):
    """`travel_hours` is rounded; an edit must not invent a transit change.

    And the two must be distinguishable: `travel_hours` is door-to-door while
    `intercity_minutes` is legs only, so a field named "travel_minutes" that
    quietly meant the second would rebuild the exact asymmetry Phase 1 had to
    fix - a trip reported as half an hour different from itself.
    """
    _, trip = searched
    assert (
        trip["intercity_minutes"] + trip["transfer_minutes"]
        == trip["total_transit_minutes"]
    )
    assert trip["total_transit_minutes"] == pytest.approx(
        trip["travel_hours"] * 60, abs=3
    )
    for field in ("intercity_minutes", "total_transit_minutes", "usable_minutes"):
        assert isinstance(trip[field], int)


def test_api_edit_keeps_locked_cities_and_drops_the_removed_one(searched):
    client, trip = searched
    keep, drop = trip["cities"][:2], trip["cities"][-1]
    body = {
        "trip_id": trip["id"], "search": SEARCH, "trip": _selected(trip),
        "patch": {"operations": [{"op": "lock_city", "city": c} for c in keep]
                  + [{"op": "remove_city", "city": drop}]},
    }
    response = client.post("/api/v1/trips/reoptimize", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["trip"] is not None
    for city in keep:
        assert city in payload["trip"]["cities"]
    assert drop not in payload["trip"]["cities"]
    assert payload["locked"] == keep
    assert payload["excluded"] == [drop]
    assert payload["similarity"]["total"] > 0
    assert payload["diff"]["summary"]


def test_api_contradictory_edit_is_422(searched):
    client, trip = searched
    city = trip["cities"][0]
    body = {
        "trip_id": trip["id"], "search": SEARCH, "trip": _selected(trip),
        "patch": {"operations": [{"op": "lock_city", "city": city},
                                 {"op": "remove_city", "city": city}]},
    }
    response = client.post("/api/v1/trips/reoptimize", json=body)
    assert response.status_code == 422
    assert "both kept and removed" in response.json()["detail"]


def test_api_impossible_edit_is_200_with_no_trip(searched):
    """Distinguishable from a 422: the question was fair, the answer is empty."""
    client, trip = searched
    body = {
        "trip_id": trip["id"], "search": SEARCH, "trip": _selected(trip),
        "patch": {"operations": [{"op": "lock_city", "city": c} for c in
                                 ["Berlin", "Prague", "Vienna", "Rome",
                                  "Madrid", "Dublin"]]},
    }
    response = client.post("/api/v1/trips/reoptimize", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["trip"] is None
    assert payload["warnings"]


def test_api_unknown_operation_is_rejected(searched):
    client, trip = searched
    body = {
        "trip_id": trip["id"], "search": SEARCH, "trip": _selected(trip),
        "patch": {"operations": [{"op": "teleport", "city": "Berlin"}]},
    }
    assert client.post("/api/v1/trips/reoptimize", json=body).status_code == 422


def test_api_unsupported_operation_is_named_not_swallowed(searched):
    client, trip = searched
    body = {
        "trip_id": trip["id"], "search": SEARCH, "trip": _selected(trip),
        "patch": {"operations": [{"op": "lock_city", "city": trip["cities"][0]},
                                 {"op": "change_baggage_requirement",
                                  "baggage": "cabin_bag"}]},
    }
    payload = client.post("/api/v1/trips/reoptimize", json=body).json()
    assert "change_baggage_requirement" in payload["unsupported_operations"]


# ---------------------------------------------------------------------------
# Compound patches (§4 of the Phase 2 gate)
#
# Both real Phase 2 defects were compound-only: each operation behaved
# correctly alone and wrongly in company. Operations are therefore exercised in
# combination here, not just in isolation.
# ---------------------------------------------------------------------------
def _invariants(out, *, locked=(), excluded=(), label=""):
    """The properties every edit must satisfy, whatever the patch said."""
    if out.trip is None:
        # An infeasible edit is a legitimate outcome; it must be reported.
        assert out.warnings, f"{label}: no trip and no explanation"
        return
    cities = out.trip.cities
    assert len(cities) == len(set(cities)), f"{label}: duplicate city in {cities}"
    for city in locked:
        assert city in cities, f"{label}: lost locked {city} from {cities}"
    for city in excluded:
        assert city not in cities, f"{label}: excluded {city} reappeared in {cities}"
    assert out.trip.legs, f"{label}: itinerary has no legs"
    assert out.trip.origin_airport and out.trip.return_airport
    assert out.trip.total_cost > 0


def test_compound_lock_plus_remove(planned):
    planner, request, original = planned
    keep, drop = original.cities[0], original.cities[-1]
    out = reoptimize(planner, request, original, patch_of(lock(keep), remove(drop)))
    _invariants(out, locked=[keep], excluded=[drop], label="lock+remove")


def test_compound_remove_plus_bare_replace_does_not_invent_cities(planned):
    """The second confirmed defect, kept as a regression.

    Removing one city and bare-replacing another must target
    ``original - removed + replacements``, not the original total. Pinning the
    original count made the optimizer top the trip back up with cities nobody
    asked for.
    """
    planner, request, original = planned
    if len(original.cities) < 3:
        pytest.skip("needs a three-city original")
    gone, swapped = original.cities[0], original.cities[1]
    patch = patch_of(remove(gone), {"op": "replace_city", "city": swapped})
    derived = derive_request(request, original, patch)
    assert derived.request.preferred_city_count == len(original.cities) - 1
    out = reoptimize(planner, request, original, patch)
    _invariants(out, excluded=[gone, swapped], label="remove+replace")
    if out.trip is not None:
        assert len(out.trip.cities) <= len(original.cities) - 1


def test_compound_lock_plus_named_replace(planned):
    planner, request, original = planned
    keep, swapped = original.cities[0], original.cities[-1]
    out = reoptimize(
        planner, request, original,
        patch_of(lock(keep), {"op": "replace_city", "city": swapped,
                              "replacement": "Prague"}),
    )
    _invariants(out, locked=[keep], excluded=[swapped], label="lock+replace")
    if out.trip is not None:
        assert "Prague" in out.trip.cities


def test_compound_remove_plus_add(planned):
    planner, request, original = planned
    gone = original.cities[-1]
    out = reoptimize(planner, request, original, patch_of(remove(gone), add("Prague")))
    _invariants(out, locked=["Prague"], excluded=[gone], label="remove+add")


def test_compound_multiple_removes(planned):
    planner, request, original = planned
    if len(original.cities) < 3:
        pytest.skip("needs a three-city original")
    gone = list(original.cities[:2])
    out = reoptimize(planner, request, original, patch_of(*[remove(c) for c in gone]))
    _invariants(out, excluded=gone, label="multi-remove")


def test_compound_multiple_locks(planned):
    planner, request, original = planned
    keep = list(original.cities[:2])
    out = reoptimize(planner, request, original, patch_of(*[lock(c) for c in keep]))
    _invariants(out, locked=keep, label="multi-lock")


def test_compound_remove_plus_duration_change(planned):
    planner, request, original = planned
    gone = original.cities[-1]
    out = reoptimize(
        planner, request, original,
        patch_of(remove(gone), {"op": "change_trip_duration", "duration_days": 5}),
    )
    _invariants(out, excluded=[gone], label="remove+duration")


def test_compound_lock_plus_budget_reduction(planned):
    """A budget cut may make the edit impossible - but never silently drop a lock."""
    planner, request, original = planned
    keep = original.cities[0]
    out = reoptimize(
        planner, request, original,
        patch_of(lock(keep), {"op": "change_budget", "budget": 700.0}),
    )
    _invariants(out, locked=[keep], label="lock+budget")


def test_compound_remove_replace_and_budget_change(planned):
    planner, request, original = planned
    if len(original.cities) < 3:
        pytest.skip("needs a three-city original")
    gone, swapped = original.cities[0], original.cities[1]
    out = reoptimize(
        planner, request, original,
        patch_of(remove(gone), {"op": "replace_city", "city": swapped},
                 {"op": "change_budget", "budget": 1400.0}),
    )
    _invariants(out, excluded=[gone, swapped], label="remove+replace+budget")


def test_compound_replace_plus_duration_change(planned):
    planner, request, original = planned
    swapped = original.cities[-1]
    out = reoptimize(
        planner, request, original,
        patch_of({"op": "replace_city", "city": swapped},
                 {"op": "change_trip_duration", "duration_days": 6}),
    )
    _invariants(out, excluded=[swapped], label="replace+duration")


def test_compound_patches_are_deterministic(planned):
    planner, request, original = planned
    patch = patch_of(lock(original.cities[0]), remove(original.cities[-1]),
                     {"op": "change_budget", "budget": 1100.0})
    first = reoptimize(planner, request, original, patch)
    second = reoptimize(planner, request, original, patch)
    assert (first.trip is None) == (second.trip is None)
    if first.trip is not None:
        assert first.trip.route_label() == second.trip.route_label()
        assert first.diff.model_dump() == second.diff.model_dump()


# ---------------------------------------------------------------------------
# Replacement semantics (§5)
# ---------------------------------------------------------------------------
def test_named_replacement_removes_a_and_adds_b(planned):
    """`replace_city(A, B)` must not mean "add B and hope A disappears"."""
    planner, request, original = planned
    target = original.cities[-1]
    survivors = [c for c in original.cities if c != target]
    out = reoptimize(
        planner, request, original,
        patch_of({"op": "replace_city", "city": target, "replacement": "Prague"}),
    )
    assert out.trip is not None
    assert target not in out.trip.cities
    assert "Prague" in out.trip.cities
    assert len(out.trip.cities) <= len(original.cities)
    # Unrelated cities are mutable unless locked - the architecture preserves
    # them by preference, not by guarantee, and this asserts the honest claim.
    assert any(city in out.trip.cities for city in survivors)


def test_replacement_target_resolves_through_catalog_aliases(planned):
    """"München" and "Munich" are one destination to the constraint layer."""
    _, request, original = planned
    derived = derive_request(
        request, original,
        patch_of({"op": "replace_city", "city": original.cities[0],
                  "replacement": "München"}),
    )
    from detoura.data.destinations import canonical_key

    assert any(canonical_key(c) == "munich" for c in derived.locked)


def test_bare_replacement_holds_the_city_count(planned):
    _, request, original = planned
    derived = derive_request(
        request, original,
        patch_of({"op": "replace_city", "city": original.cities[-1]}),
    )
    assert derived.request.preferred_city_count == len(original.cities)


# ---------------------------------------------------------------------------
# The metric invariant (§9), asserted permanently
# ---------------------------------------------------------------------------
def test_transit_minutes_invariant_holds_on_every_edit(planned):
    """intercity + ground_transfer == total_transit, on the engine model.

    Named explicitly because ambiguous travel/transit naming has now caused two
    separate defects: once comparing a gate-to-gate baseline against a
    door-to-door itinerary, and once round-tripping a trip through the API as
    39 minutes different from itself.
    """
    planner, request, original = planned
    out = reoptimize(planner, request, original, patch_of(lock(original.cities[0])))
    assert out.trip is not None
    trip = out.trip
    assert (
        trip.total_travel_minutes + trip.ground_transfer_minutes
        == trip.total_transport_minutes
    )
