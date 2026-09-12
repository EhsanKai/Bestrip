"""V9 Phase 4 §L/§M/§N/§O/§V - provider abstraction, sandbox reference
adapter, Stripe adapter (pure/HTTP-mocked only), webhook signature scheme,
sandbox/live guard."""

from __future__ import annotations

import json
import time

import pytest

from detoura.providers.http import HttpResponse
from detoura.providers.payment_provider import ProviderEventVerificationError
from detoura.providers.sandbox_payment import SandboxPaymentProvider
from detoura.providers.stripe_payment import (
    LIVE_KEY_PREFIX,
    TEST_KEY_PREFIX,
    StripeConfigurationError,
    StripePaymentProvider,
    is_live_key,
    is_test_key,
    redact_key,
    verify_webhook_signature,
)


# ======================================================================
# §V - sandbox/live guard, never leak the key
# ======================================================================
def test_is_test_key_and_is_live_key():
    assert is_test_key("sk_test_abc123")
    assert not is_test_key("sk_live_abc123")
    assert is_live_key("sk_live_abc123")
    assert not is_live_key("sk_test_abc123")
    assert not is_test_key(None)
    assert not is_test_key("")
    assert not is_test_key("garbage")


def test_redact_key_never_contains_the_real_secret():
    real = "sk_test_51ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    redacted = redact_key(real)
    assert real not in redacted
    assert real[8:] not in redacted
    assert redacted == "sk_test_<redacted>"
    assert redact_key("sk_live_secretvalue") == "sk_live_<redacted>"
    assert redact_key(None) == "<no key>"
    assert redact_key("nonsense") == "<unrecognised key format>"


def test_stripe_provider_refuses_a_live_key_by_default():
    with pytest.raises(StripeConfigurationError):
        StripePaymentProvider(secret_key="sk_live_abc123")


def test_stripe_provider_refuses_a_malformed_key():
    with pytest.raises(StripeConfigurationError):
        StripePaymentProvider(secret_key="not-a-real-key")


def test_stripe_provider_refuses_an_empty_key():
    with pytest.raises(StripeConfigurationError):
        StripePaymentProvider(secret_key="")


def test_stripe_provider_accepts_a_test_key():
    provider = StripePaymentProvider(secret_key="sk_test_abc123", http=_FakeHttp([]))
    assert provider.name == "stripe"


def test_stripe_provider_live_key_only_with_explicit_override():
    # Defence in depth: even the explicit override must be passed on
    # purpose, never inferred - a developer flipping one config flag
    # cannot silently make this succeed.
    provider = StripePaymentProvider(
        secret_key="sk_live_abc123", allow_non_test_key=True, http=_FakeHttp([]),
    )
    assert provider.name == "stripe"


def test_error_message_never_echoes_the_key_value():
    try:
        StripePaymentProvider(secret_key="sk_live_supersecretvalue12345")
    except StripeConfigurationError as error:
        assert "supersecretvalue12345" not in str(error)
    else:
        pytest.fail("expected StripeConfigurationError")


# ======================================================================
# §O - Stripe webhook signature scheme
# ======================================================================
def _sign(secret: str, payload: bytes, ts: int) -> str:
    import hashlib
    import hmac

    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_webhook_signature_valid_passes():
    secret = "whsec_test123"
    payload = b'{"id": "evt_1", "type": "payment_intent.succeeded"}'
    ts = int(time.time())
    header = _sign(secret, payload, ts)
    verify_webhook_signature(payload=payload, signature_header=header, webhook_secret=secret, now=float(ts))


def test_webhook_signature_tampered_payload_rejected():
    secret = "whsec_test123"
    payload = b'{"id": "evt_1", "type": "payment_intent.succeeded"}'
    ts = int(time.time())
    header = _sign(secret, payload, ts)
    tampered = payload.replace(b"succeeded", b"canceled_")  # forged amount/status
    with pytest.raises(ProviderEventVerificationError):
        verify_webhook_signature(payload=tampered, signature_header=header, webhook_secret=secret, now=float(ts))


def test_webhook_signature_wrong_secret_rejected():
    payload = b'{"id": "evt_1", "type": "payment_intent.succeeded"}'
    ts = int(time.time())
    header = _sign("whsec_correct", payload, ts)
    with pytest.raises(ProviderEventVerificationError):
        verify_webhook_signature(payload=payload, signature_header=header, webhook_secret="whsec_wrong", now=float(ts))


def test_webhook_signature_missing_timestamp_rejected():
    with pytest.raises(ProviderEventVerificationError):
        verify_webhook_signature(payload=b"{}", signature_header="v1=deadbeef", webhook_secret="whsec_x")


def test_webhook_signature_missing_v1_rejected():
    with pytest.raises(ProviderEventVerificationError):
        verify_webhook_signature(payload=b"{}", signature_header="t=123456", webhook_secret="whsec_x")


def test_webhook_signature_replay_outside_tolerance_rejected():
    secret = "whsec_test123"
    payload = b'{"id": "evt_1", "type": "x"}'
    old_ts = int(time.time()) - 10_000  # far outside the default 300s window
    header = _sign(secret, payload, old_ts)
    with pytest.raises(ProviderEventVerificationError):
        verify_webhook_signature(payload=payload, signature_header=header, webhook_secret=secret)


def test_webhook_signature_rotation_accepts_either_v1():
    """Stripe may send multiple v1= values during secret rotation - any
    matching one is accepted."""
    secret = "whsec_new"
    payload = b'{"id": "evt_1", "type": "x"}'
    ts = int(time.time())
    good = _sign(secret, payload, ts).split(",")[1]  # "v1=<hex>"
    header = f"t={ts},v1=deadbeefdeadbeef,{good}"
    verify_webhook_signature(payload=payload, signature_header=header, webhook_secret=secret, now=float(ts))


# ======================================================================
# §L/§M - Stripe adapter's pure result-mapping (HTTP-mocked, per module's
# own honesty note: never exercised against Stripe's real servers)
# ======================================================================
class _FakeHttp:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if not self._responses:
            raise TimeoutError("fake http exhausted")
        return self._responses.pop(0)


def _intent_response(status_code: int, **fields) -> HttpResponse:
    return HttpResponse(status=status_code, body=json.dumps(fields))


def test_stripe_authorize_maps_requires_capture_to_ok_authorized():
    http = _FakeHttp([_intent_response(200, id="pi_1", status="requires_capture", amount_capturable=5000)])
    provider = StripePaymentProvider(secret_key="sk_test_x", http=http)
    result = provider.authorize(idempotency_key="k1", amount=50.0, currency="EUR", reference="pay_1")
    assert result.ok
    assert not result.unknown
    assert result.provider_reference == "pi_1"
    assert result.authorized_amount == 50.0


def test_stripe_authorize_maps_requires_action_without_treating_as_failure_label():
    http = _FakeHttp([_intent_response(200, id="pi_2", status="requires_action")])
    provider = StripePaymentProvider(secret_key="sk_test_x", http=http)
    result = provider.authorize(idempotency_key="k2", amount=50.0, currency="EUR", reference="pay_2")
    assert not result.ok
    assert not result.unknown
    assert result.status == "requires_action"


def test_stripe_authorize_network_timeout_is_unknown_never_failed():
    http = _FakeHttp([])  # first call raises TimeoutError
    provider = StripePaymentProvider(secret_key="sk_test_x", http=http)
    result = provider.authorize(idempotency_key="k3", amount=50.0, currency="EUR", reference="pay_3")
    assert result.unknown
    assert not result.ok


def test_stripe_declined_card_maps_to_clean_failure():
    http = _FakeHttp([HttpResponse(status=402, body=json.dumps(
        {"error": {"code": "card_declined", "message": "Your card was declined."}}
    ))])
    provider = StripePaymentProvider(secret_key="sk_test_x", http=http)
    result = provider.authorize(idempotency_key="k4", amount=50.0, currency="EUR", reference="pay_4")
    assert not result.ok
    assert not result.unknown
    assert result.status == "card_declined"


def test_stripe_idempotency_key_is_sent_on_every_mutating_call():
    http = _FakeHttp([_intent_response(200, id="pi_5", status="requires_capture", amount_capturable=1000)])
    provider = StripePaymentProvider(secret_key="sk_test_x", http=http)
    provider.authorize(idempotency_key="idem-key-xyz", amount=10.0, currency="EUR", reference="pay_5")
    assert http.calls[0]["headers"]["Idempotency-Key"] == "idem-key-xyz"


def test_stripe_never_sends_raw_card_data_fields():
    """§M: the backend must stay outside direct card-data handling - assert
    the request body never contains card-shaped field names."""
    http = _FakeHttp([_intent_response(200, id="pi_6", status="requires_capture", amount_capturable=1000)])
    provider = StripePaymentProvider(secret_key="sk_test_x", http=http)
    provider.authorize(idempotency_key="k6", amount=10.0, currency="EUR", reference="pay_6")
    body = http.calls[0]["body"]
    for forbidden in ("card_number", "cvc", "number=", "cvv"):
        assert forbidden not in body


# ======================================================================
# §L/§V - the sandbox reference adapter's own contract
# ======================================================================
def test_sandbox_name_is_never_stripe():
    assert SandboxPaymentProvider().name == "sandbox"


def test_sandbox_capabilities_declared_honestly():
    caps = SandboxPaymentProvider().capabilities()
    assert caps.supports_manual_capture
    assert caps.supports_webhooks
    assert "bnpl" in caps.unsupported_payment_methods


def test_sandbox_idempotency_cache_returns_identical_result_object_semantics():
    provider = SandboxPaymentProvider()
    r1 = provider.authorize(idempotency_key="same-key", amount=10.0, currency="EUR", reference="ref_a")
    r2 = provider.authorize(idempotency_key="same-key", amount=999.0, currency="USD", reference="ref_b")
    # Second call is a pure cache hit on the idempotency key - it must not
    # re-run fault injection/compute logic for a totally different reference.
    assert r1.provider_reference == r2.provider_reference
    assert r1.authorized_amount == r2.authorized_amount == 10.0


def test_sandbox_fail_auth_suffix():
    provider = SandboxPaymentProvider()
    result = provider.authorize(idempotency_key="k1", amount=10.0, currency="EUR", reference="pay_x_FAIL_AUTH")
    assert not result.ok and not result.unknown


def test_sandbox_requires_action_suffix():
    provider = SandboxPaymentProvider()
    result = provider.authorize(idempotency_key="k2", amount=10.0, currency="EUR", reference="pay_x_REQUIRES_ACTION")
    assert result.status == "requires_action"
    assert not result.ok and not result.unknown


def test_sandbox_fail_capture_suffix():
    provider = SandboxPaymentProvider()
    auth = provider.authorize(idempotency_key="k3", amount=10.0, currency="EUR", reference="pay_x_FAIL_CAPTURE")
    result = provider.capture(idempotency_key="k3c", provider_reference=auth.provider_reference)
    assert not result.ok and not result.unknown


def test_sandbox_fail_cancel_suffix():
    provider = SandboxPaymentProvider()
    auth = provider.authorize(idempotency_key="k4", amount=10.0, currency="EUR", reference="pay_x_FAIL_CANCEL")
    result = provider.cancel_authorization(idempotency_key="k4c", provider_reference=auth.provider_reference)
    assert not result.ok and not result.unknown


def test_sandbox_cannot_cancel_an_already_captured_authorization():
    provider = SandboxPaymentProvider()
    auth = provider.authorize(idempotency_key="k5", amount=10.0, currency="EUR", reference="pay_x")
    provider.capture(idempotency_key="k5c", provider_reference=auth.provider_reference)
    result = provider.cancel_authorization(idempotency_key="k5x", provider_reference=auth.provider_reference)
    assert not result.ok
    assert result.status == "already_captured"


def test_sandbox_refund_cannot_exceed_captured_amount_at_the_provider_itself():
    provider = SandboxPaymentProvider()
    auth = provider.authorize(idempotency_key="k6", amount=10.0, currency="EUR", reference="pay_x")
    provider.capture(idempotency_key="k6c", provider_reference=auth.provider_reference)
    result = provider.refund(idempotency_key="k6r", provider_reference=auth.provider_reference, amount=10.01)
    assert not result.ok


def test_sandbox_verify_event_rejects_bad_signature():
    provider = SandboxPaymentProvider()
    payload = json.dumps({"id": "evt_1", "type": "x"}).encode()
    with pytest.raises(ProviderEventVerificationError):
        provider.verify_event(payload=payload, signature="not-the-real-hash")


def test_sandbox_verify_event_accepts_correct_signature():
    provider = SandboxPaymentProvider()
    payload = json.dumps({"id": "evt_1", "type": "payment.captured", "provider_reference": "sbx_ref_1"}).encode()
    signature = SandboxPaymentProvider.sign(payload)
    event = provider.verify_event(payload=payload, signature=signature)
    assert event.provider_event_id == "evt_1"
    assert event.event_type == "payment.captured"


def test_sandbox_verify_event_rejects_missing_id_or_type():
    provider = SandboxPaymentProvider()
    payload = json.dumps({"id": "evt_1"}).encode()  # no "type"
    signature = SandboxPaymentProvider.sign(payload)
    with pytest.raises(ProviderEventVerificationError):
        provider.verify_event(payload=payload, signature=signature)
