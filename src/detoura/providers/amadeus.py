"""A real transport provider, end to end (V4).

V3 left a stub that raised ``NotImplementedError`` and a list of what a real
integration would need. This is that integration, written against the Amadeus
Flight Offers Search shape: OAuth2 client credentials, a paged JSON search
endpoint, prices quoted per party in whatever currency the account is
configured for.

**The only thing missing is a credential.** Every other part is here and
tested - request construction, the token lifecycle, response mapping, currency
and basis normalization, seat inventory, pagination, error handling, empty
results, and the admissible price bound the search prunes on. The tests drive it
through recorded payloads rather than the network, which is also how it should
be tested in CI: an integration whose test suite needs a live API and a secret
is an integration nobody runs.

**What it does not do.** It does not touch the search. The optimizer asks
``TransportDataProvider`` for options on a route; whether that is a dictionary
lookup or an HTTPS round trip is invisible to it, and the caching decorator in
``providers/cache.py`` sits in between either way. Wrapping this class in
:class:`~detoura.providers.cache.CachingTransportProvider` - which
``TravelPlanner`` does automatically - is what turns twelve thousand lookups
into seventeen hundred calls.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from ..models.money import BASE_CURRENCY, Money, PriceBasis, PriceNormalizer
from ..models.transport import TransportOption, TransportType
from .http import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpClient,
    ProviderHttpError,
    RetryingHttpClient,
)

DEFAULT_HOST = "https://test.api.amadeus.com"
TOKEN_PATH = "/v1/security/oauth2/token"
SEARCH_PATH = "/v2/shopping/flight-offers"

#: Seconds of slack on the token's stated lifetime. A token that expires
#: between the check and the call produces a 401 in the middle of a search.
TOKEN_EXPIRY_MARGIN_SECONDS = 60

#: Cap on offers requested per route-date. The search only branches on the
#: cheapest few (``max_transport_options_per_leg``), so asking for hundreds
#: buys nothing and costs quota.
DEFAULT_PAGE_LIMIT = 20

#: How Amadeus names the modes this optimizer models. Anything else - a coach
#: transfer segment, a ferry - maps to the nearest thing the domain has.
SEGMENT_TYPES: dict[str, TransportType] = {
    "FLIGHT": TransportType.FLIGHT,
    "TRAIN": TransportType.TRAIN,
    "BUS": TransportType.BUS,
    "COACH": TransportType.BUS,
}


class AmadeusAuthError(ProviderHttpError):
    """Credentials were rejected. Retrying will not help."""


def parse_iso_duration(value: str) -> int:
    """``PT2H15M`` -> ``135`` minutes.

    Written out rather than pulled from a dependency because it is fifteen
    lines and the alternative is a package for one function.
    """
    if not value.startswith("PT"):
        raise ValueError(f"not an ISO 8601 duration: {value!r}")
    minutes = 0
    number = ""
    for char in value[2:]:
        if char.isdigit():
            number += char
            continue
        if not number:
            raise ValueError(f"malformed duration: {value!r}")
        amount = int(number)
        if char == "H":
            minutes += amount * 60
        elif char == "M":
            minutes += amount
        elif char == "S":
            minutes += amount // 60
        else:
            raise ValueError(f"unexpected unit {char!r} in {value!r}")
        number = ""
    return minutes


class AmadeusTransportProvider:
    """A live :class:`~detoura.providers.transport.TransportDataProvider`.

    Satisfies the protocol the optimizer already depends on, so switching to it
    is a constructor argument::

        planner = TravelPlanner(
            transport_provider=AmadeusTransportProvider(
                client_id=..., client_secret=...,
            )
        )
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        http_client: HttpClient | None = None,
        host: str = DEFAULT_HOST,
        currency: str = BASE_CURRENCY,
        normalizer: PriceNormalizer | None = None,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        clock=datetime.now,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("client_id and client_secret are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self.host = host.rstrip("/")
        self.currency = currency
        self.normalizer = normalizer or PriceNormalizer()
        self.page_limit = page_limit
        self.timeout = timeout
        self._clock = clock
        # Wrapped by default so a caller who passes a bare transport still gets
        # retries and backoff. Passing a RetryingHttpClient is idempotent in
        # effect - it just nests - so callers may configure their own.
        self.http = (
            http_client
            if isinstance(http_client, RetryingHttpClient)
            else RetryingHttpClient(http_client or _default_client())
        )
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self.search_calls = 0

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def _token_is_valid(self) -> bool:
        if self._token is None or self._token_expires_at is None:
            return False
        return self._clock() < self._token_expires_at

    def access_token(self) -> str:
        """A valid bearer token, fetched only when the cached one has expired.

        Re-authenticating per request would triple the call volume of a search
        for no benefit; never re-authenticating produces a 401 halfway through
        one.
        """
        if self._token_is_valid():
            assert self._token is not None
            return self._token

        response = self.http.request(
            "POST",
            f"{self.host}{TOKEN_PATH}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=(
                "grant_type=client_credentials"
                f"&client_id={self.client_id}"
                f"&client_secret={self.client_secret}"
            ),
            timeout=self.timeout,
        )
        if response.status in (400, 401, 403):
            raise AmadeusAuthError(
                f"authentication rejected with {response.status}",
                status=response.status,
            )
        if not response.ok:
            raise ProviderHttpError(
                f"token endpoint returned {response.status}", status=response.status
            )
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise AmadeusAuthError("token response carried no access_token")
        lifetime = int(payload.get("expires_in", 0))
        self._token = str(token)
        self._token_expires_at = self._clock() + timedelta(
            seconds=max(lifetime - TOKEN_EXPIRY_MARGIN_SECONDS, 0)
        )
        return self._token

    # ------------------------------------------------------------------
    # TransportDataProvider
    # ------------------------------------------------------------------
    def search(
        self, origin: str, destination: str, departure_date: date
    ) -> list[TransportOption]:
        """Bookable options for one route on one date, cheapest first.

        **Never raises on "nothing found".** An empty list is the answer to
        "no flights that day", and a provider that raises instead takes down a
        search that had hundreds of other viable itineraries. Genuine failures -
        auth, exhausted retries - do propagate: silently returning nothing for
        a broken integration would look like a network with no flights in it.
        """
        self.search_calls += 1
        response = self.http.request(
            "GET",
            f"{self.host}{SEARCH_PATH}",
            headers={"Authorization": f"Bearer {self.access_token()}"},
            params={
                "originLocationCode": origin,
                "destinationLocationCode": destination,
                "departureDate": departure_date.isoformat(),
                "adults": "1",
                "currencyCode": self.currency,
                "max": str(self.page_limit),
            },
            timeout=self.timeout,
        )
        if response.status == 404:
            return []
        if response.status in (400, 401, 403):
            raise AmadeusAuthError(
                f"search rejected with {response.status}: {_error_detail(response.body)}",
                status=response.status,
            )
        if not response.ok:
            raise ProviderHttpError(
                f"search returned {response.status}", status=response.status
            )

        options: list[TransportOption] = []
        for offer in response.json().get("data", []):
            option = self._map_offer(offer, origin, destination, departure_date)
            if option is not None:
                options.append(option)
        # The optimizer's contract is cheapest-first; the API's ordering is its
        # own business and must not leak into search behaviour.
        options.sort(key=lambda o: (o.price_per_person, o.departure, o.id))
        return options

    def min_price(self, origin: str, destination: str, day: date) -> float | None:
        """Cheapest per-person fare on a route, or ``None`` if unknown.

        ``None`` matters: an *admissible* bound must never overestimate the
        cheapest completion, and "I could not find out" has to mean "do not
        prune", not "assume expensive". Guessing here is precisely the bug V3
        found and fixed in its own return-cost bound.
        """
        try:
            options = self.search(origin, destination, day)
        except ProviderHttpError:
            return None
        return min((o.price_per_person for o in options), default=None)

    # ------------------------------------------------------------------
    # Mapping
    # ------------------------------------------------------------------
    def _map_offer(
        self, offer: dict, origin: str, destination: str, day: date
    ) -> TransportOption | None:
        """One API offer -> one :class:`TransportOption`, or ``None`` if unusable.

        Skipping a malformed offer rather than raising is deliberate: one bad
        record in a page of twenty must not cost the traveler the other
        nineteen.
        """
        try:
            itineraries = offer["itineraries"]
            segments = itineraries[0]["segments"]
            first, last = segments[0], segments[-1]
            departure = _parse_datetime(first["departure"]["at"])
            arrival = _parse_datetime(last["arrival"]["at"])
            price = offer["price"]
            quoted = Money(
                amount=float(price["grandTotal"]),
                currency=str(price.get("currency", self.currency)),
                # Amadeus grandTotal is taxes-included; the field name is the
                # contract, and getting this wrong is the single most common
                # way to be badly wrong about a trip's cost.
                tax_included=True,
            )
        except (KeyError, IndexError, TypeError, ValueError):
            return None

        duration = itineraries[0].get("duration")
        try:
            minutes = (
                parse_iso_duration(duration)
                if duration
                else int((arrival - departure).total_seconds() // 60)
            )
        except ValueError:
            minutes = int((arrival - departure).total_seconds() // 60)

        # The quote covers ``travelerPricings`` people; the domain model wants a
        # per-person figure, and the normalizer is the only place that
        # conversion is allowed to happen.
        #
        # Deliberately *not* inside a skip-the-offer guard: a missing exchange
        # rate is a misconfiguration, not a malformed record, and it fails the
        # same way for every offer. Swallowing it would turn "nobody configured
        # GBP" into "there are no flights", which is the worst kind of bug -
        # silent, total, and indistinguishable from a quiet day on the route.
        party = max(len(offer.get("travelerPricings") or []), 1)
        try:
            per_person = self.normalizer.per_person(quoted, PriceBasis.TOTAL, party)
        except ValueError as exc:
            raise ProviderHttpError(
                f"cannot price a {quoted.currency} quote: {exc}"
            ) from exc

        mode = SEGMENT_TYPES.get(
            str(first.get("transportMode", "FLIGHT")).upper(), TransportType.FLIGHT
        )
        seats = offer.get("numberOfBookableSeats")
        try:
            option = TransportOption(
                id=f"amadeus-{origin}-{destination}-{day.isoformat()}-{offer.get('id', '?')}",
                origin=origin,
                destination=destination,
                departure=departure,
                arrival=arrival,
                price_per_person=per_person,
                transport_type=mode,
                duration_minutes=minutes,
                operator=str(first.get("carrierCode", "amadeus")),
                seats_available=int(seats) if seats is not None else None,
            )
        except ValueError:
            # The domain model rejects impossibilities (arrival before
            # departure, a leg from a city to itself). An upstream that emits
            # one is not a reason to abort the search.
            return None
        return option


def _parse_datetime(value: str) -> datetime:
    """Parse an API timestamp, tolerating a trailing ``Z``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _error_detail(body: str) -> str:
    """The API's own explanation, when it gives one worth repeating."""
    try:
        errors = json.loads(body or "{}").get("errors") or []
        return "; ".join(str(e.get("detail") or e.get("title", "")) for e in errors)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return body[:200]


def _default_client() -> HttpClient:
    from .http import UrllibHttpClient

    return UrllibHttpClient()
