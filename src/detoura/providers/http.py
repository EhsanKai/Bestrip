"""The HTTP plumbing a real provider needs (V4).

V3 shipped provider *protocols* and stubs that raised ``NotImplementedError``,
and its own limitations section listed "the HTTP clients and their response
mapping" as the missing half. This is that half: everything between "the
optimizer asked for options on a route" and "an upstream API answered".

Three deliberate choices.

**stdlib only.** ``urllib.request`` is not glamorous, but a travel optimizer
should not grow a networking stack to make one kind of POST request. The
abstraction is :class:`HttpClient`, so a deployment that already has ``httpx``
or ``requests`` supplies its own in about fifteen lines.

**Trust is explicit (V8).** ``urllib`` verifies TLS against the interpreter's
default trust store, and a build whose ``openssl_cafile`` points at a path that
does not exist - a Framework Python, a slim container - fails *every* HTTPS
call at the handshake, before a request leaves the machine. So the client
pins :mod:`certifi`'s CA bundle when it is installed, unless the operator has
set ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` to say otherwise. Verification is
never disabled: an unverified TLS connection to a payments-capable API is not
a fallback, it is the vulnerability.

**The client is injected, always.** :class:`HttpClient` is a protocol, and
every provider takes one. That is what makes a real integration testable
against recorded payloads instead of the network - which is how the tests in
this repository exercise the real provider without ever making a call.

**Failure is a first-class case.** Rate limits, timeouts and 5xx are what a
travel API does on a bad day, and a provider that raises on any of them takes a
search that had eight hundred viable itineraries down with it. Retries and
budgeting live here so every provider inherits the same behaviour.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable
from urllib.parse import urlencode


def _build_ssl_context() -> ssl.SSLContext:
    """A TLS context that verifies, using a CA bundle that actually exists.

    Order of trust, most explicit first:

    1. ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` in the environment - an operator
       override, honoured by :func:`ssl.create_default_context` itself.
    2. :mod:`certifi`'s bundled roots, when the package is installed. This is
       the case the function exists for: it makes a real Duffel call work on a
       host whose system trust store is unconfigured, which the V8 sandbox
       probe hit on the first attempt.
    3. The interpreter default, when neither of the above is available.

    ``check_hostname`` and ``CERT_REQUIRED`` are left at their defaults
    throughout. There is no code path here that disables verification.
    """
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())

#: Status codes worth trying again. 408 and 429 are explicit "come back later";
#: 5xx is the server having a bad moment. Everything else is a bug in the
#: request and retrying it just spends the rate-limit budget twice.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 0.5
DEFAULT_MAX_BACKOFF_SECONDS = 8.0


class ProviderHttpError(RuntimeError):
    """An upstream call failed in a way retrying will not fix."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimitExceeded(ProviderHttpError):
    """The upstream rate limit was hit and the retry budget is spent."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Just enough of a response for a JSON API."""

    status: int
    body: str
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> dict:
        """Parse the body, turning malformed JSON into a provider error.

        A 200 with a truncated body is a real failure mode of real APIs, and it
        must not surface as a ``JSONDecodeError`` from somewhere deep in a
        search.
        """
        try:
            parsed = json.loads(self.body or "{}")
        except json.JSONDecodeError as exc:
            raise ProviderHttpError(
                f"upstream returned {self.status} with a body that is not JSON: {exc}",
                status=self.status,
            ) from exc
        if not isinstance(parsed, dict):
            raise ProviderHttpError(
                f"expected a JSON object, got {type(parsed).__name__}",
                status=self.status,
            )
        return parsed

    @property
    def retry_after_seconds(self) -> float | None:
        """The server's own advice, when it gives any."""
        raw = self.headers.get("Retry-After") or self.headers.get("retry-after")
        if raw is None:
            return None
        try:
            return max(float(raw), 0.0)
        except ValueError:
            return None


@runtime_checkable
class HttpClient(Protocol):
    """The one operation a JSON travel API needs."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        body: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse: ...


class UrllibHttpClient:
    """An :class:`HttpClient` over the standard library.

    Deliberately thin: it does not retry, count, or interpret. Those are
    :class:`RetryingHttpClient`'s job, so a caller supplying their own transport
    inherits the retry behaviour rather than having to reimplement it.
    """

    def __init__(self, *, user_agent: str = "travel-planner/4.0") -> None:
        self.user_agent = user_agent
        # Built once: loading a CA bundle per request is wasted work, and the
        # trust configuration cannot change under a running process anyway.
        self._ssl_context = _build_ssl_context()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        body: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        if params:
            url = f"{url}?{urlencode(sorted(params.items()))}"
        request = urllib.request.Request(
            url,
            method=method.upper(),
            data=body.encode("utf-8") if body is not None else None,
            headers={"User-Agent": self.user_agent, **(headers or {})},
        )
        # The context is used only for https:// URLs; urllib ignores it for
        # plain http, so passing it unconditionally is safe.
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=self._ssl_context
            ) as response:
                return HttpResponse(
                    status=response.status,
                    body=response.read().decode("utf-8", errors="replace"),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            # An HTTP error status is a response, not an exception: the caller
            # decides whether 429 is retryable, and the body usually says why.
            return HttpResponse(
                status=exc.code,
                body=exc.read().decode("utf-8", errors="replace"),
                headers=dict(exc.headers.items()) if exc.headers else {},
            )
        except urllib.error.URLError as exc:
            raise ProviderHttpError(f"could not reach {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ProviderHttpError(f"{url} timed out after {timeout}s") from exc


@dataclass
class HttpMetrics:
    """What the transport actually did, for the same reason provider caching is
    measured: an integration's cost should be visible before anyone pays it."""

    requests: int = 0
    retries: int = 0
    failures: int = 0
    rate_limited: int = 0
    seconds_waiting: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "requests": float(self.requests),
            "retries": float(self.retries),
            "failures": float(self.failures),
            "rate_limited": float(self.rate_limited),
            "seconds_waiting": round(self.seconds_waiting, 4),
        }


class RateLimiter:
    """A minimum interval between calls, enforced by waiting.

    Travel APIs are quoted in requests per second, and the cheapest way to stay
    inside one is not to exceed it. The clock and the sleep are both injected so
    tests assert the spacing without spending the time.
    """

    def __init__(
        self,
        min_interval_seconds: float = 0.0,
        *,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")
        self.min_interval = min_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._next_allowed = 0.0

    def acquire(self) -> float:
        """Block until the next call is allowed. Returns seconds waited."""
        if self.min_interval <= 0:
            return 0.0
        now = self._clock()
        wait = self._next_allowed - now
        if wait > 0:
            self._sleep(wait)
            now += wait
        else:
            wait = 0.0
        self._next_allowed = now + self.min_interval
        return wait


class RetryingHttpClient:
    """Wraps any :class:`HttpClient` with retries, backoff and rate limiting.

    A decorator rather than a base class, for the same reason the provider
    caches are: it composes with a transport this project did not write.
    """

    def __init__(
        self,
        inner: HttpClient,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        rate_limiter: RateLimiter | None = None,
        sleep=time.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.inner = inner
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.rate_limiter = rate_limiter or RateLimiter()
        self._sleep = sleep
        self.metrics = HttpMetrics()

    def _delay(self, attempt: int, response: HttpResponse | None) -> float:
        """Exponential backoff, unless the server said how long to wait.

        Honouring ``Retry-After`` is not politeness: guessing shorter than the
        server asked is how a client gets itself banned.
        """
        advised = response.retry_after_seconds if response is not None else None
        if advised is not None:
            return min(advised, self.max_backoff_seconds)
        return min(self.backoff_seconds * (2**attempt), self.max_backoff_seconds)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        body: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        last: HttpResponse | None = None
        last_error: ProviderHttpError | None = None

        for attempt in range(self.max_retries + 1):
            self.metrics.seconds_waiting += self.rate_limiter.acquire()
            self.metrics.requests += 1
            try:
                response = self.inner.request(
                    method, url, headers=headers, params=params, body=body,
                    timeout=timeout,
                )
            except ProviderHttpError as exc:
                # A connection failure is exactly as retryable as a 503.
                last_error, response = exc, None
            else:
                last_error, last = None, response
                if response.ok or response.status not in RETRYABLE_STATUS:
                    return response
                if response.status == 429:
                    self.metrics.rate_limited += 1

            if attempt == self.max_retries:
                break
            delay = self._delay(attempt, response)
            self.metrics.retries += 1
            self.metrics.seconds_waiting += delay
            if delay:
                self._sleep(delay)

        self.metrics.failures += 1
        if last_error is not None:
            raise last_error
        assert last is not None
        message = (
            f"{method} {url} failed with {last.status} after "
            f"{self.max_retries + 1} attempts"
        )
        if last.status == 429:
            raise RateLimitExceeded(message, status=last.status)
        raise ProviderHttpError(message, status=last.status)
