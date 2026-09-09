"""Duffel-backed offer acquisition for a real trip search (V8).

V7.5 built the acquisition *shape* - a bounded plan, a snapshot the optimizer
searches instead of a provider, typed truncation - and tested it against the
synthetic transport graph. This module is the piece that was explicitly left
for V8: running that plan against real Duffel Test Mode.

Everything the V7.5 architecture guarantees still holds, and this module is
built so it cannot quietly break them:

* **Beam search still makes zero network calls.** This produces an
  :class:`~detoura.services.acquisition.OfferSnapshot`; the optimizer is handed
  a :class:`~detoura.services.acquisition.SnapshotTransportProvider`, which has
  no client. Acquisition is the only network stage and it is this one.
* **The plan is counted before it is sent.** ``build_plan`` shapes the request
  set against a :class:`ProviderCallBudget`; this module executes it and no
  more. The Duffel provider's own hard ``max_calls`` ceiling is a second
  backstop.
* **Truncation is never silent.** A route that returned more offers than the
  cap kept is recorded on the snapshot as ``OFFERS_TRUNCATED`` - the probe
  checkpoint found 23 offers displayed as 20 with no trace.
* **A city is not an airport.** The catalog reasons in city ids ("Berlin"); a
  real Offer Request needs an IATA code ("BER"). The translation happens only
  around the HTTP call - the snapshot stays in city space so the optimizer can
  still connect a leg's arrival city to the next leg's departure.
* **An unresolvable route is a recorded gap, not an empty market.** A city with
  no ``primary_airport`` cannot be priced; that is disclosed, never rendered as
  "no flights".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from time import perf_counter
from typing import Callable, Mapping, Sequence

from ..data.destinations import ORIGIN_AIRPORTS
from ..models.destination import Destination
from ..models.transport import TransportOption
from ..models.trip import TripRequest
from ..providers.cache import ExpiringProviderCache
from ..providers.duffel import DuffelTransportProvider
from ..providers.failures import ProviderFailureKind
from ..services.acquisition import (
    AcquisitionEdge,
    OfferSnapshot,
    ProviderAcquisitionPlan,
    ProviderCallBudget,
    acquire,
    build_plan,
)

#: IATA codes that are already airports rather than catalog cities - the origin
#: side of an edge, and any city whose id someone passed as a code directly.
KNOWN_AIRPORT_CODES: frozenset[str] = frozenset(a.code for a in ORIGIN_AIRPORTS)


class UnresolvableRoute(RuntimeError):
    """An edge names a place with no known airport.

    Raised inside the fetch callable so :func:`acquire` records it as a typed
    issue on the snapshot and carries on with the routes that did resolve -
    exactly how a timeout on one edge is handled.
    """


def resolve_airport(node: str, city_airports: Mapping[str, str]) -> str | None:
    """The IATA code to query a provider with for ``node``, or ``None``.

    ``node`` is whatever an :class:`AcquisitionEdge` carries: an origin airport
    code, or a destination city id. A three-letter upper-case token is taken to
    already be a code; anything else is looked up in the city table.
    """
    if node in KNOWN_AIRPORT_CODES:
        return node
    if len(node) == 3 and node.isupper() and node.isalpha():
        return node
    return city_airports.get(node)


def city_airport_table(destinations: Sequence[Destination]) -> dict[str, str]:
    """``city id -> primary airport`` for every catalog entry that has one."""
    return {d.id: d.primary_airport for d in destinations if d.primary_airport}


@dataclass(slots=True)
class RealSupplyMetrics:
    """What the real acquisition pass actually did.

    Every number here is something the probe checkpoint asked to be made
    visible before anyone pays for it: calls sent, wall time, cache behaviour,
    and how the raw offer count relates to what was kept.
    """

    edges_planned: int = 0
    edges_answered: int = 0
    edges_unresolved: int = 0
    provider_calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    seconds_in_provider: float = 0.0
    offers_received: int = 0
    offers_retained: int = 0
    offers_truncated: int = 0
    offers_unusable: int = 0

    def as_dict(self) -> dict[str, float]:
        return {
            "edges_planned": self.edges_planned,
            "edges_answered": self.edges_answered,
            "edges_unresolved": self.edges_unresolved,
            "provider_calls": self.provider_calls,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "seconds_in_provider": round(self.seconds_in_provider, 4),
            "offers_received": self.offers_received,
            "offers_retained": self.offers_retained,
            "offers_truncated": self.offers_truncated,
            "offers_unusable": self.offers_unusable,
        }


@dataclass(slots=True)
class RealSupplyResult:
    plan: ProviderAcquisitionPlan
    snapshot: OfferSnapshot
    metrics: RealSupplyMetrics
    unresolved_nodes: tuple[str, ...] = ()
    """Places named by the plan that have no airport, so were never priced."""


def _earliest_expiry(options: Sequence[TransportOption]) -> datetime | None:
    deadlines = [
        o.provider_ref.expires_at
        for o in options
        if o.provider_ref is not None and o.provider_ref.expires_at is not None
    ]
    return min(deadlines) if deadlines else None


def acquire_real_supply(
    request: TripRequest,
    *,
    duffel: DuffelTransportProvider,
    destinations: Sequence[Destination],
    airports: Sequence[str],
    days: Sequence[date],
    budget: ProviderCallBudget | None = None,
    cache: ExpiringProviderCache | None = None,
    now: Callable[[], datetime] | None = None,
) -> RealSupplyResult:
    """Run one bounded acquisition pass against real Duffel Test Mode.

    ``duffel`` is injected already configured - the caller owns the test-token
    check and the HTTP client, so this function never sees a credential. The
    cache is injected too, so a caller can share one across a session or pass a
    fresh one per request; a fresh :class:`ExpiringProviderCache` is the default.
    """
    clock = now or (lambda: datetime.now(timezone.utc))
    plan = build_plan(
        request,
        destinations=destinations,
        airports=airports,
        days=days,
        budget=budget,
    )
    city_airports = city_airport_table(destinations)
    cache = cache if cache is not None else ExpiringProviderCache()
    metrics = RealSupplyMetrics(edges_planned=plan.planned_request_count)
    unresolved: set[str] = set()

    def fetch(edge: AcquisitionEdge) -> list[TransportOption]:
        origin = resolve_airport(edge.origin, city_airports)
        destination = resolve_airport(edge.destination, city_airports)
        if origin is None or destination is None:
            if origin is None:
                unresolved.add(edge.origin)
            if destination is None:
                unresolved.add(edge.destination)
            raise UnresolvableRoute(
                f"{edge.origin}->{edge.destination}: no airport for "
                f"{edge.origin if origin is None else edge.destination}"
            )
        if origin == destination:
            # Two catalog cities served by the same airport, or an airport to
            # itself. Not a flight, not an error.
            return []

        key = (origin, destination, edge.day, edge.travelers)

        def call() -> list[TransportOption]:
            started = perf_counter()
            try:
                return duffel.fetch_offers(
                    origin, destination, edge.day, travelers=edge.travelers
                )
            finally:
                metrics.seconds_in_provider += perf_counter() - started
                metrics.provider_calls += 1

        options = cache.get_or_compute(key, call, expires_from=_earliest_expiry)
        # Back to city space: the beam matches a leg's destination to the next
        # leg's origin by string, and "BER" would not connect to "Berlin".
        return [
            option.model_copy(
                update={"origin": edge.origin, "destination": edge.destination}
            )
            for option in options
        ]

    snapshot = acquire(plan, fetch, now=clock)

    metrics.cache_hits = cache.stats.hits
    metrics.cache_misses = cache.stats.misses
    metrics.offers_received = duffel.offers_received
    metrics.offers_retained = duffel.offers_retained
    metrics.offers_truncated = duffel.offers_truncated
    metrics.offers_unusable = len(duffel.offers_dropped)
    metrics.edges_unresolved = sum(
        1
        for edge in plan.edges
        if resolve_airport(edge.origin, city_airports) is None
        or resolve_airport(edge.destination, city_airports) is None
    )
    metrics.edges_answered = len(snapshot.offers_by_edge)

    if duffel.offers_truncated:
        snapshot.record(
            ProviderFailureKind.OFFERS_TRUNCATED,
            f"{duffel.offers_truncated} offers beyond the per-route cap of "
            f"{duffel.max_offers} were dropped; the cheapest were kept",
        )
    if unresolved:
        snapshot.truncated = True
        snapshot.record(
            ProviderFailureKind.UNAVAILABLE,
            "no airport is mapped for: " + ", ".join(sorted(unresolved)),
        )

    return RealSupplyResult(
        plan=plan,
        snapshot=snapshot,
        metrics=metrics,
        unresolved_nodes=tuple(sorted(unresolved)),
    )
