"""Getting real offers without asking a real API ten thousand times (V7.5).

Measured on V7's own HEAD, one search issues this many upstream route lookups:

    QUICK                1,566
    SMART                2,177
    DEEP                 2,927
    SMART, flexible 30d  7,515

At roughly a second per Offer Request that is thirty-six minutes for one SMART
search and over two hours for a flexible one - at sixteen destinations, with
fifty on the roadmap. Wiring a real provider into beam expansion is therefore
not slow, it is impossible.

The fix is an inversion. Today the optimizer *pulls* fares from a provider
while it explores. Here, a bounded acquisition pass runs **before** the search
and hands the optimizer a snapshot to explore instead:

    TripRequest -> origin resolution -> candidate discovery -> acquisition plan
        -> bounded provider calls -> OfferSnapshot -> beam search

Beam search then makes **zero** network calls, which is the architectural
invariant this module exists to enforce. `SnapshotTransportProvider` has no
client, no host and no token; it physically cannot reach the network.

Two failure modes are treated as first-class rather than as edge cases.
Truncated coverage is *reported*, because a bounded search that found nothing
must never be phrased as "there are no trips". And exhaustion is *typed*, for
the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Sequence

from ..models.destination import Destination
from ..models.transport import TransportOption
from ..models.trip import TripRequest
from ..providers.failures import ProviderFailureKind

#: Slots reserved for destinations that did *not* score well on preference.
#:
#: Detoura's whole claim is the trip you would not have searched for. A pool
#: filled purely by affinity returns the obvious eight cities and quietly
#: deletes the product's reason to exist, so a quarter of the pool is held for
#: cities the ranking did not favour.
DEFAULT_EXPLORATION_SHARE = 0.25


@dataclass(frozen=True, slots=True)
class ProviderCallBudget:
    """Hard ceilings on how much external traffic one search may cause.

    Every dimension is capped, because the explosion is multiplicative: cities
    times airports times dates times both directions. Capping only one of them
    leaves the product of the others free to grow.
    """

    max_offer_requests: int = 100
    max_destinations: int = 8
    max_date_variants: int = 1
    max_airport_variants: int = 2
    exploration_share: float = DEFAULT_EXPLORATION_SHARE
    include_inter_city: bool = True
    """Fetch destination-to-destination edges, which is what makes a multi-city
    itinerary possible at all. Off, the optimizer can only build out-and-back
    trips - so this is the expensive setting that Detoura actually needs."""


@dataclass(frozen=True, slots=True)
class AcquisitionEdge:
    """One provider question: this route, this day, this many people.

    ``travelers`` is part of the identity, not decoration. Airlines price per
    party and per remaining seat, so a quote fetched for one adult is not the
    quote for four - and a cache keyed only on (origin, destination, day) will
    hand a family of four the solo traveller's fare. Putting the party size in
    the key makes that mistake unrepresentable rather than merely discouraged.
    """

    origin: str
    destination: str
    day: date
    travelers: int = 1


@dataclass(frozen=True, slots=True)
class ProviderAcquisitionPlan:
    """What we intend to ask, decided *before* anything is asked.

    Planning first is the point. A plan can be counted, compared against the
    budget and shrunk while it is still free; a loop that discovers it has
    overspent has already spent it.
    """

    edges: tuple[AcquisitionEdge, ...]
    destinations: tuple[str, ...]
    airports: tuple[str, ...]
    days: tuple[date, ...]
    budget: ProviderCallBudget
    travelers: int = 1
    dropped_edges: int = 0
    """Edges the budget refused. Non-zero means coverage is incomplete and the
    result must say so rather than implying the search was exhaustive."""
    dropped_destinations: tuple[str, ...] = ()

    @property
    def planned_request_count(self) -> int:
        return len(self.edges)

    @property
    def is_truncated(self) -> bool:
        return self.dropped_edges > 0 or bool(self.dropped_destinations)


def rank_candidates(
    destinations: Sequence[Destination],
    request: TripRequest,
    *,
    limit: int,
    exploration_share: float = DEFAULT_EXPLORATION_SHARE,
) -> tuple[list[Destination], list[Destination]]:
    """Choose which cities are worth paying a provider to price.

    Returns ``(chosen, dropped)``.

    Explicitly **not** ``catalog[:limit]``. Catalog order is an artefact of the
    order somebody typed the file in; truncating by it silently deletes every
    city near the end and makes the result depend on data-entry sequence. The
    same objection applies to alphabetical order, to cheapest-first-leg (which
    cannot see a city that is expensive to enter and cheap to leave) and to
    nearest-first (which is just a smaller map).

    Cities are ranked on preference affinity, richness and novelty - and then a
    reserved share of the pool is filled from the cities that ranking *rejected*,
    interleaved deterministically so the exploration slots are spread across the
    remainder rather than taken from its top. Without that reservation, a
    fifty-city catalog collapses to "the obvious eight".
    """
    if limit <= 0:
        return [], list(destinations)

    weights = request.preferences.experience_weights()
    visited = {name.casefold() for name in request.previously_visited}
    disliked = set(request.disliked_experiences)
    preferred = {name.casefold() for name in request.preferred_destinations}
    must = {name.casefold() for name in request.must_visit}
    avoid = {name.casefold() for name in request.avoid_destinations}

    def affinity(destination: Destination) -> float:
        profile = destination.experience_vector()
        if weights:
            total = sum(weights.values())
            score = sum(profile[name] * weight for name, weight in weights.items())
            score = score / total if total else 0.0
        else:
            score = destination.richness
        # A city the traveller has already seen is worth less, not forbidden.
        if destination.id.casefold() in visited:
            score *= 0.6
        if any(profile.get(name, 0.0) > 0.6 for name in disliked):
            score *= 0.5
        return score

    eligible = [d for d in destinations if d.id.casefold() not in avoid]
    # Named destinations are not candidates to be ranked - they are the answer
    # to a question the traveller already settled.
    pinned = [d for d in eligible if d.id.casefold() in must or d.id.casefold() in preferred]
    rest = [d for d in eligible if d not in pinned]

    # Ties broken on id so the pool is identical across runs; a search whose
    # provider calls differ run to run cannot be benchmarked or reproduced.
    ranked = sorted(rest, key=lambda d: (-affinity(d), d.id))

    chosen: list[Destination] = list(pinned[:limit])
    remaining = limit - len(chosen)
    if remaining > 0:
        explore_slots = min(
            int(round(remaining * exploration_share)), max(len(ranked) - remaining, 0)
        )
        affinity_slots = remaining - explore_slots
        chosen.extend(ranked[:affinity_slots])
        if explore_slots > 0:
            tail = ranked[affinity_slots:]
            if tail:
                # Spread evenly *inside* the tail, never taking its first
                # entry. An earlier cut stepped from tail[0], which spends an
                # exploration slot on the city affinity would have picked next
                # anyway - measured, it chose rank 7 and then jumped to rank
                # 12, so of two "exploration" slots only one explored anything.
                # Sampling at even fractions reaches genuinely different cities
                # and leaves the near-ties to the affinity slots that want them.
                picked = []
                for index in range(1, explore_slots + 1):
                    position = min(
                        int(len(tail) * index / (explore_slots + 1)), len(tail) - 1
                    )
                    candidate = tail[position]
                    # Deterministic nudge on collision, so the pool stays
                    # reproducible run to run.
                    while candidate in picked and position + 1 < len(tail):
                        position += 1
                        candidate = tail[position]
                    if candidate not in picked:
                        picked.append(candidate)
                chosen.extend(picked)

    chosen_ids = {d.id for d in chosen}
    dropped = [d for d in destinations if d.id not in chosen_ids]
    return chosen, dropped


def days_for_request(
    request: TripRequest, start_dates: Sequence[date], *, max_days: int
) -> tuple[date, ...]:
    """Every day a leg of this trip could depart on.

    Acquiring only the start dates is the mistake this function exists to stop.
    A five-day trip through three cities has legs leaving on days two, three
    and four as well; a snapshot built from start dates alone contains the
    outbound flights and nothing to continue with, and the optimizer correctly
    concludes there are no trips - from a gap in the data, not in the market.

    Capped by ``max_days``, taking the earliest span, because the budget has to
    bind somewhere and the earliest departures are the ones the traveller asked
    about first.
    """
    if not start_dates:
        return ()
    span = max(request.duration_days, 1)
    days: set[date] = set()
    for start in start_dates:
        for offset in range(span):
            days.add(start + timedelta(days=offset))
    ordered = sorted(d for d in days if request.date_from <= d <= request.date_to)
    return tuple(ordered[:max_days])


def build_plan(
    request: TripRequest,
    *,
    destinations: Sequence[Destination],
    airports: Sequence[str],
    days: Sequence[date],
    budget: ProviderCallBudget | None = None,
) -> ProviderAcquisitionPlan:
    """Decide the whole question set up front, inside the budget."""
    budget = budget or ProviderCallBudget()
    chosen, dropped = rank_candidates(
        destinations,
        request,
        limit=budget.max_destinations,
        exploration_share=budget.exploration_share,
    )
    travelers = max(request.travelers, 1)
    used_airports = tuple(sorted(airports)[: budget.max_airport_variants])
    used_days = tuple(sorted(days)[: budget.max_date_variants])
    if not used_days:
        used_days = ()
    city_ids = tuple(d.id for d in chosen)

    edges: list[AcquisitionEdge] = []
    for day in used_days:
        for airport in used_airports:
            for city in city_ids:
                edges.append(AcquisitionEdge(airport, city, day, travelers))
                edges.append(AcquisitionEdge(city, airport, day, travelers))
        if budget.include_inter_city:
            for first in city_ids:
                for second in city_ids:
                    if first != second:
                        edges.append(AcquisitionEdge(first, second, day, travelers))

    # Order before truncating, so a budget cut removes the same edges every
    # time rather than whichever the dict happened to yield.
    #
    # Sorted by *position within its day* first, so truncation thins every day
    # evenly instead of keeping day one whole and deleting day five entirely.
    # The latter is worse than useless: it buys outbound flights the traveller
    # can never continue from, and the search reports no trips because the data
    # stops, not because the trips do.
    edges.sort(key=lambda e: (e.origin, e.destination, e.day))
    by_day: dict[date, list[AcquisitionEdge]] = {}
    for edge in edges:
        by_day.setdefault(edge.day, []).append(edge)
    interleaved: list[AcquisitionEdge] = []
    for row in zip(*by_day.values()) if by_day else []:
        interleaved.extend(row)
    seen = set(interleaved)
    interleaved.extend(e for e in edges if e not in seen)
    edges = interleaved
    kept = edges[: budget.max_offer_requests]
    return ProviderAcquisitionPlan(
        edges=tuple(kept),
        destinations=city_ids,
        airports=used_airports,
        days=used_days,
        budget=budget,
        travelers=travelers,
        dropped_edges=len(edges) - len(kept),
        dropped_destinations=tuple(d.id for d in dropped),
    )


@dataclass(frozen=True, slots=True)
class SnapshotIssue:
    kind: ProviderFailureKind
    detail: str
    occurrences: int = 1


@dataclass(slots=True)
class OfferSnapshot:
    """Every offer this search is allowed to consider, fetched once.

    The optimizer searches *this*, not a provider. That separation is what
    makes a combinatorial search safe against a rate-limited external API - and
    it is checkable, because :class:`SnapshotTransportProvider` has no way to
    make a request even if something asked it to.
    """

    offers_by_edge: dict[AcquisitionEdge, list[TransportOption]] = field(
        default_factory=dict
    )
    generated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    travelers: int = 1
    requests_made: int = 0
    requests_planned: int = 0
    issues: list[SnapshotIssue] = field(default_factory=list)
    truncated: bool = False

    @property
    def offer_count(self) -> int:
        return sum(len(v) for v in self.offers_by_edge.values())

    @property
    def earliest_expiry(self) -> datetime | None:
        """When the first offer in this snapshot dies, if any of them say.

        The whole snapshot is only as current as its shortest-lived member, so
        this is what a caller must watch before treating any price as quotable.
        """
        deadlines = [
            option.provider_ref.expires_at
            for options in self.offers_by_edge.values()
            for option in options
            if option.provider_ref is not None and option.provider_ref.expires_at
        ]
        return min(deadlines) if deadlines else None

    def record(self, kind: ProviderFailureKind, detail: str) -> None:
        for existing in self.issues:
            if existing.kind is kind and existing.detail == detail:
                existing.occurrences += 1
                return
        self.issues.append(SnapshotIssue(kind=kind, detail=detail))


class SnapshotTransportProvider:
    """A :class:`TransportDataProvider` that reads only from a snapshot.

    Deliberately holds no HTTP client, no host and no token, so "beam search
    makes no network calls" is guaranteed by construction rather than by
    reviewing every call site. A route that was never acquired returns an empty
    list, which the optimizer already handles as "no connection here".
    """

    def __init__(self, snapshot: OfferSnapshot, *, travelers: int = 1) -> None:
        self.snapshot = snapshot
        self.travelers = max(travelers, 1)
        """The party this snapshot was acquired for.

        Carried because the protocol's ``search`` cannot express it, and a
        lookup that ignored it would read another party's prices out of the
        very key designed to keep them apart.
        """
        self.lookups = 0
        self.misses = 0

    def search(
        self, origin: str, destination: str, departure_date: date
    ) -> list[TransportOption]:
        self.lookups += 1
        options = self.snapshot.offers_by_edge.get(
            AcquisitionEdge(origin, destination, departure_date, self.travelers)
        )
        if options is None:
            self.misses += 1
            return []
        return list(options)


def acquire(
    plan: ProviderAcquisitionPlan,
    fetch: Callable[[AcquisitionEdge], Iterable[TransportOption]],
    *,
    now: Callable[[], datetime] | None = None,
) -> OfferSnapshot:
    """Execute a plan, once, and collect the answers.

    ``fetch`` is injected so this is testable without a provider and so the
    caller decides what caching and resilience wrap the real call. Failures are
    recorded as typed issues and never as an absence of flights: a timeout on
    one route must not make that route look unserved.
    """
    snapshot = OfferSnapshot(
        travelers=plan.travelers,
        requests_planned=plan.planned_request_count,
        truncated=plan.is_truncated,
        generated_at=(now or (lambda: datetime.now(timezone.utc)))(),
    )
    if plan.is_truncated:
        snapshot.record(
            ProviderFailureKind.CALL_BUDGET_EXHAUSTED,
            f"provider coverage was bounded: {plan.dropped_edges} route/date "
            f"combinations and {len(plan.dropped_destinations)} destinations "
            "were not priced",
        )
    for edge in plan.edges:
        try:
            options = list(fetch(edge))
        except Exception as error:
            snapshot.record(
                ProviderFailureKind.UNAVAILABLE,
                f"{edge.origin}->{edge.destination} on {edge.day}: {type(error).__name__}",
            )
            continue
        finally:
            snapshot.requests_made += 1
        if options:
            snapshot.offers_by_edge[edge] = options
    return snapshot
