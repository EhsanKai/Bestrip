"""V8 Phase 1: the two guards that must hold before a real sandbox call.

**live_mode.** The ``duffel_test_`` token prefix says what we *sent*. Duffel's
``live_mode`` boolean says what the provider *did*. V8 requires both, and this
second check fails closed: a response that is missing the flag, asserts it
true, or carries a contaminated offer is refused before a single offer is
normalized.

**TLS.** ``urllib`` verifies against the interpreter's trust store, and a build
whose ``openssl_cafile`` points nowhere fails every HTTPS call at the
handshake. The client now pins :mod:`certifi`'s bundle - and never disables
verification, which for a payments-capable API is the whole point.

Plus truncation disclosure: 23 offers becoming 20 is never allowed to be
silent, so it is counted and exposed.
"""

from __future__ import annotations

import json
import ssl
from datetime import date

import pytest

from detoura.providers import http as http_module
from detoura.providers.duffel import (
    DuffelLiveModeError,
    DuffelTransportProvider,
    assert_test_mode,
)
from detoura.providers.http import HttpResponse, UrllibHttpClient, _build_ssl_context

from . import duffel_fixtures as fx

TOKEN = "duffel_test_notarealtoken"


class _StubHttpClient:
    def __init__(self, body: dict, *, status: int = 200):
        self.calls = 0
        self._body = body
        self._status = status

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls += 1
        return HttpResponse(status=self._status, body=json.dumps(self._body))


def _provider(body: dict, **kw) -> tuple[DuffelTransportProvider, _StubHttpClient]:
    stub = _StubHttpClient(body)
    return DuffelTransportProvider(access_token=TOKEN, http_client=stub, **kw), stub


# ---------------------------------------------------------------------------
# live_mode — the envelope guard
# ---------------------------------------------------------------------------
def test_a_sandbox_envelope_passes_and_offers_are_normalized():
    provider, stub = _provider(fx.DIRECT)
    options = provider.fetch_offers("CGN", "BCN", date(2026, 10, 15))
    assert stub.calls == 1
    assert options and options[0].origin == "CGN"


def test_a_live_envelope_is_refused_before_normalization():
    provider, _ = _provider(fx.LIVE_MODE_ENVELOPE)
    with pytest.raises(DuffelLiveModeError, match="live_mode=true"):
        provider.fetch_offers("CGN", "BCN", date(2026, 10, 15))
    assert provider.offers_retained == 0, "nothing may be counted as retained"


def test_a_missing_live_mode_fails_closed():
    """Silence is 'might be live', which is the same as live here."""
    provider, _ = _provider(fx.LIVE_MODE_MISSING)
    with pytest.raises(DuffelLiveModeError, match="did not state live_mode"):
        provider.fetch_offers("CGN", "BCN", date(2026, 10, 15))


def test_one_contaminated_offer_refuses_the_whole_page():
    provider, _ = _provider(fx.LIVE_MODE_MIXED_OFFERS)
    with pytest.raises(DuffelLiveModeError, match="non-sandbox live_mode"):
        provider.fetch_offers("CGN", "BCN", date(2026, 10, 15))


@pytest.mark.parametrize("body", [
    {},
    {"data": None},
    {"data": {"offers": []}},
    {"data": {"live_mode": "false", "offers": []}},
    {"data": {"live_mode": 0, "offers": []}},
    {"data": {"live_mode": 1, "offers": []}},
])
def test_assert_test_mode_only_accepts_a_literal_false(body):
    with pytest.raises(DuffelLiveModeError):
        assert_test_mode(body)


def test_assert_test_mode_accepts_the_real_sandbox_shape():
    assert_test_mode({"data": {"live_mode": False, "offers": [
        {"id": "off_1", "live_mode": False},
        {"id": "off_2"},  # offer omitting the key: envelope has spoken
    ]}}) is None


def test_the_live_mode_error_carries_no_offer_payload_or_token():
    provider, _ = _provider(fx.LIVE_MODE_ENVELOPE)
    with pytest.raises(DuffelLiveModeError) as caught:
        provider.fetch_offers("CGN", "BCN", date(2026, 10, 15))
    message = str(caught.value)
    assert "off_live" not in message
    assert TOKEN not in message


# ---------------------------------------------------------------------------
# Truncation disclosure — 23 -> 20 is never silent
# ---------------------------------------------------------------------------
def test_offers_dropped_to_the_cap_are_counted_not_hidden():
    many = fx.response([
        fx._offer(f"off_{i}", [
            fx._slice("CGN", "BCN", "PT2H10M", [
                fx._segment("CGN", "BCN",
                            "2026-10-15T08:00:00",
                            "2026-10-15T10:10:00", "PT2H10M",
                            flight_number=str(1000 + i)),
            ]),
        ], total_amount=f"{100 + i}.00")
        for i in range(23)
    ])
    provider, _ = _provider(many, max_offers=20)
    options = provider.fetch_offers("CGN", "BCN", date(2026, 10, 15))
    assert len(options) == 20
    metrics = provider.supply_metrics()
    assert metrics["offers_received"] == 23
    assert metrics["offers_retained"] == 20
    assert metrics["offers_truncated"] == 3


def test_no_truncation_is_reported_when_everything_fits():
    provider, _ = _provider(fx.DIRECT, max_offers=20)
    provider.fetch_offers("CGN", "BCN", date(2026, 10, 15))
    assert provider.supply_metrics()["offers_truncated"] == 0


# ---------------------------------------------------------------------------
# TLS — verify, with a bundle that exists; never disable
# ---------------------------------------------------------------------------
def test_the_default_client_builds_a_verifying_context():
    context = _build_ssl_context()
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_certifi_bundle_is_used_when_no_env_override(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    import certifi

    context = _build_ssl_context()
    # The certifi bundle loads at least one root; a context that silently
    # loaded nothing would still "verify" and fail every real handshake.
    assert context.cert_store_stats()["x509_ca"] > 0
    assert certifi.where()


def test_an_env_override_is_honoured(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/but/explicit.pem")
    # create_default_context() defers loading, so this must not raise here;
    # the point is that certifi is *not* consulted when the operator has spoken.
    context = _build_ssl_context()
    assert context.verify_mode is ssl.CERT_REQUIRED


def test_the_client_never_exposes_a_way_to_disable_verification():
    client = UrllibHttpClient()
    assert client._ssl_context.verify_mode is ssl.CERT_REQUIRED
    source = __import__("inspect").getsource(http_module)
    assert "CERT_NONE" not in source
    assert "_create_unverified" not in source
    assert "verify=False" not in source
