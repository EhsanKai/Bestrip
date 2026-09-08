"""Duffel, behind the provider boundary (V7.5).

A sibling of :mod:`detoura.providers.amadeus`, not new architecture. It
satisfies :class:`~detoura.providers.transport.TransportDataProvider`, so the
optimizer sees `TransportOption` and never learns what an Offer is. Every
Duffel-shaped dictionary in this file stops at :meth:`_map_slice`.

Three rules this module exists to keep.

**Test mode or nothing.** The token is checked for a ``duffel_test_`` prefix
before any request is built. A missing, malformed or live-looking token fails
closed with no network call, because the cost of being wrong here is a real
booking made with somebody's real money.

**A slice is a leg; a segment is not a city.** ``CGN -> LHR -> MAD`` is one
journey the traveller takes to reach Madrid, with a connection in London.
Mapping segments to legs would make the optimizer count Heathrow as a visited
city, inflate the trip's city count, and let a layover masquerade as a
destination.

**Absent is unknown.** Duffel states baggage as an inclusion quantity, and says
nothing at all about bags it does not mention. Missing therefore maps to
``UNKNOWN``, never to ``INCLUDED`` and never to a fee of zero - the V7 Phase 3
guarantee, applied at the point where real data arrives.

The fixtures this was built against are **modelled on documented shapes, not
captured from the API**. Until one real sandbox response has been compared
against them, the live path is UNVERIFIED.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from ..models.baggage import (
    BaggageAllowance,
    BaggageKind,
    BaggagePolicy,
    BaggageStatus,
)
from ..models.money import BASE_CURRENCY, Money, PriceNormalizer
from ..models.provider_reference import ProviderOfferReference
from ..models.transport import TransportOption, TransportType
from .amadeus import parse_iso_duration
from .failures import ProviderFailureKind
from .http import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpClient,
    ProviderHttpError,
    RetryingHttpClient,
    UrllibHttpClient,
)

log = logging.getLogger(__name__)

DEFAULT_HOST = "https://api.duffel.com"
OFFER_REQUEST_PATH = "/air/offer_requests"
DUFFEL_API_VERSION = "v2"

#: The only token prefix this adapter will transmit.
TEST_TOKEN_PREFIX = "duffel_test_"

#: Duffel's own baggage vocabulary, mapped to ours.
_BAG_TYPES = {"carry_on": BaggageKind.CABIN_BAG, "checked": BaggageKind.CHECKED_BAG}


class DuffelConfigurationError(RuntimeError):
    """The adapter refuses to run as configured.

    Raised before any socket is opened. Carries no token material: the message
    describes the *shape* problem and never echoes the value, because an
    exception string ends up in logs, tickets and screenshots.
    """


class DuffelAuthError(ProviderHttpError):
    """Duffel rejected the credentials."""


def redact(token: str | None) -> str:
    """A token rendered safe to print.

    Never the value, and never a prefix long enough to be useful to anyone who
    finds it. Callers that want to say *which* token is configured get the
    mode, which is the only part anyone debugging actually needs.
    """
    if not token:
        return "<unset>"
    return "duffel_test_<redacted>" if is_test_token(token) else "<non-test token>"


def is_test_token(token: str | None) -> bool:
    return bool(token) and token.startswith(TEST_TOKEN_PREFIX)


class ProviderCallBudgetExceeded(RuntimeError):
    """More requests were attempted than this instance may make.

    A backstop against something bypassing the acquisition plan. Loud on
    purpose: the alternative to an exception here is an invoice.
    """


class DuffelTransportProvider:
    """Live Duffel offers, normalized into Detoura's own models.

    **Deliberately not wireable into beam search.** An earlier draft of this
    docstring showed::

        TravelPlanner(transport_provider=DuffelTransportProvider(...))

    which structurally satisfies ``TransportDataProvider`` and therefore works
    - and issues one Offer Request per route the beam explores, which measured
    2,177 of them for a single SMART search. The "zero network calls during
    search" guarantee held only as long as every caller remembered to route
    through :mod:`detoura.services.acquisition`, and a guarantee that depends
    on remembering is not one.

    So :meth:`search` refuses. :meth:`fetch_offers` is the network path, the
    acquisition stage calls it, and a planner handed this object fails loudly
    on its first lookup instead of quietly spending a rate limit.

    Correct use::

        plan = build_plan(request, destinations=..., airports=..., days=...)
        snapshot = acquire(plan, lambda edge: duffel.fetch_offers(
            edge.origin, edge.destination, edge.day, travelers=edge.travelers))
        planner = TravelPlanner(
            transport_provider=SnapshotTransportProvider(snapshot)
        )
    """

    def __init__(
        self,
        *,
        access_token: str,
        http_client: HttpClient | None = None,
        host: str = DEFAULT_HOST,
        currency: str = BASE_CURRENCY,
        normalizer: PriceNormalizer | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_offers: int = 20,
        travelers: int = 1,
        max_calls: int = 500,
        acquisition_only: bool = True,
        allow_non_test_token: bool = False,
    ) -> None:
        if not access_token:
            raise DuffelConfigurationError(
                "DUFFEL_ACCESS_TOKEN is not set; refusing to build a Duffel "
                "provider without one"
            )
        if not is_test_token(access_token) and not allow_non_test_token:
            # Fail closed. A token that is not clearly test-mode might be live,
            # and "might be live" is the same as live when the downside is a
            # real order against a real card.
            raise DuffelConfigurationError(
                "the configured Duffel token is not a test-mode token "
                f"(expected a {TEST_TOKEN_PREFIX!r} prefix); refusing to send "
                "any request. Set a sandbox token, or pass "
                "allow_non_test_token=True deliberately."
            )
        self._token = access_token
        self.host = host.rstrip("/")
        self.currency = currency
        self.normalizer = normalizer or PriceNormalizer()
        self.timeout = timeout
        self.max_offers = max_offers
        self.travelers = max(travelers, 1)
        """Party size used when a caller does not state one.

        The ``TransportDataProvider`` protocol carries no traveller count, so a
        provider driven through it prices every trip for one adult however many
        people are going. Acquisition passes the real number per edge; this is
        the floor for anything that does not.
        """
        self.max_calls = max_calls
        """Hard ceiling on requests from one instance, whatever the caller does.

        A backstop, not the budget. `ProviderCallBudget` shapes the plan before
        anything is sent; this catches something bypassing the plan entirely,
        so the failure is a loud exception rather than a bill.
        """
        self._acquisition_only = acquisition_only
        self.http = (
            http_client
            if isinstance(http_client, RetryingHttpClient)
            else RetryingHttpClient(http_client or UrllibHttpClient())
        )
        self.search_calls = 0
        self.offers_seen = 0
        self.offers_dropped: list[tuple[str, ProviderFailureKind]] = []
        """Offers that arrived but could not be represented, and why.

        Recorded rather than silently discarded. An unconvertible currency is a
        misconfiguration, not an absence of flights, and a caller that reports
        "no results" without checking this is reporting our bug as the market's
        answer.
        """

    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Duffel-Version": DUFFEL_API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def search(
        self, origin: str, destination: str, departure_date: date
    ) -> list[TransportOption]:
        """The ``TransportDataProvider`` entry point - which this refuses.

        Beam search calls this once per route it explores. Answering would turn
        a combinatorial search into combinatorial API traffic, so the honest
        response is to fail immediately and say where to go instead. Nothing is
        counted and nothing is sent.
        """
        if self._acquisition_only:
            raise DuffelConfigurationError(
                "DuffelTransportProvider must not be driven by beam search: "
                "one search explores thousands of routes and would issue an "
                "Offer Request for each. Acquire offers first with "
                "detoura.services.acquisition.build_plan/acquire and give the "
                "planner a SnapshotTransportProvider, or pass "
                "acquisition_only=False deliberately."
            )
        return self.fetch_offers(origin, destination, departure_date)

    def fetch_offers(
        self,
        origin: str,
        destination: str,
        departure_date: date,
        *,
        travelers: int | None = None,
    ) -> list[TransportOption]:
        """One Offer Request. The only method here that touches the network.

        Never raises on "nothing found" - an empty list is a real answer, and a
        provider that raises instead takes down a search that had hundreds of
        other viable itineraries. Genuine failures do propagate, because
        returning nothing for a broken integration looks exactly like a network
        with no flights in it.
        """
        travelers = self.travelers if travelers is None else max(travelers, 1)
        if self.search_calls >= self.max_calls:
            raise ProviderCallBudgetExceeded(
                f"refusing request {self.search_calls + 1}: this provider's "
                f"hard ceiling of {self.max_calls} calls is spent. Something "
                "is bypassing the acquisition plan."
            )
        self.search_calls += 1
        payload = {
            "data": {
                "slices": [{
                    "origin": origin,
                    "destination": destination,
                    "departure_date": departure_date.isoformat(),
                }],
                "passengers": [{"type": "adult"} for _ in range(max(travelers, 1))],
                "cabin_class": "economy",
            }
        }
        response = self.http.request(
            "POST",
            f"{self.host}{OFFER_REQUEST_PATH}",
            headers=self._headers(),
            body=json.dumps(payload),
            timeout=self.timeout,
        )
        if response.status in (401, 403):
            raise DuffelAuthError(
                f"Duffel rejected the credentials with {response.status}",
                status=response.status,
            )
        if response.status == 422:
            # A request Duffel understood and refused - usually an unservable
            # route. That is "no flights", not a broken integration.
            return []
        if not response.ok:
            raise ProviderHttpError(
                f"Duffel offer request returned {response.status}",
                status=response.status,
            )
        try:
            body = response.json()
        except Exception as error:
            raise ProviderHttpError(f"Duffel response was not JSON: {error}") from error

        return self.parse_offers(body, origin, destination, travelers=travelers)

    # ------------------------------------------------------------------
    # Parsing - the whole Duffel vocabulary stops here
    # ------------------------------------------------------------------
    def parse_offers(
        self, body: dict, origin: str, destination: str, *, travelers: int = 1
    ) -> list[TransportOption]:
        """Normalize a whole Offer Request payload.

        Separated from :meth:`search` so every shape in the fixture file can be
        exercised without a socket. One malformed offer must not cost the
        traveller the other flights on the page, so mapping failures are
        recorded and skipped rather than raised.
        """
        offers = (body or {}).get("data", {}).get("offers", []) or []
        options: list[TransportOption] = []
        for offer in offers:
            self.offers_seen += 1
            try:
                option = self._map_offer(offer, origin, destination, travelers)
            except Exception as error:  # defensive: a broken offer is not fatal
                log.debug("Duffel offer skipped: %s", error)
                self.offers_dropped.append(
                    (str(offer.get("id", "<no id>")), ProviderFailureKind.MALFORMED_RESPONSE)
                )
                continue
            if option is not None:
                options.append(option)
        options.sort(key=lambda o: (o.price_per_person, o.departure, o.id))
        return options[: self.max_offers]

    def _map_offer(
        self, offer: dict, origin: str, destination: str, travelers: int
    ) -> TransportOption | None:
        offer_id = offer.get("id")
        slices = offer.get("slices") or []
        if not offer_id or not slices:
            self.offers_dropped.append(
                (str(offer_id or "<no id>"), ProviderFailureKind.MALFORMED_RESPONSE)
            )
            return None

        if len(slices) > 1:
            # An offer covering several slices quotes ONE total for all of
            # them, and nothing in it says how that total divides. We only ever
            # ask for a single one-way slice, so this shape answers a question
            # we did not put - and attaching the whole round-trip figure to the
            # outbound leg would price a one-way flight at the return fare.
            # Splitting it evenly would be worse: an invented number wearing a
            # provider's authority. Recorded and skipped.
            self.offers_dropped.append((offer_id, ProviderFailureKind.MALFORMED_RESPONSE))
            return None

        # Only the slice that answers the question asked.
        chosen = next(
            (s for s in slices
             if _iata(s.get("origin")) == origin and _iata(s.get("destination")) == destination),
            slices[0],
        )
        price = self._price_per_person(offer, offer_id, travelers)
        if price is None:
            return None

        segments = chosen.get("segments") or []
        if not segments:
            self.offers_dropped.append((offer_id, ProviderFailureKind.MALFORMED_RESPONSE))
            return None

        departure = _parse_dt(segments[0].get("departing_at"))
        arrival = _parse_dt(segments[-1].get("arriving_at"))
        if departure is None or arrival is None or arrival < departure:
            self.offers_dropped.append((offer_id, ProviderFailureKind.MALFORMED_RESPONSE))
            return None

        duration = _duration_minutes(chosen, departure, arrival)
        carrier = (segments[0].get("marketing_carrier") or {}).get("iata_code") or "??"
        flight_no = segments[0].get("marketing_carrier_flight_number") or ""

        return TransportOption(
            id=f"duffel-{offer_id}",
            origin=_iata(chosen.get("origin")) or origin,
            destination=_iata(chosen.get("destination")) or destination,
            departure=departure,
            arrival=arrival,
            price_per_person=price,
            transport_type=TransportType.FLIGHT,
            duration_minutes=duration,
            operator=f"{carrier} {flight_no}".strip(),
            baggage=self._map_baggage(offer, segments),
            provider_ref=self._reference(offer, offer_id, segments),
        )

    # ------------------------------------------------------------------
    def _price_per_person(
        self, offer: dict, offer_id: str, travelers: int
    ) -> float | None:
        """The per-person fare in the optimizer's currency, or ``None``.

        Duffel quotes the **party** total as a decimal string. Dividing is
        correct here and the currency is never assumed: an amount nothing can
        convert is recorded as ``CURRENCY_UNAVAILABLE`` and the offer is
        skipped, because relabelling USD 20 as EUR 20 is the exact defect V7
        Phase 3 had to fix.
        """
        raw = offer.get("total_amount")
        currency = offer.get("total_currency")
        if raw is None or not currency:
            self.offers_dropped.append((offer_id, ProviderFailureKind.MALFORMED_RESPONSE))
            return None
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            self.offers_dropped.append((offer_id, ProviderFailureKind.MALFORMED_RESPONSE))
            return None
        if amount < 0:
            self.offers_dropped.append((offer_id, ProviderFailureKind.MALFORMED_RESPONSE))
            return None
        try:
            base = self.normalizer.to_base(Money(amount=amount, currency=currency))
        except ValueError:
            # A real quote in a currency we have no rate for. Not "no flights".
            self.offers_dropped.append((offer_id, ProviderFailureKind.CURRENCY_UNAVAILABLE))
            return None
        return round(base.amount / max(travelers, 1), 2)

    def _map_baggage(self, offer: dict, segments: list[dict]) -> BaggagePolicy:
        """What this fare says about bags, across every segment of the slice.

        Combined conservatively. A cabin bag included on the first flight and
        unmentioned on the second does **not** make the journey cabin-bag
        friendly: the traveller still has to get the bag onto the second
        aircraft. So a slice is only ``INCLUDED`` when every segment says so,
        and any silence makes the whole slice ``UNKNOWN``.
        """
        services = {
            (s.get("metadata") or {}).get("type"): s
            for s in (offer.get("available_services") or [])
            if s.get("type") == "baggage"
        }
        policy: dict[str, BaggageAllowance] = {}
        for duffel_type, kind in _BAG_TYPES.items():
            policy[kind.value] = self._allowance_for(kind, duffel_type, segments, services)
        return BaggagePolicy(
            # Duffel has no personal-item concept, so we have not been told.
            personal_item=BaggageAllowance.unknown(BaggageKind.PERSONAL_ITEM),
            cabin_bag=policy[BaggageKind.CABIN_BAG.value],
            checked_bag=policy[BaggageKind.CHECKED_BAG.value],
        )

    def _allowance_for(
        self,
        kind: BaggageKind,
        duffel_type: str,
        segments: list[dict],
        services: dict[str, dict],
    ) -> BaggageAllowance:
        # Three outcomes per segment, and they must stay apart:
        #   "included"  the fare carries this bag on this flight
        #   "excluded"  the fare explicitly does not (quantity 0)
        #   "silent"    the segment says nothing about this kind of bag
        # Silence is the one that must never be read as either of the others.
        stated: list[str] = []
        for segment in segments:
            passengers = segment.get("passengers") or []
            if not passengers:
                stated.append("silent")
                continue
            bags = passengers[0].get("baggages")
            if bags is None:
                stated.append("silent")
                continue
            entry = next((b for b in bags if b.get("type") == duffel_type), None)
            if entry is None:
                stated.append("silent")
                continue
            stated.append("included" if entry.get("quantity", 0) >= 1 else "excluded")

        if stated and all(value == "included" for value in stated):
            return BaggageAllowance.included(kind)

        # Not included on every segment. A purchasable service prices it - but
        # only when every segment actually told us something, because a fee
        # quoted against a journey with an unmentioned leg is not a fee for the
        # journey.
        service = services.get(duffel_type)
        if service is not None and "silent" not in stated:
            amount, currency = service.get("total_amount"), service.get("total_currency")
            try:
                money = self.normalizer.to_base(
                    Money(amount=float(amount), currency=currency or BASE_CURRENCY)
                )
                return BaggageAllowance.extra(kind, money.amount, money.currency)
            except (TypeError, ValueError):
                return BaggageAllowance.extra_unpriced(kind)
        return BaggageAllowance.unknown(kind)

    def _reference(
        self, offer: dict, offer_id: str, segments: list[dict]
    ) -> ProviderOfferReference:
        payment = offer.get("payment_requirements") or {}
        instant = payment.get("requires_instant_payment")
        return ProviderOfferReference(
            provider="duffel",
            offer_id=offer_id,
            expires_at=_parse_dt(offer.get("expires_at")),
            # `requires_instant_payment` absent means Duffel did not say, which
            # is not the same as "hold is available".
            hold_supported=(not instant) if isinstance(instant, bool) else None,
            hold_until=_parse_dt(payment.get("payment_required_by")),
            owner_iata=(offer.get("owner") or {}).get("iata_code"),
            raw_segments=tuple(
                {
                    "id": s.get("id"),
                    "origin": _iata(s.get("origin")),
                    "destination": _iata(s.get("destination")),
                    "marketing_carrier": (s.get("marketing_carrier") or {}).get("iata_code"),
                    "flight_number": s.get("marketing_carrier_flight_number"),
                    "departing_at": s.get("departing_at"),
                    "arriving_at": s.get("arriving_at"),
                }
                for s in segments
            ),
        )


# ---------------------------------------------------------------------------
def _iata(place: Any) -> str | None:
    return (place or {}).get("iata_code") if isinstance(place, dict) else None


def _parse_dt(value: Any) -> datetime | None:
    """Duffel timestamps, or ``None`` when unparseable.

    ``None`` rather than an exception: a missing expiry is a fact about the
    offer, and one bad timestamp must not discard a whole page of flights.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_minutes(chosen: dict, departure: datetime, arrival: datetime) -> int:
    """The slice duration Duffel stated, or the one its own timestamps imply."""
    stated = chosen.get("duration")
    if isinstance(stated, str):
        try:
            return parse_iso_duration(stated)
        except ValueError:
            pass
    return int((arrival - departure).total_seconds() // 60)
