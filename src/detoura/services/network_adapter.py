"""The authorized network acquisition adapter (V9 Phase 2.5 §6, §29).

This is the *only* code path allowed to make an HTTP request on behalf of an
``AUTHORIZED_WEB_SOURCE`` / ``API_SOURCE`` Market-Prior source. It builds on
the existing V4 HTTP plumbing (:mod:`detoura.providers.http` —
``RetryingHttpClient``, ``RateLimiter``) rather than duplicating it, and adds
exactly the controls a *market-data* acquisition needs on top: fail-closed
authorization, a domain allowlist, a minimal SSRF guard, a response-size
limit, redirect validation (each hop re-checked, never auto-followed across
hosts), and detection of the handful of "this source does not want to be
automated" signals Phase 2.5 is required to stop on rather than fight
(CAPTCHA, persistent 403/429, redirect off the allowed domain).

Optimizes for correctness, politeness, recoverability and traceability — not
maximum throughput.
"""

from __future__ import annotations

import ipaddress
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping
from urllib.parse import urlencode, urlparse

from ..models.market_prior_acquisition import SourceRegistration, StopReason
from ..providers.http import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpClient,
    HttpResponse,
    ProviderHttpError,
    RateLimiter,
    RateLimitExceeded,
    RetryingHttpClient,
    _build_ssl_context,
)

if TYPE_CHECKING:
    from ..persistence.db import Database

#: A market-fare page or a small JSON payload, not a firehose. Configurable
#: per fetcher; this is the conservative default.
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000

DEFAULT_USER_AGENT = (
    "DetouraMarketPriorBot/1.0 (+authorized acquisition; see "
    "docs/V9_MARKET_PRIOR_SOURCE_OPTIONS.md)"
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_CAPTCHA_MARKERS = (
    "captcha", "are you a human", "verify you are human", "cf-challenge",
    "checking your browser", "unusual traffic from your computer",
    "access denied", "request blocked",
)


class AccessBlocked(RuntimeError):
    """A stop condition fired. Never bypassed, never retried around — the
    caller is expected to mark the task/source accordingly and halt (§20)."""

    def __init__(self, reason: StopReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.detail = detail


class BudgetExhausted(RuntimeError):
    """The job's hard request budget has no capacity left for one more real
    outbound attempt.

    Deliberately **not** a :class:`~detoura.providers.http.ProviderHttpError`
    — :class:`~detoura.providers.http.RetryingHttpClient` only retries
    ``ProviderHttpError``, so this exception type propagates out of the retry
    loop immediately rather than being retried against (which would just
    raise the same exception again after a wasted backoff sleep, and worse,
    would let the loop reach ``max_retries`` without a caller ever finding
    out *why* — see the "retry request accounting" fix in
    ``docs/V9_PHASE2_5_MARKET_PRIOR_ACQUISITION.md``)."""


def _budget_gated_client(inner: HttpClient, *, db: "Database", job_id: str, source_id: str,
                          min_interval_seconds: float, sleep=time.sleep) -> HttpClient:
    """Wraps ``inner`` so **every** actual outbound attempt — including every
    :class:`RetryingHttpClient` retry, not just once per logical task — is
    (a) charged against the job's hard request budget via
    :func:`persistence.market_prior_acquisition.reserve_request` (atomic,
    correct across threads *and processes*), and (b) paced against a
    persisted, cross-process source rate limit via
    :func:`persistence.market_prior_acquisition.reserve_rate_limit_slot`,
    *before* the real request is ever made. A single ``reserve_request()``
    once per task is not sufficient when the HTTP layer can retry internally
    — each retry is its own real request and must consume its own budget
    unit (§16, §17, §59)."""
    from ..persistence import market_prior_acquisition as store

    class _Gated:
        def request(self, method, url, *, headers=None, params=None, body=None,
                    timeout=DEFAULT_TIMEOUT_SECONDS):
            if not store.reserve_request(db, job_id):
                raise BudgetExhausted(
                    f"job {job_id} has no request-budget capacity left for source {source_id!r}"
                )
            wait = store.reserve_rate_limit_slot(
                db, source_id, min_interval_seconds=min_interval_seconds,
            )
            if wait > 0:
                sleep(wait)
            return inner.request(method, url, headers=headers, params=params, body=body, timeout=timeout)

    return _Gated()


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _domain_allowed(host: str, base_domain: str) -> bool:
    host = host.lower().rstrip(".")
    base = base_domain.lower().rstrip(".")
    return host == base or host.endswith("." + base)


def looks_like_challenge(status: int, body: str) -> bool:
    """A heuristic, not a certainty — false positives just mean an extra,
    safe stop; false negatives are the risk this whole module is built to
    minimise, so the check is intentionally broad."""
    low = (body or "").lower()
    if any(m in low for m in _CAPTCHA_MARKERS):
        return True
    return status == 403 and "forbidden" in low and len(low) < 4000


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turns off urllib's automatic redirect-following.

    ``urlopen`` follows 301/302/303/307/308 itself by default, re-requesting
    a possibly different host with no chance for this module to validate it
    — exactly the redirect-escape SSRF risk §29 calls out. Returning ``None``
    here tells urllib "do not build a follow-up request"; the 3xx status and
    ``Location`` header are returned to the caller instead, so
    :class:`AuthorizedHttpFetcher` can validate and follow each hop itself.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NonRedirectingHttpClient:
    """An :class:`HttpClient` over the standard library that never follows a
    redirect on its own (see :class:`_NoRedirectHandler`)."""

    def __init__(self, *, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.user_agent = user_agent
        self._opener = urllib.request.build_opener(
            _NoRedirectHandler(), urllib.request.HTTPSHandler(context=_build_ssl_context()),
        )

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None, body: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        if params:
            url = f"{url}?{urlencode(sorted(params.items()))}"
        request = urllib.request.Request(
            url, method=method.upper(),
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
            # A 3xx also lands here because _NoRedirectHandler declined to
            # follow it - urllib treats "no follow-up request" as an error.
            return HttpResponse(
                status=exc.code,
                body=exc.read().decode("utf-8", errors="replace") if exc.fp else "",
                headers=dict(exc.headers.items()) if exc.headers else {},
            )
        except urllib.error.URLError as exc:
            raise ProviderHttpError(f"could not reach {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ProviderHttpError(f"{url} timed out after {timeout}s") from exc


@dataclass
class AuthorizedHttpFetcher:
    """Fetches one URL for one :class:`SourceRegistration`, or raises
    :class:`AccessBlocked` — there is no silent-degrade path."""

    registration: SourceRegistration
    http_client: HttpClient
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    max_redirects: int = 3
    user_agent: str = DEFAULT_USER_AGENT
    #: When both are supplied, budget *and* rate-limit enforcement move to
    #: persisted, cross-process-safe state (every real attempt, including
    #: retries) instead of this instance's own in-memory ``RateLimiter`` —
    #: see :func:`_budget_gated_client`. Omitted only by call sites (mostly
    #: tests) that exercise this fetcher in isolation, with no job to charge.
    db: "Database | None" = None
    job_id: str | None = None
    metrics: dict = field(default_factory=lambda: {
        "requests": 0, "blocked": 0, "redirects_followed": 0,
    })

    def __post_init__(self) -> None:
        pol = self.registration.rate_limit_policy
        if self.db is not None and self.job_id is not None:
            gated = _budget_gated_client(
                self.http_client, db=self.db, job_id=self.job_id,
                source_id=self.registration.source_id,
                min_interval_seconds=pol.effective_min_interval_seconds,
            )
            self._client = RetryingHttpClient(gated, rate_limiter=RateLimiter(0.0))
        else:
            self._rate_limiter = RateLimiter(min_interval_seconds=pol.effective_min_interval_seconds)
            self._client = RetryingHttpClient(self.http_client, rate_limiter=self._rate_limiter)

    def _check_authorized(self) -> None:
        if not self.registration.network_allowed:
            self.metrics["blocked"] += 1
            raise AccessBlocked(
                StopReason.AUTHORIZATION_DISABLED,
                f"source {self.registration.source_id} is "
                f"{self.registration.authorization_status.value}, not cleared for network access",
            )

    def _check_host(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            self.metrics["blocked"] += 1
            raise AccessBlocked(StopReason.DOMAIN_NOT_ALLOWED, f"non-https scheme {parsed.scheme!r}")
        host = parsed.hostname or ""
        if not host or _is_ip_literal(host):
            self.metrics["blocked"] += 1
            raise AccessBlocked(StopReason.DOMAIN_NOT_ALLOWED, f"unusable/IP-literal host {host!r}")
        base = self.registration.base_domain
        if not base or not _domain_allowed(host, base):
            self.metrics["blocked"] += 1
            raise AccessBlocked(StopReason.DOMAIN_NOT_ALLOWED, f"{host} is outside allowed domain {base!r}")
        return host

    def fetch(
        self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        self._check_authorized()
        current_url, redirects = url, 0
        while True:
            self._check_host(current_url)
            merged = {"User-Agent": self.user_agent, **(headers or {})}
            self.metrics["requests"] += 1
            try:
                response = self._client.request(
                    method, current_url, headers=merged, params=params if redirects == 0 else None,
                    timeout=timeout,
                )
            except RateLimitExceeded as exc:
                raise AccessBlocked(StopReason.RATE_LIMITED, str(exc)) from exc
            except ProviderHttpError as exc:
                raise AccessBlocked(StopReason.ACCESS_DENIED, str(exc)) from exc

            size = len(response.body.encode("utf-8", errors="ignore"))
            if size > self.max_response_bytes:
                self.metrics["blocked"] += 1
                raise AccessBlocked(
                    StopReason.RESPONSE_TOO_LARGE, f"{size} bytes exceeds {self.max_response_bytes}",
                )
            if response.status in _REDIRECT_STATUSES:
                location = response.headers.get("Location") or response.headers.get("location")
                if not location:
                    raise AccessBlocked(StopReason.ACCESS_DENIED, "redirect with no Location header")
                if redirects >= self.max_redirects:
                    raise AccessBlocked(StopReason.ACCESS_DENIED, "too many redirects")
                redirects += 1
                self.metrics["redirects_followed"] += 1
                current_url = location
                continue
            if response.status == 403:
                if looks_like_challenge(response.status, response.body):
                    raise AccessBlocked(StopReason.CAPTCHA_DETECTED, "challenge markers in 403 response")
                raise AccessBlocked(StopReason.ACCESS_DENIED, "403 Forbidden")
            if looks_like_challenge(response.status, response.body):
                raise AccessBlocked(StopReason.CAPTCHA_DETECTED, "challenge markers in response body")
            return response
