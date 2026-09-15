"""V9 Phase 6 Network / SSRF Security adversarial slice.

Fresh attack pass over Detoura's outbound network surface: URL parsing,
private/internal-network blocking (now enforced against the REAL resolved
IP address, not just the hostname string), redirect safety, and - the
headline finding of this slice, unrelated to SSRF but explicitly in scope
per Invariant 9 - blind HTTP-level retry of non-idempotent Duffel
Order/cancellation/change operations.

No test in this file makes a request to a real private/internal endpoint or
depends on live network access for its SAFETY assertions - a fake resolver
stands in for DNS wherever an unsafe target needs to be proven caught. A
few tests do perform a real (harmless, DNS-only) lookup of long-lived,
stable public infrastructure (``example.com``, Wikimedia's domains) via the
default resolver, matching the existing V9 Phase 2.5 test suite's own
established pattern.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request

import pytest

from detoura.providers.http import (
    HttpResponse,
    ProviderHttpError,
    RetryingHttpClient,
    UnsafeHostError,
    assert_safe_public_host,
)
from detoura.services.destination_images.wikimedia import (
    DomainNotAllowed,
    WikimediaCommonsClient,
    _check_domain,
)
from detoura.services.network_adapter import AccessBlocked, AuthorizedHttpFetcher
from detoura.models.market_prior_acquisition import (
    AuthorizationStatus, RateLimitPolicy, SourceRegistration, SourceType, StopReason,
)
from datetime import datetime, timezone

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)

#: A small, self-contained fake DNS map for this file's own SSRF tests -
#: deliberately not shared with tests/test_v9_phase25_network_safety.py
#: (which has its own, larger one) to keep each test file independently
#: readable, matching this project's existing one-file-one-fixture-set
#: convention (no test file imports fixtures from another).
_FAKE_DNS: dict[str, list[str]] = {
    "example.com": ["93.184.216.34"],
    "safe.example.com": ["93.184.216.34"],
    "loopback.example.com": ["127.0.0.1"],
    "loopback6.example.com": ["::1"],
    "private.example.com": ["10.1.2.3"],
    "linklocal.example.com": ["169.254.169.254"],
    "multihomed.example.com": ["93.184.216.34", "127.0.0.1"],
    "mapped-private.example.com": ["::ffff:10.1.2.3"],
    "rebinds.example.com": ["127.0.0.1"],
}


def _fake_resolver(host, port, *args, **kwargs):
    addrs = _FAKE_DNS.get(host)
    if not addrs:
        raise OSError(f"[fake resolver] no record for {host!r}")
    return [
        (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
        for a in addrs
    ]


def _reg(**kw) -> SourceRegistration:
    base = dict(source_id="web1", source_name="Web One", source_type=SourceType.AUTHORIZED_WEB_SOURCE,
                authorization_status=AuthorizationStatus.APPROVED, base_domain="example.com",
                rate_limit_policy=RateLimitPolicy(requests_per_minute=6000, min_delay_seconds=0),
                created_at=NOW, updated_at=NOW)
    base.update(kw)
    return SourceRegistration(**base)


def _fetcher(reg, client, **kw) -> AuthorizedHttpFetcher:
    kw.setdefault("resolver", _fake_resolver)
    return AuthorizedHttpFetcher(reg, client, **kw)


class _StubClient:
    def __init__(self, plan):
        self.plan = list(plan)
        self.calls: list[str] = []

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls.append(url)
        item = self.plan.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ======================================================================
# Group A - assert_safe_public_host, the core helper, in isolation
# ======================================================================
@pytest.mark.parametrize("literal", [
    "127.0.0.1", "127.255.255.255",  # IPv4 loopback
    "::1",  # IPv6 loopback
    "10.0.0.1", "172.16.5.5", "192.168.1.1",  # RFC1918 private
    "169.254.169.254",  # link-local / the cloud-metadata address class
    "fe80::1",  # IPv6 link-local
    "224.0.0.1",  # multicast
    "0.0.0.0",  # unspecified
    "::",  # IPv6 unspecified
    "::ffff:127.0.0.1",  # IPv4-mapped IPv6 wrapping loopback
    "::ffff:10.1.2.3",  # IPv4-mapped IPv6 wrapping a private address
    # V9 Phase 6 independent-review finding: RFC 6598 Carrier-Grade NAT
    # space - `ipaddress.IPv4Address.is_private` does NOT include this
    # range (verified directly), so a naive is_loopback/is_private/
    # is_link_local/... OR-chain misses it entirely. Some cloud/container
    # network fabrics route 100.64.0.0/10 internally specifically because
    # RFC1918-only filters miss it - a known real SSRF-filter-bypass class.
    "100.64.0.1", "100.100.100.1", "100.127.255.254",
    # The IPv6 NAT64 translation prefix - is_global is True for this
    # range but is_reserved is also True; it can embed an arbitrary IPv4
    # address in its low bits (another IPv6-notation SSRF-bypass class).
    "64:ff9b::a00:1",
])
def test_unsafe_ip_literals_are_rejected(literal):
    with pytest.raises(UnsafeHostError):
        assert_safe_public_host(literal)


def test_cgnat_range_boundary_is_public_just_outside_it():
    """100.63.255.255 and 100.128.0.0 are the addresses immediately outside
    100.64.0.0/10 on either side - confirms the fix is bounded to the real
    CGNAT range, not an overbroad "reject 100.*" that would also reject
    ordinary public 100.x addresses outside the RFC 6598 block."""
    assert_safe_public_host("100.63.255.255")  # does not raise
    assert_safe_public_host("100.128.0.0")  # does not raise


@pytest.mark.parametrize("literal", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"])
def test_safe_public_ip_literals_pass(literal):
    assert_safe_public_host(literal)  # does not raise


def test_hostname_resolving_to_a_safe_address_passes():
    def resolver(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    assert_safe_public_host("safe.example.test", resolver=resolver)


def test_hostname_resolving_to_loopback_is_rejected():
    def resolver(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    with pytest.raises(UnsafeHostError):
        assert_safe_public_host("attacker-controlled.example.test", resolver=resolver)


def test_hostname_with_one_bad_record_among_good_ones_is_rejected():
    """A DNS answer can carry several A/AAAA records; a connecting library
    may pick any of them - reject the whole host if even one is unsafe."""
    def resolver(host, port, *a, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    with pytest.raises(UnsafeHostError):
        assert_safe_public_host("multihomed.example.test", resolver=resolver)


def test_unresolvable_hostname_is_rejected_not_silently_allowed():
    def resolver(host, port, *a, **kw):
        raise OSError("nodename nor servname provided, or not known")

    with pytest.raises(UnsafeHostError):
        assert_safe_public_host("does-not-exist.example.test", resolver=resolver)


def test_numeric_ip_notation_bypassing_literal_detection_is_still_caught_via_resolution():
    """`ipaddress.ip_address()` (what the IP-literal fast-path uses) does
    NOT parse decimal/hex/octal IP notation (`2130706433`, `0x7f000001`,
    `017700000001` for 127.0.0.1) - a classic SSRF-filter-bypass trick
    against literal-only IP checks. This platform's own resolver DOES
    interpret them as 127.0.0.1 (verified directly - not a test assumption).
    assert_safe_public_host must still catch it, since it resolves through
    the SAME real resolver rather than special-casing "looks like an IP"."""
    real = socket.getaddrinfo
    resolved = real("2130706433", None)
    assert resolved[0][4][0] == "127.0.0.1", (
        "this test's premise (this platform's resolver treats a bare "
        "decimal integer as an IPv4 address) does not hold here - the "
        "numeric-notation bypass this test targets is not reproducible "
        "on this platform, so the assertion below would not be meaningful"
    )
    with pytest.raises(UnsafeHostError):
        assert_safe_public_host("2130706433")


# ======================================================================
# Group B - AuthorizedHttpFetcher end to end, real SSRF targets
# (V9 Phase 6, reachability note: this engine has no registered
# network-capable fetcher factory in production today - see
# services/bootstrap_registry.py's own module docstring, independently
# confirmed by grep - so this is a LOW/defense-in-depth closure of the
# exact path a future real AUTHORIZED_WEB_SOURCE will use, not a currently
# exploitable production gap. Still tested to the same standard, since a
# fresh adversarial pass must not skip "currently unreachable" code that a
# future slice will make reachable.)
# ======================================================================
@pytest.mark.parametrize("host,reason_contains", [
    ("loopback.example.com", "loopback"),
    ("loopback6.example.com", "loopback"),
    ("private.example.com", "private"),
    ("linklocal.example.com", "metadata"),  # 169.254.169.254
    ("multihomed.example.com", "multihomed"),
    ("mapped-private.example.com", "mapped"),
    ("rebinds.example.com", "rebind"),
])
def test_registered_source_pointing_at_an_unsafe_address_is_blocked(host, reason_contains):
    """The trivial, concrete exploit this fix closes: an Ops-registered
    source's `base_domain` is free text with no format validation (see
    api/ops_market_prior_acquisition.py's RegisterSourceRequest) - a typo,
    an insider, or a later-compromised/rebound DNS record for an
    already-authorized domain can point straight at loopback/private/
    link-local addresses. The OLD `_check_host` (hostname-string-only)
    would have let every one of these through; only the real resolved-IP
    check catches them."""
    assert host in _FAKE_DNS, f"test setup: {host!r} needs a _FAKE_DNS entry"
    fetcher = _fetcher(_reg(base_domain="example.com"), _StubClient([HttpResponse(200, "should never be reached")]))
    with pytest.raises(AccessBlocked) as exc:
        fetcher.fetch(f"https://{host}/x")
    assert exc.value.reason is StopReason.DOMAIN_NOT_ALLOWED
    assert fetcher.http_client.calls == []  # never actually connected


def test_registered_source_with_a_genuinely_safe_address_still_works():
    fetcher = _fetcher(_reg(base_domain="example.com"), _StubClient([HttpResponse(200, "fine")]))
    assert fetcher.fetch("https://safe.example.com/x").status == 200


def test_redirect_to_an_unsafe_resolved_address_is_blocked_even_within_the_allowed_domain():
    """Not just the first hop: a redirect target still allowed by the
    domain suffix (base_domain) but resolving unsafe must also be
    rejected - `_check_host` runs on every hop of the existing redirect
    loop, and now includes the IP-safety check on every one of those
    calls too."""
    stub = _StubClient([
        HttpResponse(302, "", headers={"Location": "https://loopback.example.com/steal"}),
    ])
    fetcher = _fetcher(_reg(base_domain="example.com"), stub)
    with pytest.raises(AccessBlocked) as exc:
        fetcher.fetch("https://safe.example.com/x")
    assert exc.value.reason is StopReason.DOMAIN_NOT_ALLOWED
    assert stub.calls == ["https://safe.example.com/x"]  # the redirect was never followed


# ======================================================================
# Group C - the Wikimedia destination-image path (Invariant 2/3/4)
# Reachability note: WikimediaCommonsClient.download/search_candidates are
# invoked only by scripts/acquire_destination_images.py, an operator-run
# offline CLI - not a live HTTP-reachable path (confirmed by grep: the live
# api/destination_images.py endpoints only ever read a pre-built static
# manifest, never call this client at request time).
# ======================================================================
def test_check_domain_rejects_an_allowlisted_hostname_resolving_unsafe():
    def resolver(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    with pytest.raises(DomainNotAllowed):
        _check_domain(
            "https://upload.wikimedia.org/rebound.jpg", "upload.wikimedia.org", resolver=resolver,
        )


def test_check_domain_allows_a_hostname_resolving_safely():
    def resolver(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    _check_domain("https://upload.wikimedia.org/x.jpg", "upload.wikimedia.org", resolver=resolver)  # no raise


def test_download_never_silently_follows_a_redirect_off_the_media_cdn(monkeypatch):
    """Before this fix, `download()` called the bare `urllib.request.urlopen`
    global function, whose default opener auto-follows a 3xx with no chance
    to re-validate the destination. Simulate the CDN issuing a redirect -
    the fix (a no-redirect opener) must surface it as a catchable error,
    never a silently-followed request to wherever the Location header
    points."""
    def resolver(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    class _RedirectingOpener:
        def open(self, request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found", {"Location": "http://169.254.169.254/steal"}, None,
            )

    monkeypatch.setattr(
        "detoura.services.destination_images.wikimedia.build_no_redirect_opener",
        lambda *a, **kw: _RedirectingOpener(),
    )
    client = WikimediaCommonsClient(resolver=resolver)
    with pytest.raises(ProviderHttpError):
        client.download("https://upload.wikimedia.org/x.jpg", max_bytes=1_000_000)


# ======================================================================
# Group D - Invariant 9: retry safety for non-idempotent Duffel operations
# (the headline finding of this slice - not SSRF, but explicitly in scope)
# ======================================================================
class _CountingFailingClient:
    """Fails every call with a retryable-looking condition, so if ANYTHING
    retries, `.calls` grows past 1."""

    def __init__(self, *, ok_after: int | None = None):
        self.calls = 0
        self.ok_after = ok_after

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls += 1
        if self.ok_after is not None and self.calls > self.ok_after:
            return HttpResponse(200, json.dumps({
                "data": {
                    "id": "ord_test", "live_mode": False,
                    "change_total_amount": "0", "change_total_currency": "EUR",
                },
            }))
        return HttpResponse(503, json.dumps({"errors": [{"code": "x", "title": "down"}]}))


def _duffel(client, **kw):
    from detoura.providers.duffel import DuffelTransportProvider

    return DuffelTransportProvider(
        access_token="duffel_test_abc", http_client=client, max_calls=40, **kw,
    )


def test_create_test_order_never_auto_retries_at_the_transport_layer():
    """The headline finding: `self.http` is always a RetryingHttpClient,
    which retries a POST exactly like a GET on any 5xx/timeout - Duffel
    gives no idempotency key for Order creation, so an automatic retry
    after a LOST RESPONSE (not necessarily a lost effect) can create a
    second real Order for the same leg. Order creation must make at most
    ONE raw HTTP attempt, ever - a failure surfaces once, as one typed
    exception, for the booking layer's own (already-hardened) uncertain-
    outcome handling to deal with, never silently retried underneath it."""
    from detoura.providers.duffel import DuffelOrderError

    provider = _duffel(_CountingFailingClient())
    # get_offer (a safe, retry-friendly read) is called first inside
    # create_test_order to re-prove the price - stub it out directly so
    # this test isolates the Order-creation POST specifically.
    provider.get_offer = lambda offer_id: {  # type: ignore[method-assign]
        "id": offer_id, "total_amount": "100.00", "total_currency": "EUR", "live_mode": False,
    }
    with pytest.raises(DuffelOrderError):
        provider.create_test_order(
            "off_0000AAAAAAAAAAAAAAAAAA", passengers=[{"id": "pas_0"}],
            expected_amount="100.00", expected_currency="EUR",
        )
    assert provider.http.inner.calls == 1, (
        "create_test_order must make exactly one raw HTTP attempt, never a "
        "transport-level retry of a real Order-creation POST"
    )


def test_confirm_order_cancellation_never_auto_retries():
    from detoura.providers.duffel import DuffelChangeError

    provider = _duffel(_CountingFailingClient())
    with pytest.raises(DuffelChangeError):
        provider.confirm_order_cancellation("ore_0000AAAAAAAAAAAA")
    assert provider.http.inner.calls == 1


def test_create_and_confirm_order_change_confirm_step_never_auto_retries():
    from detoura.providers.duffel import DuffelChangeError

    client = _CountingFailingClient(ok_after=1)  # the FIRST call (create) succeeds...
    provider = _duffel(client)
    with pytest.raises(DuffelChangeError):
        # ...but the SECOND call (confirm - the money-moving one) fails and
        # must not be transport-retried.
        provider.create_and_confirm_order_change("oco_0000AAAAAAAAAAAAAAAA")
    assert client.calls == 2, "create (1) + confirm (1, never retried) = exactly 2 raw attempts"


def test_read_only_offer_lookup_still_retries_normally():
    """The fix is scoped to genuinely irreversible mutations - a read
    (get_offer) must keep its existing, desirable retry-on-5xx behavior,
    proving the fix did not regress resilience for safe operations."""
    client = _CountingFailingClient(ok_after=2)
    provider = _duffel(client, max_offers=50)
    # get_offer's underlying RetryingHttpClient still owns real backoff
    # sleeps by default; keep the test fast without touching retry count.
    provider.http._sleep = lambda s: None
    offer = provider.get_offer("off_0000AAAAAAAAAAAAAAAAAA")
    assert offer["id"]
    assert client.calls == 3, "two failed attempts, then a retried success - retry is still active for reads"


def test_search_offer_request_still_retries_normally():
    client = _CountingFailingClient(ok_after=2)
    provider = _duffel(client)
    provider.http._sleep = lambda s: None
    # The point of this test is only that the retry loop itself still ran
    # 3 times for a safe, read-only Offer Request - not that a search
    # meaningfully succeeds against this minimal stub (its "success" body
    # is not a real offers payload, so this legitimately returns an empty
    # list rather than raising - "never raises on nothing found" is this
    # provider's own documented behavior).
    result = provider.fetch_offers("BER", "LHR", __import__("datetime").date(2026, 6, 1))
    assert result == []
    assert client.calls == 3, "two failed attempts, then a retried success - retry is still active for search"


# ======================================================================
# Group E - Invariant 10: UrllibHttpClient (Duffel/Amadeus/Stripe's
# shared transport) must not auto-follow a redirect and forward
# Authorization cross-host (independent-review Finding 2). A REAL local
# HTTP server, not a mock of urllib internals - the reviewer's own note
# was that the wikimedia redirect test monkeypatches the mechanism away
# rather than proving the real opener blocks a redirect; this closes that
# gap for the shared client directly.
# ======================================================================
def test_urllib_http_client_does_not_auto_follow_a_redirect_or_leak_the_header():
    import http.server
    import threading as _threading

    from detoura.providers.http import UrllibHttpClient

    captured_on_redirect_target: list[dict] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/secret")
                self.end_headers()
            elif self.path == "/secret":
                # If this is ever reached, the redirect was followed - and
                # crucially it would arrive WITH the original Authorization
                # header attached, which is exactly what must never happen.
                captured_on_redirect_target.append(dict(self.headers.items()))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"leaked")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):  # silence the test-run output
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = UrllibHttpClient()
        response = client.request(
            "GET", f"http://127.0.0.1:{server.server_port}/start",
            headers={"Authorization": "Bearer super-secret-token"},
        )
        # The redirect must come back AS a 3xx response, never silently
        # followed - and /secret must never have been reached at all.
        assert response.status == 302
        assert response.headers.get("Location", "").endswith("/secret")
        assert captured_on_redirect_target == [], (
            "the redirect target was reached - Authorization would have "
            "been forwarded cross-host, exactly the credential-exfiltration "
            "class this fix closes"
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
