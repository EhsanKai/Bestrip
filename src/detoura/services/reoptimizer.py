"""Editing a chosen trip instead of searching for a new one (V7 Phase 2).

The product requirement is narrow and easy to get wrong: when somebody removes
one city from a trip they picked, they want *their* trip without that city -
not the best trip in Europe. An engine that answers the second question when
asked the first is optimising correctly and ignoring the person.

Two mechanisms keep that from happening, and only one of them is new.

**Hard edits become constraints, not preferences.** A lock is
``TripRequest.must_visit`` and a removal is ``avoid_destinations``, both of
which the optimizer already enforces: a missing mandatory city fails
completion, and a forbidden city is rejected on *every* state, partial or
complete. So the unrelated-but-higher-scoring trip from the V7 brief is not
out-ranked, it is **infeasible**. That is a structural guarantee rather than a
tuned weight, and it needed no change to the optimizer.

**Soft preservation becomes selection.** Among candidates that all satisfy the
edit, similarity decides - same order, same hotel, same airport. That is what
:mod:`detoura.services.similarity` is for, and it is applied here and nowhere
else.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from ..data.destinations import canonical_key
from ..models.itinerary import Itinerary
from ..models.patch import (
    AddCity,
    ChangeBudget,
    ChangeTripDuration,
    ExcludeCity,
    LockCity,
    RemoveCity,
    ReplaceCity,
    TripPatch,
    UnlockCity,
)
from ..models.search import SearchState
from ..models.trip import TripRequest
from ..profiles import ProfileName
from ..providers.failures import FailureLog
from . import similarity as similarity_module
from .similarity import TripSimilarity
from .trip_comparison import (
    Favours,
    MetricComparison,
    build_metrics,
    values_of_itinerary,
)
from .trip_comparison import _phrase as _metric_phrase

#: How much of the ranking is "keep what I chose" rather than "find the best".
#:
#: Not tuned to a benchmark yet, and deliberately marked as such. Note what it
#: is *not* doing: locked cities are already hard constraints, so this never
#: decides whether the traveler's instruction is honoured. It only chooses
#: between candidates that all honour it - Hamburg before Berlin or after, this
#: hotel or that one - which is why its influence is bounded.
DEFAULT_SIMILARITY_WEIGHT = 0.35


class EditConflict(ValueError):
    """The edit contradicts itself or cannot be expressed as a request.

    Raised rather than resolved. When somebody locks and removes the same city
    in one breath, guessing which they meant is worse than saying so.
    """


def _fold(name: str) -> str:
    """Fold a city name the way the *constraint layer* folds it.

    Deliberately `canonical_key`, not `str.casefold`. The validator resolves
    names through the catalog's alias table, so "München" and "Munich" are one
    destination by the time a constraint is enforced. A patch layer that
    compared them with plain casefolding saw two different cities, so locking
    one and removing the other raised no conflict and produced a request that
    demanded and forbade the same place. The search then correctly found
    nothing, and the endpoint reported a self-contradictory edit as "fair
    question, empty answer" - the exact conflation its own contract forbids.
    """
    return canonical_key(name)


@dataclass(frozen=True, slots=True)
class DerivedEdit:
    """The edit, expressed as something the existing optimizer understands."""

    request: TripRequest
    locked: tuple[str, ...]
    excluded: tuple[str, ...]
    added: tuple[str, ...]
    unsupported: tuple[str, ...]
    """Operations the patch declared that this phase does not carry out.

    Reported rather than dropped: an instruction that is silently ignored is
    indistinguishable, to the traveler, from one that was obeyed and did
    nothing.
    """


def derive_request(
    request: TripRequest, itinerary: Itinerary, patch: TripPatch
) -> DerivedEdit:
    """Turn a patch into a derived :class:`TripRequest`.

    Operations apply in order, and for value-setting operations the last one
    wins: two ``change_budget`` operations leave the later figure standing.

    City operations are stricter. A patch that both keeps and removes the same
    city is **refused**, whichever order they arrive in. Order disambiguates a
    sequence of intentions; it does not disambiguate a contradiction, and
    picking the later of two opposite instructions would be guessing which one
    the traveler meant. Saying so is better.
    """
    must_visit = {_fold(c): c for c in request.must_visit}
    avoid = {_fold(c): c for c in request.avoid_destinations}
    budget = request.budget
    duration_days = request.duration_days
    city_count = request.preferred_city_count

    locked_here: set[str] = set()
    removed_here: set[str] = set()
    added: list[str] = []
    bare_replacements = 0
    original_cities = {_fold(city) for city in itinerary.cities}

    for operation in patch.operations:
        if isinstance(operation, LockCity):
            key = _fold(operation.city)
            locked_here.add(key)
            must_visit[key] = operation.city
            avoid.pop(key, None)
        elif isinstance(operation, UnlockCity):
            # Stop protecting it. Emphatically not a removal: unlocking means
            # "you may change this", and treating it as "take this out" would
            # delete cities the traveler merely stopped insisting on.
            must_visit.pop(_fold(operation.city), None)
        elif isinstance(operation, (RemoveCity, ExcludeCity)):
            key = _fold(operation.city)
            removed_here.add(key)
            avoid[key] = operation.city
            must_visit.pop(key, None)
        elif isinstance(operation, ReplaceCity):
            key = _fold(operation.city)
            removed_here.add(key)
            avoid[key] = operation.city
            must_visit.pop(key, None)
            if operation.replacement:
                replacement_key = _fold(operation.replacement)
                locked_here.add(replacement_key)
                must_visit[replacement_key] = operation.replacement
                avoid.pop(replacement_key, None)
                added.append(operation.replacement)
            else:
                bare_replacements += 1
        elif isinstance(operation, AddCity):
            key = _fold(operation.city)
            locked_here.add(key)
            must_visit[key] = operation.city
            avoid.pop(key, None)
            added.append(operation.city)
        elif isinstance(operation, ChangeBudget):
            budget = operation.budget
        elif isinstance(operation, ChangeTripDuration):
            duration_days = operation.duration_days

    if bare_replacements:
        # "Replace this city" must not become "remove it", so a target count is
        # pinned. It has to be the count the *whole patch* implies, though:
        # pinning the original total ignored any other operation that also
        # shrank the trip, and the optimizer then made up the difference with
        # cities nobody asked for. Removing one city and bare-replacing another
        # on a three-city trip kept one and invented two.
        removed_from_original = len(removed_here & original_cities)
        gained = len(
            {_fold(city) for city in added} - original_cities
        )
        city_count = max(
            1,
            len(itinerary.cities)
            - removed_from_original
            + bare_replacements
            + gained,
        )

    contradictory = sorted(locked_here & removed_here)
    if contradictory:
        raise EditConflict(
            "the same city is both kept and removed in one edit: "
            + ", ".join(must_visit.get(k) or avoid.get(k) or k for k in contradictory)
        )

    try:
        derived = request.model_copy(
            update={
                "must_visit": list(must_visit.values()),
                "avoid_destinations": list(avoid.values()),
                "budget": budget,
                "duration_days": duration_days,
                "preferred_city_count": city_count,
            }
        )
        # `model_copy` skips validation, so the derived request is re-validated
        # explicitly. Without this an edit could produce a request the search
        # would happily run and the domain model would never have accepted -
        # a duration that does not fit its own window, say.
        derived = TripRequest.model_validate(derived.model_dump())
    except ValueError as error:
        raise EditConflict(f"the edit does not describe a valid trip: {error}") from error

    return DerivedEdit(
        request=derived,
        locked=tuple(must_visit.values()),
        excluded=tuple(avoid.values()),
        added=tuple(added),
        unsupported=tuple(patch.unsupported),
    )


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    state: SearchState
    similarity: TripSimilarity
    travel_value: float
    score: float
    """Blended: Travel Value and preservation, in that proportion."""


def rank_candidates(
    completed: list[SearchState],
    original: Itinerary,
    *,
    similarity_weight: float = DEFAULT_SIMILARITY_WEIGHT,
) -> list[RankedCandidate]:
    """Rank valid candidates by Travel Value blended with preservation.

    Deliberately does **not** run the Pareto filter or the diversity filter
    that discovery uses.

    Pareto compares eight generic objectives and similarity is not one of them,
    so it can dominate away the very candidate an edit is looking for - the one
    that keeps the traveler's route and pays a little for it. Diversity is
    worse: after a lock every valid candidate shares the locked cities, so
    their overlap is high *by construction*, and a filter built to collapse
    near-duplicates would discard exactly the alternatives the edit is choosing
    between.

    What is kept is the exact-route de-duplication, which is safe because it is
    exact: two candidates differing only in departure time are one choice.
    """
    weight = min(max(similarity_weight, 0.0), 1.0)
    ranked: list[RankedCandidate] = []
    seen: set[tuple[str, ...]] = set()
    for state in completed:
        signature = (state.origin_airport, *state.cities, state.current_location)
        if signature in seen:
            continue
        seen.add(signature)
        measure = similarity_module.compare(original, state)
        ranked.append(
            RankedCandidate(
                state=state,
                similarity=measure,
                travel_value=state.score,
                score=round(
                    state.score * (1.0 - weight) + measure.total * weight, 9
                ),
            )
        )
    # Ties broken on the state's stable signature, so repeated re-optimization
    # of the same edit returns the same trip.
    ranked.sort(key=lambda c: (-c.score, c.state.total_cost, c.state.signature()))
    return ranked


class ChangeDiff(BaseModel):
    """What the traveler asked for, what changed, and what it cost.

    Every number here comes from :func:`build_metrics`, the same comparison
    path Phase 1 uses, so an edit and a your-idea-versus-ours comparison cannot
    disagree about what "3.8 hours less transit" means. Nothing in the prose is
    invented: it is assembled from the structured metrics, which is why it can
    be tested.
    """

    model_config = ConfigDict(frozen=True)

    requested: list[str] = Field(default_factory=list)
    """The edit, in words, echoed back so the answer names the question."""

    cities_kept: list[str] = Field(default_factory=list)
    cities_removed: list[str] = Field(default_factory=list)
    cities_added: list[str] = Field(default_factory=list)
    order_preserved: bool = True
    departure_airport_changed: bool = False
    return_airport_changed: bool = False
    previous_departure_airport: str = ""
    departure_airport: str = ""
    previous_return_airport: str = ""
    return_airport: str = ""

    metrics: list[MetricComparison] = Field(default_factory=list)
    price_delta: float = 0.0
    transit_delta_hours: float = 0.0
    usable_delta_hours: float = 0.0
    experience_delta: float = 0.0
    preference_delta: float = 0.0
    accommodation_delta: float = 0.0

    improvements: list[str] = Field(default_factory=list)
    costs: list[str] = Field(default_factory=list)
    """What the edit cost. Reported beside the improvements, never instead."""
    summary: str = ""


def _describe(patch: TripPatch) -> list[str]:
    """The patch in plain words, in the order it was given."""
    described: list[str] = []
    for operation in patch.operations:
        if isinstance(operation, LockCity):
            described.append(f"keep {operation.city}")
        elif isinstance(operation, UnlockCity):
            described.append(f"stop protecting {operation.city}")
        elif isinstance(operation, RemoveCity):
            described.append(f"remove {operation.city}")
        elif isinstance(operation, ExcludeCity):
            described.append(f"never offer {operation.city}")
        elif isinstance(operation, ReplaceCity):
            described.append(
                f"replace {operation.city} with {operation.replacement}"
                if operation.replacement
                else f"replace {operation.city}"
            )
        elif isinstance(operation, AddCity):
            described.append(f"add {operation.city}")
        elif isinstance(operation, ChangeBudget):
            described.append(f"budget {operation.budget:.0f}")
        elif isinstance(operation, ChangeTripDuration):
            described.append(f"{operation.duration_days} days")
        else:
            described.append(operation.op.replace("_", " "))
    return described


def build_diff(
    original: Itinerary, edited: Itinerary, patch: TripPatch
) -> ChangeDiff:
    """Compare the trip the traveler had against the one the edit produced."""
    metrics = build_metrics(
        values_of_itinerary(original), values_of_itinerary(edited)
    )
    by_name = {m.metric: m for m in metrics}

    before = [c for c in original.cities]
    after = [c for c in edited.cities]
    before_folded = {_fold(c) for c in before}
    after_folded = {_fold(c) for c in after}
    kept = [c for c in after if _fold(c) in before_folded]
    removed = [c for c in before if _fold(c) not in after_folded]
    added = [c for c in after if _fold(c) not in before_folded]

    # Order counts only over the cities both trips still contain; a city the
    # traveler asked to remove must not also be scored as a reordering.
    shared_before = [_fold(c) for c in before if _fold(c) in after_folded]
    shared_after = [_fold(c) for c in after if _fold(c) in before_folded]
    order_preserved = shared_before == shared_after

    improvements = [
        _metric_phrase(m, edited.currency)
        for m in metrics
        if m.favours is Favours.DETOURA
    ]
    costs = [
        _metric_phrase(m, edited.currency)
        for m in metrics
        if m.favours is Favours.ORIGINAL
    ]

    parts = [f"You asked to {_join_words(_describe(patch))}."]
    changes: list[str] = []
    if removed and added:
        changes.append(f"{_join_words(removed)} became {_join_words(added)}")
    elif removed:
        changes.append(f"dropped {_join_words(removed)}")
    elif added:
        changes.append(f"added {_join_words(added)}")
    if edited.origin_airport != original.origin_airport:
        changes.append(
            f"departure {original.origin_airport} to {edited.origin_airport}"
        )
    if edited.return_airport != original.return_airport:
        changes.append(f"return {original.return_airport} to {edited.return_airport}")
    parts.append(
        f"Detoura changed: {_join_words(changes)}."
        if changes
        else "Detoura found no change was needed."
    )
    if improvements:
        parts.append(f"Better: {_join_words(improvements)}.")
    if costs:
        # Stated in the same breath as the gains. An edit summary that reports
        # only what improved is a sales pitch for a change the traveler is
        # about to accept on trust.
        parts.append(f"Worse: {_join_words(costs)}.")
    if not improvements and not costs:
        parts.append("Nothing else moved by enough to matter.")

    return ChangeDiff(
        requested=_describe(patch),
        cities_kept=kept,
        cities_removed=removed,
        cities_added=added,
        order_preserved=order_preserved,
        departure_airport_changed=edited.origin_airport != original.origin_airport,
        return_airport_changed=edited.return_airport != original.return_airport,
        previous_departure_airport=original.origin_airport,
        departure_airport=edited.origin_airport,
        previous_return_airport=original.return_airport,
        return_airport=edited.return_airport,
        metrics=metrics,
        price_delta=by_name["price"].delta,
        transit_delta_hours=by_name["transit_hours"].delta,
        usable_delta_hours=by_name["usable_hours"].delta,
        experience_delta=by_name["experience"].delta,
        preference_delta=by_name["preference_match"].delta,
        accommodation_delta=by_name["accommodation"].delta,
        improvements=improvements,
        costs=costs,
        summary=" ".join(parts),
    )


def _join_words(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


@dataclass(frozen=True, slots=True)
class ReoptimizationResult:
    """The edited trip, what it preserved, and what the edit cost."""

    trip: Itinerary | None
    alternatives: tuple[Itinerary, ...]
    similarity: TripSimilarity | None
    diff: ChangeDiff | None
    derived: DerivedEdit
    considered: int
    warnings: tuple[str, ...] = ()


def reoptimize(
    planner,
    request: TripRequest,
    itinerary: Itinerary,
    patch: TripPatch,
    *,
    failures: FailureLog | None = None,
    similarity_weight: float = DEFAULT_SIMILARITY_WEIGHT,
    alternatives: int = 2,
    profile: ProfileName | str | None = None,
) -> ReoptimizationResult:
    """Re-optimize ``itinerary`` under ``patch``.

    Raises :class:`EditConflict` when the edit contradicts itself or cannot be
    expressed as a valid request. Returns a result with ``trip=None`` when the
    edit is coherent but nothing satisfies it - a different answer, and one the
    caller must be able to tell apart, because "you asked for something
    impossible" and "we could not find it" call for different next steps.
    """
    derived = derive_request(request, itinerary, patch)
    exploration = planner.explore(derived.request, profile=profile, failures=failures)
    ranked = rank_candidates(
        exploration.completed, itinerary, similarity_weight=similarity_weight
    )

    if not ranked:
        return ReoptimizationResult(
            trip=None,
            alternatives=(),
            similarity=None,
            diff=None,
            derived=derived,
            considered=0,
            warnings=tuple(exploration.warnings)
            + (
                "no itinerary satisfies this edit inside the budget, window "
                "and duration",
            ),
        )

    best = ranked[0]
    trip = planner.materialize(
        best.state, derived.request, 1, exploration.profile
    )
    others = tuple(
        planner.materialize(
            candidate.state, derived.request, rank, exploration.profile
        )
        for rank, candidate in enumerate(ranked[1 : 1 + alternatives], start=2)
    )
    return ReoptimizationResult(
        trip=trip,
        alternatives=others,
        similarity=best.similarity,
        diff=build_diff(itinerary, trip, patch),
        derived=derived,
        considered=len(ranked),
        warnings=tuple(exploration.warnings),
    )
