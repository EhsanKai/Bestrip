"""A real transport provider, end to end (V4).

V3 shipped protocols and a stub that raised ``NotImplementedError``. These
tests drive the real integration - auth, request shape, response mapping,
currency, inventory, pagination, failure - through recorded payloads.

**No network call is made here, and that is the design.** An integration whose
tests need a live API and a secret is an integration nobody runs. The HTTP
client is injected, so the only thing this repository is missing to go live is
a credential.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from detoura.models.money import FixedExchangeRates, PriceNormalizer
from detoura.models.transport import TransportType
from detoura.providers.amadeus import (
    AmadeusAuthError,
    AmadeusTransportProvider,
    parse_iso_duration,
)
from detoura.providers.cache import CachingTransportProvider
from detoura.providers.http import (
    HttpMetrics,
    HttpResponse,
    ProviderHttpError,
    RateLimiter,
    RateLimitExceeded,
    RetryingHttpClient,
    UrllibHttpClient,
)
from detoura.providers.transport import TransportDataProvider

DAY = date(2026, 9, 10)


# ---------------------------------------------------------------------------
# A recorded upstream
# ---------------------------------------------------------------------------
def offer(
    offer_id: str,
    *,
    total: str = "118.40",
    currency: str = "EUR",
    depart: str = "2026-09-10T08:40:00",
    arrive: str = "2026-09-10T10:05:00",
    duration: str | None = "PT1H25M",
    travelers: int = 2,
    seats: int | None = 4,
    carrier: str = "LH",
    mode: str = "FLIGHT",
) -> dict:
    """One Amadeus flight offer, in the shape the API actually returns."""
    body = {
        "id": offer_id,
        "itineraries": [
            {
                "segments": [
                    {
                        "departure": {"iataCode": "CGN", "at": depart},
                        "arrival": {"iataCode": "VIE", "at": arrive},
                        "carrierCode": carrier,
                        "transportMode": mode,
                    }
                ]
            }
        ],
        "price": {"currency": currency, "grandTotal": total, "total": total},
        "travelerPricings": [{"travelerId": str(i)} for i in range(1, travelers + 1)],
    }
    if duration is not None:
        body["itineraries"][0]["duration"] = duration
    if seats is not None:
        body["numberOfBookableSeats"] = seats
    return body


TOKEN_BODY = json.dumps({"access_token": "tok-abc", "expires_in": 1799})


class RecordedHttp:
    """An :class:`HttpClient` that replays scripted responses.

    Records every request so the tests can assert on what was actually sent -
    the half of an integration that response mapping alone never checks.
    """

    def __init__(self, *responses: HttpResponse, token: HttpResponse | None = None):
        self.token_response = token or HttpResponse(200, TOKEN_BODY)
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.token_calls = 0

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "params": dict(params or {}),
                "body": body,
            }
        )
        if url.endswith("/v1/security/oauth2/token"):
            self.token_calls += 1
            return self.token_response
        if not self.responses:
            raise AssertionError(f"no scripted response left for {url}")
        return self.responses.pop(0)


def search_response(*offers: dict, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps({"data": list(offers)}))


def provider(http: RecordedHttp, **kwargs) -> AmadeusTransportProvider:
    return AmadeusTransportProvider(
        client_id="id", client_secret="secret", http_client=http, **kwargs
    )


# ---------------------------------------------------------------------------
# It satisfies the protocol the optimizer depends on
# ---------------------------------------------------------------------------
def test_it_is_a_transport_data_provider():
    """The whole point of the seam: no algorithm change to go live."""
    assert isinstance(provider(RecordedHttp()), TransportDataProvider)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_it_fetches_a_token_and_sends_it():
    http = RecordedHttp(search_response(offer("1")))
    provider(http).search("CGN", "VIE", DAY)
    assert http.token_calls == 1
    search = http.requests[-1]
    assert search["headers"]["Authorization"] == "Bearer tok-abc"


def test_the_token_is_reused_across_searches():
    """Re-authenticating per request would triple a search's call volume."""
    http = RecordedHttp(*[search_response(offer(str(i))) for i in range(4)])
    live = provider(http)
    for _ in range(4):
        live.search("CGN", "VIE", DAY)
    assert http.token_calls == 1


def test_an_expired_token_is_refreshed():
    now = datetime(2026, 9, 1, 12, 0)
    clock = lambda: now  # noqa: E731
    http = RecordedHttp(search_response(offer("1")), search_response(offer("2")))
    live = provider(http, clock=lambda: clock())
    live.search("CGN", "VIE", DAY)
    assert http.token_calls == 1
    now = now + timedelta(hours=2)  # past expires_in
    live.search("CGN", "VIE", DAY)
    assert http.token_calls == 2


def test_the_token_expires_early_by_a_safety_margin():
    """A token that expires between the check and the call is a 401 mid-search."""
    now = datetime(2026, 9, 1, 12, 0)
    http = RecordedHttp(search_response(offer("1")), search_response(offer("2")))
    live = provider(http, clock=lambda: now)
    live.search("CGN", "VIE", DAY)
    # expires_in is 1799s; the margin is 60s, so the token must be gone by 1740s.
    now = now + timedelta(seconds=1745)
    live.search("CGN", "VIE", DAY)
    assert http.token_calls == 2


def test_rejected_credentials_raise_rather_than_return_nothing():
    """An empty list here would look like a network with no flights in it."""
    http = RecordedHttp(token=HttpResponse(401, '{"error":"invalid_client"}'))
    with pytest.raises(AmadeusAuthError):
        provider(http).search("CGN", "VIE", DAY)


def test_a_token_response_without_a_token_is_an_error():
    http = RecordedHttp(token=HttpResponse(200, "{}"))
    with pytest.raises(AmadeusAuthError, match="no access_token"):
        provider(http).search("CGN", "VIE", DAY)


def test_credentials_are_required():
    with pytest.raises(ValueError, match="client_id"):
        AmadeusTransportProvider(client_id="", client_secret="s")


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------
def test_the_search_request_carries_the_route_and_date():
    http = RecordedHttp(search_response(offer("1")))
    provider(http).search("CGN", "VIE", DAY)
    params = http.requests[-1]["params"]
    assert params["originLocationCode"] == "CGN"
    assert params["destinationLocationCode"] == "VIE"
    assert params["departureDate"] == "2026-09-10"
    assert params["currencyCode"] == "EUR"


def test_the_page_limit_is_sent():
    http = RecordedHttp(search_response(offer("1")))
    provider(http, page_limit=7).search("CGN", "VIE", DAY)
    assert http.requests[-1]["params"]["max"] == "7"


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------
def test_an_offer_becomes_a_transport_option():
    http = RecordedHttp(search_response(offer("1")))
    [option] = provider(http).search("CGN", "VIE", DAY)
    assert option.origin == "CGN" and option.destination == "VIE"
    assert option.departure == datetime(2026, 9, 10, 8, 40)
    assert option.arrival == datetime(2026, 9, 10, 10, 5)
    assert option.duration_minutes == 85
    assert option.transport_type is TransportType.FLIGHT
    assert option.operator == "LH"


def test_a_party_quote_becomes_a_per_person_price():
    """The domain model is per person; the API quotes the party. Getting this
    backwards doubles or halves every trip in the search."""
    http = RecordedHttp(search_response(offer("1", total="118.40", travelers=2)))
    [option] = provider(http).search("CGN", "VIE", DAY)
    assert option.price_per_person == 59.20
    assert option.total_price(2) == 118.40


def test_a_foreign_currency_is_converted_at_the_boundary():
    """Nothing downstream of a SearchState has ever seen a foreign currency."""
    http = RecordedHttp(
        search_response(offer("1", total="100.00", currency="GBP", travelers=1))
    )
    live = provider(
        http,
        currency="GBP",
        # Units of EUR per one unit of the key currency.
        normalizer=PriceNormalizer(rates=FixedExchangeRates({"EUR": 1.0, "GBP": 1.17})),
    )
    [option] = live.search("CGN", "VIE", DAY)
    assert option.price_per_person == 117.00


def test_an_unconfigured_currency_fails_loudly():
    """A missing rate is a misconfiguration, not a malformed record.

    Skipping the offer would turn "nobody configured GBP" into "there are no
    flights" - silent, total, and indistinguishable from a quiet route.
    """
    http = RecordedHttp(
        search_response(offer("1", total="100.00", currency="GBP", travelers=1))
    )
    live = provider(
        http, normalizer=PriceNormalizer(rates=FixedExchangeRates({"EUR": 1.0}))
    )
    with pytest.raises(ProviderHttpError, match="cannot price a GBP quote"):
        live.search("CGN", "VIE", DAY)


def test_seat_inventory_is_carried_through():
    http = RecordedHttp(search_response(offer("1", seats=3)))
    [option] = provider(http).search("CGN", "VIE", DAY)
    assert option.seats_available == 3
    assert option.has_seats_for(3) and not option.has_seats_for(4)


def test_a_missing_seat_count_is_unknown_not_unlimited():
    http = RecordedHttp(search_response(offer("1", seats=None)))
    [option] = provider(http).search("CGN", "VIE", DAY)
    assert option.seats_available is None
    assert option.has_seats_for(9)


def test_the_transport_mode_is_mapped():
    http = RecordedHttp(search_response(offer("1", mode="TRAIN")))
    [option] = provider(http).search("CGN", "VIE", DAY)
    assert option.transport_type is TransportType.TRAIN


def test_an_unknown_mode_falls_back_to_flight():
    http = RecordedHttp(search_response(offer("1", mode="TELEPORT")))
    [option] = provider(http).search("CGN", "VIE", DAY)
    assert option.transport_type is TransportType.FLIGHT


def test_duration_falls_back_to_the_timestamps():
    http = RecordedHttp(search_response(offer("1", duration=None)))
    [option] = provider(http).search("CGN", "VIE", DAY)
    assert option.duration_minutes == 85


def test_results_are_cheapest_first_whatever_the_api_returns():
    """The optimizer's contract, not the API's ordering."""
    http = RecordedHttp(
        search_response(
            offer("a", total="300.00"), offer("b", total="100.00"), offer("c", total="200.00")
        )
    )
    prices = [o.price_per_person for o in provider(http).search("CGN", "VIE", DAY)]
    assert prices == sorted(prices)


def test_a_malformed_offer_is_skipped_not_fatal():
    """One bad record must not cost the traveler the other nineteen."""
    http = RecordedHttp(
        search_response({"id": "broken"}, offer("good"), {"itineraries": []})
    )
    options = provider(http).search("CGN", "VIE", DAY)
    assert len(options) == 1
    assert options[0].id.endswith("good")


def test_an_impossible_offer_is_skipped():
    """Arrival before departure is rejected by the domain model, and that
    rejection must not abort the search."""
    http = RecordedHttp(
        search_response(
            offer("backwards", depart="2026-09-10T10:00:00", arrive="2026-09-10T08:00:00"),
            offer("fine"),
        )
    )
    assert len(provider(http).search("CGN", "VIE", DAY)) == 1


def test_a_z_suffixed_timestamp_parses():
    http = RecordedHttp(
        search_response(offer("1", depart="2026-09-10T08:40:00Z", arrive="2026-09-10T10:05:00Z"))
    )
    [option] = provider(http).search("CGN", "VIE", DAY)
    assert option.departure == datetime(2026, 9, 10, 8, 40)


# ---------------------------------------------------------------------------
# "Nothing found" is not an error
# ---------------------------------------------------------------------------
def test_an_empty_page_is_an_empty_list():
    assert provider(RecordedHttp(search_response())).search("CGN", "VIE", DAY) == []


def test_a_404_is_an_empty_list():
    """No flights that day is an answer, not a failure."""
    http = RecordedHttp(HttpResponse(404, '{"errors":[{"detail":"not found"}]}'))
    assert provider(http).search("CGN", "VIE", DAY) == []


def test_a_broken_integration_does_raise():
    """The opposite failure: silently returning nothing for a 500 would look
    like a network with no flights in it."""
    http = RecordedHttp(*[HttpResponse(500, "{}")] * 8)
    with pytest.raises(ProviderHttpError):
        provider(http).search("CGN", "VIE", DAY)


def test_a_truncated_body_is_a_provider_error_not_a_json_error():
    http = RecordedHttp(HttpResponse(200, '{"data": [{"id"'))
    with pytest.raises(ProviderHttpError, match="not JSON"):
        provider(http).search("CGN", "VIE", DAY)


# ---------------------------------------------------------------------------
# The admissible bound
# ---------------------------------------------------------------------------
def test_min_price_is_the_cheapest_fare():
    http = RecordedHttp(search_response(offer("a", total="300.00"), offer("b", total="100.00")))
    assert provider(http).min_price("CGN", "VIE", DAY) == 50.0  # 100 / 2 travelers


def test_min_price_is_none_when_it_cannot_find_out():
    """Admissibility: "unknown" must mean "do not prune", never "assume
    expensive". Guessing here is the exact bug V3 found in its return bound."""
    http = RecordedHttp(*[HttpResponse(503, "{}")] * 8)
    assert provider(http).min_price("CGN", "VIE", DAY) is None


def test_min_price_is_none_on_an_empty_route():
    assert provider(RecordedHttp(search_response())).min_price("CGN", "VIE", DAY) is None


def test_min_price_never_exceeds_a_bookable_fare():
    """The property that makes it safe to prune on."""
    http = RecordedHttp(
        search_response(offer("a", total="300.00"), offer("b", total="140.00")),
        search_response(offer("a", total="300.00"), offer("b", total="140.00")),
    )
    live = provider(http)
    bound = live.min_price("CGN", "VIE", DAY)
    assert all(bound <= o.price_per_person for o in live.search("CGN", "VIE", DAY))


# ---------------------------------------------------------------------------
# Retries, backoff, rate limiting
# ---------------------------------------------------------------------------
class Flaky:
    def __init__(self, *statuses: int):
        self.statuses = list(statuses)
        self.calls = 0

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls += 1
        status = self.statuses.pop(0) if self.statuses else 200
        return HttpResponse(status, "{}" if status != 429 else "{}", {})


def test_a_transient_failure_is_retried():
    inner = Flaky(503, 503, 200)
    client = RetryingHttpClient(inner, sleep=lambda _: None)
    assert client.request("GET", "https://x").ok
    assert inner.calls == 3
    assert client.metrics.retries == 2


def test_a_client_error_is_not_retried():
    """Retrying a 400 just spends the rate-limit budget twice."""
    inner = Flaky(400)
    client = RetryingHttpClient(inner, sleep=lambda _: None)
    assert client.request("GET", "https://x").status == 400
    assert inner.calls == 1


def test_the_retry_budget_is_finite():
    inner = Flaky(*[503] * 20)
    client = RetryingHttpClient(inner, max_retries=2, sleep=lambda _: None)
    with pytest.raises(ProviderHttpError):
        client.request("GET", "https://x")
    assert inner.calls == 3
    assert client.metrics.failures == 1


def test_exhausted_rate_limiting_has_its_own_error():
    inner = Flaky(*[429] * 20)
    client = RetryingHttpClient(inner, max_retries=1, sleep=lambda _: None)
    with pytest.raises(RateLimitExceeded):
        client.request("GET", "https://x")
    assert client.metrics.rate_limited == 2


def test_backoff_is_exponential():
    slept: list[float] = []
    client = RetryingHttpClient(
        Flaky(*[503] * 10), max_retries=3, backoff_seconds=0.5, sleep=slept.append
    )
    with pytest.raises(ProviderHttpError):
        client.request("GET", "https://x")
    assert slept == [0.5, 1.0, 2.0]


def test_backoff_is_capped():
    slept: list[float] = []
    client = RetryingHttpClient(
        Flaky(*[503] * 10),
        max_retries=5,
        backoff_seconds=1.0,
        max_backoff_seconds=2.0,
        sleep=slept.append,
    )
    with pytest.raises(ProviderHttpError):
        client.request("GET", "https://x")
    assert max(slept) == 2.0


def test_the_servers_own_retry_after_wins():
    """Guessing shorter than the server asked is how a client gets banned."""

    class Advising:
        calls = 0

        def request(self, *a, **k):
            self.calls += 1
            return HttpResponse(429, "{}", {"Retry-After": "4"})

    slept: list[float] = []
    client = RetryingHttpClient(Advising(), max_retries=1, backoff_seconds=0.1, sleep=slept.append)
    with pytest.raises(RateLimitExceeded):
        client.request("GET", "https://x")
    assert slept == [4.0]


def test_a_connection_failure_is_retried_like_a_503():
    class Broken:
        calls = 0

        def request(self, *a, **k):
            self.calls += 1
            if self.calls < 3:
                raise ProviderHttpError("connection reset")
            return HttpResponse(200, "{}")

    inner = Broken()
    client = RetryingHttpClient(inner, sleep=lambda _: None)
    assert client.request("GET", "https://x").ok
    assert inner.calls == 3


def test_the_rate_limiter_spaces_calls():
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(0.25, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.acquire()
    assert slept == [] or all(s == pytest.approx(0.25) for s in slept)
    assert len(slept) == 2


def test_a_zero_interval_never_waits():
    limiter = RateLimiter(0.0, sleep=lambda _: pytest.fail("must not sleep"))
    assert limiter.acquire() == 0.0


def test_the_transport_reports_what_it_did():
    client = RetryingHttpClient(Flaky(503, 200), sleep=lambda _: None)
    client.request("GET", "https://x")
    metrics = client.metrics.as_dict()
    assert metrics["requests"] == 2.0
    assert metrics["retries"] == 1.0
    assert metrics["failures"] == 0.0


def test_metrics_start_empty():
    assert HttpMetrics().as_dict()["requests"] == 0.0


def test_invalid_transport_settings_are_rejected():
    with pytest.raises(ValueError, match="max_retries"):
        RetryingHttpClient(Flaky(), max_retries=-1)
    with pytest.raises(ValueError, match="min_interval"):
        RateLimiter(-1.0)


def test_the_stdlib_client_satisfies_the_protocol():
    """It is not exercised against the network here - only its shape is."""
    from detoura.providers.http import HttpClient

    assert isinstance(UrllibHttpClient(), HttpClient)


# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,minutes",
    [("PT1H25M", 85), ("PT45M", 45), ("PT3H", 180), ("PT2H15M30S", 135), ("PT0M", 0)],
)
def test_iso_durations_parse(value, minutes):
    assert parse_iso_duration(value) == minutes


@pytest.mark.parametrize("value", ["1H25M", "PTXM", "PT1Q", ""])
def test_malformed_durations_are_rejected(value):
    with pytest.raises(ValueError):
        parse_iso_duration(value)


# ---------------------------------------------------------------------------
# It composes with the rest of the engine
# ---------------------------------------------------------------------------
def test_it_works_behind_the_caching_decorator():
    """The measured claim from V3: caching is what makes a metered API viable."""
    http = RecordedHttp(search_response(offer("1")))
    cached = CachingTransportProvider(provider(http))
    for _ in range(50):
        cached.search("CGN", "VIE", DAY)
    assert cached.stats.lookups == 50
    assert cached.stats.misses == 1


def test_the_planner_accepts_it_without_an_algorithm_change():
    """Going live is a constructor argument."""
    from detoura.services.planner import TravelPlanner

    http = RecordedHttp(*[search_response(offer(str(i))) for i in range(400)])
    planner = TravelPlanner(transport_provider=provider(http))
    assert isinstance(planner.transport, CachingTransportProvider)
    assert isinstance(planner.transport.inner, AmadeusTransportProvider)


def test_the_search_never_calls_the_api_itself():
    """External calls stay at the provider boundary, out of the algorithm."""
    import ast
    import pathlib

    import detoura

    root = pathlib.Path(detoura.__file__).parent
    offenders = []
    for package in ("algorithms", "constraints"):
        for path in (root / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                if any(
                    n.split(".")[0] in {"urllib", "http", "socket", "requests", "httpx"}
                    for n in names
                ):
                    offenders.append(f"{path}: {names}")
    assert offenders == []
