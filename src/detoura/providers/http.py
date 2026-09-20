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

import ipaddress
import json
import logging
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable
from urllib.parse import urlencode, urlparse

from ..observability import log_event
from ..observability import metrics as _metrics

_logger = logging.getLogger(__name__)


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

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Declines to auto-follow a 3xx. ``urlopen``'s default opener follows
    301/302/303/307/308 itself, re-requesting a possibly different host with
    no chance for the caller to validate it first - the redirect-escape SSRF
    risk V9 Phase 6's Network/SSRF slice (Invariant 4) calls out. Returning
    ``None`` tells urllib "do not build a follow-up request"; the 3xx status
    and ``Location`` header come back to the caller as an ``HTTPError``
    instead, to validate and decide whether to follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_no_redirect_opener(context: "ssl.SSLContext | None" = None) -> urllib.request.OpenerDirector:
    """An opener that never follows a redirect on its own - see
    :class:`_NoRedirectHandler`. Shared by any caller that fetches a URL
    whose destination is not a fixed, hardcoded, trusted host (see
    ``services/network_adapter.py`` and
    ``services/destination_images/wikimedia.py``)."""
    return urllib.request.build_opener(
        _NoRedirectHandler(), urllib.request.HTTPSHandler(context=context or _build_ssl_context()),
    )


class UnsafeHostError(RuntimeError):
    """A hostname/IP literal is, or resolves to, a non-public address
    (loopback, private/RFC1918, link-local - including the
    169.254.0.0/16 cloud-metadata range, multicast, reserved, or
    unspecified). Raised instead of ever connecting (V9 Phase 6
    Network/SSRF slice, Invariant 2/3)."""


def _is_unsafe_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True for anything that is not an ordinary public/routable address.

    An IPv4-mapped IPv6 address (``::ffff:a.b.c.d``) is unwrapped and its
    embedded IPv4 address checked too, so wrapping a private/loopback IPv4
    in IPv6 syntax is not a way past this check.

    ``not ip.is_global`` (V9 Phase 6, independent-review finding), not a
    manual OR-chain of ``is_loopback``/``is_private``/``is_link_local``/
    ``is_reserved``/``is_unspecified``: that chain was found, empirically,
    to miss RFC 6598 Carrier-Grade NAT space (``100.64.0.0/10``) - some
    cloud/container network fabrics route this range internally
    specifically because naive RFC1918-only filters miss it, a known real
    SSRF-filter-bypass class. ``ipaddress``'s own ``is_global`` already
    excludes CGNAT along with everything the old chain listed (verified
    directly, not assumed) - EXCEPT two categories ``is_global`` reports
    ``True`` for and so must stay as explicit extra conditions: multicast
    (globally-scoped addressing, not a private range) and a couple of
    IPv6 reserved-but-``is_global``-true prefixes (e.g. the ``64:ff9b::/96``
    NAT64 translation prefix, which can embed an arbitrary IPv4 address in
    its low bits - a real class of IPv6-notation SSRF-filter bypass)."""
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None and _is_unsafe_ip(mapped):
            return True
    return not ip.is_global or ip.is_multicast or ip.is_reserved


def assert_safe_public_host(host: str, *, resolver=socket.getaddrinfo) -> None:
    """Fail closed unless ``host`` - a literal IP, or a hostname resolved
    through ``resolver`` - is (or resolves only to) an ordinary public
    address. Raises :class:`UnsafeHostError` otherwise.

    This is deliberately a separate check from "is this hostname on an
    allowed domain list" (see e.g. ``services/network_adapter.py``'s
    ``_check_host``): a domain-suffix allowlist says which NAME may be
    requested, never what IP ADDRESS that name actually resolves to at
    connect time. A mistyped, malicious, or later-compromised DNS record
    for an otherwise-allowlisted domain (the trivial case: a registered
    source's ``base_domain`` is literally ``"localhost"``, or a private-
    network hostname) is invisible to a hostname-string check alone - only
    resolving and inspecting the actual address catches it. Every resolved
    address is checked, not just the first: a DNS answer with several
    records where even one is unsafe is rejected outright, since a
    connecting library may pick any of them.

    This closes the reachable, practical gap (a bad or hostile DNS answer
    *at request time*) - it does not fully close a textbook DNS-rebinding
    race (a different answer between this check and the transport's own,
    separate resolution moments later in the same request), which would
    need IP-level connection pinning to close completely. That residual
    gap is documented, not claimed fixed - see
    docs/V9_PHASE6_SECURITY_REPORT.md's Network/SSRF slice section.
    """
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if _is_unsafe_ip(literal):
            raise UnsafeHostError(f"{host} is not a public address")
        return
    try:
        infos = resolver(host, None)
    except OSError as exc:
        raise UnsafeHostError(f"{host} could not be resolved: {exc}") from exc
    if not infos:
        raise UnsafeHostError(f"{host} resolved to no addresses")
    for info in infos:
        sockaddr = info[4]
        raw_addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(raw_addr.split("%")[0])  # strip an IPv6 zone id
        except ValueError:
            raise UnsafeHostError(f"{host} resolved to an unparseable address {raw_addr!r}")
        if _is_unsafe_ip(ip):
            raise UnsafeHostError(f"{host} resolves to non-public address {ip}")


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


def _classify_status(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status in (408, 504):
        return "timeout"
    if 400 <= status < 500:
        return "client_error"
    if 500 <= status < 600:
        return "server_error"
    return "unexpected_status"


def _classify_error(exc: BaseException) -> str:
    """Distinguish timeout / rate-limit / network failure for observability
    (V9 Limited Beta observability contract §8) - never the exception's
    message, which for a ``ProviderHttpError`` may embed the request URL."""
    if isinstance(exc, RateLimitExceeded):
        return "rate_limited"
    if isinstance(exc, (TimeoutError,)):
        return "timeout"
    if isinstance(exc, ProviderHttpError):
        status = exc.status
        if status is not None:
            return _classify_status(status)
        message = str(exc)
        return "timeout" if "timed out" in message else "network_error"
    return "network_error"


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
        # V9 Phase 6 Network/SSRF slice (independent-review finding): every
        # provider built on this client (Duffel, Amadeus, Stripe) previously
        # went through urllib's global `urlopen`, which installs the
        # DEFAULT redirect handler - it follows a 3xx automatically and
        # forwards every request header, `Authorization` included, onto the
        # follow-up request with no same-host check at all (confirmed
        # directly against CPython's own `HTTPRedirectHandler` source). The
        # destination for these providers is a hardcoded, trusted host, not
        # attacker-influenced - but a compromised/DNS-hijacked upstream
        # could still exfiltrate these bearer credentials to an attacker
        # host via a single redirect. A no-redirect opener (already used by
        # services/network_adapter.py and destination_images/wikimedia.py
        # this same slice) turns a 3xx into an ordinary ``HttpResponse``
        # instead - every existing caller's own `response.ok`/status-code
        # handling already treats an unexpected 3xx as a failure, so this
        # is a behavior-preserving change for the normal case (these APIs
        # do not redirect) and a fail-closed one for the abnormal case.
        self._opener = build_no_redirect_opener(self._ssl_context)

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
        try:
            with self._opener.open(request, timeout=timeout) as response:
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
        provider: str = "unknown",
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
        #: A bounded, small-cardinality label for observability only (V9
        #: Limited Beta observability contract §8) - "duffel"/"amadeus"/
        #: "network_adapter"/... never an offer id or other identifier.
        #: Defaults to "unknown" so existing callers that do not pass it
        #: keep working exactly as before.
        self.provider = provider

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
        """Retries/backoff/rate-limiting - unchanged - wrapped with an
        observability boundary (V9 Limited Beta observability contract §8):
        one structured log event + metric per call, classifying the outcome
        (ok / timeout / rate_limited / client_error / server_error /
        network_error), never the request/response body or headers (which,
        for a provider like Stripe, carry the bearer credential)."""
        start = time.monotonic()
        try:
            response = self._request_impl(
                method, url, headers=headers, params=params, body=body, timeout=timeout,
            )
        except BaseException as exc:
            self._log_outcome(method, url, outcome=_classify_error(exc), start=start)
            raise
        else:
            outcome = "ok" if response.ok else _classify_status(response.status)
            self._log_outcome(method, url, outcome=outcome, start=start, status=response.status)
            return response

    def _log_outcome(
        self, method: str, url: str, *, outcome: str, start: float, status: int | None = None,
    ) -> None:
        duration_ms = round((time.monotonic() - start) * 1000, 2)
        _metrics.observe_provider_call(
            provider=self.provider, operation=method, outcome=outcome, duration_ms=duration_ms,
        )
        log_event(
            _logger, "provider_request_completed",
            level=logging.INFO if outcome == "ok" else logging.WARNING,
            provider=self.provider, operation=method, host=urlparse(url).hostname or "",
            outcome=outcome, status=status, duration_ms=duration_ms,
        )

    def _request_impl(
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
