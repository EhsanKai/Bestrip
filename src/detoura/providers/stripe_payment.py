"""Stripe adapter (V9 Phase 4 §L/§M).

**Provider decision** (documented per §M): Stripe is the selected target
for Detoura's EU/Germany architecture - broad card + SEPA support, native
SCA/3DS handling via PaymentIntents, manual-capture authorization
(``capture_method=manual``), a mature refunds API, signed webhooks, a
first-class test/live key distinction, and a well-documented REST API this
codebase's existing ``providers/http.py`` transport already fits without a
vendored SDK. This is an engineering judgement, not a legal one - see
``docs/V9_PHASE4_PAYMENT_ARCHITECTURE.md`` for the compliance items a
specialist must still confirm before real money moves.

**Honesty about what has and has not been verified**: this environment has
no live Stripe test API key. This adapter is written faithfully against
Stripe's publicly documented REST API (PaymentIntents, Refunds, webhook
signature scheme) and is exercised by this project's own tests only through
its pure helper functions (amount formatting, signature verification,
capability description) - never against Stripe's real servers. The full
authorize/capture/refund flow this project's test suite actually runs
end-to-end is :mod:`detoura.providers.sandbox_payment`. If a real
``STRIPE_SECRET_KEY`` (test mode) is ever supplied, this adapter is what
runs - but that has not happened in this sandbox, and the final Phase 4
report says so plainly rather than claiming a live-verified integration.

Never stores or logs a raw card number, CVC, or full payment credential -
Stripe's own PaymentIntents/Payment Methods hold that; this adapter only
ever sends amounts, currency and a payment_method reference the client
obtained from Stripe.js/Elements (never handled by this backend).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from ..observability import log_event
from ..observability import metrics as _metrics
from .http import HttpClient, HttpResponse, ProviderHttpError, _classify_status

_logger = logging.getLogger(__name__)
from .payment_provider import (
    ProviderCapabilities,
    ProviderEvent,
    ProviderEventVerificationError,
    ProviderResult,
)

TEST_KEY_PREFIX = "sk_test_"
LIVE_KEY_PREFIX = "sk_live_"
API_BASE = "https://api.stripe.com/v1"
API_VERSION = "2024-06-20"


class StripeConfigurationError(RuntimeError):
    """The adapter refuses to run as configured. Never carries the key
    itself - only what shape it expected."""


def is_test_key(key: str | None) -> bool:
    return bool(key) and key.startswith(TEST_KEY_PREFIX)


def is_live_key(key: str | None) -> bool:
    return bool(key) and key.startswith(LIVE_KEY_PREFIX)


def redact_key(key: str | None) -> str:
    """Safe to log/print - never the real key (§V: "startup/preflight
    diagnostics must not print secret values")."""
    if not key:
        return "<no key>"
    if is_test_key(key):
        return "sk_test_<redacted>"
    if is_live_key(key):
        return "sk_live_<redacted>"
    return "<unrecognised key format>"


def _to_stripe_minor(amount: float) -> int:
    from ..models.money import to_minor_units

    return to_minor_units(amount)


def _from_stripe_minor(minor: int) -> float:
    from ..models.money import from_minor_units

    return from_minor_units(minor)


def verify_webhook_signature(
    *, payload: bytes, signature_header: str, webhook_secret: str, tolerance_seconds: int = 300,
    now: float | None = None,
) -> None:
    """Stripe's documented scheme: the ``Stripe-Signature`` header is
    ``t=<timestamp>,v1=<hex hmac>[,v1=<hex hmac>...]``; the signed payload is
    ``f"{timestamp}.{payload}"``, HMAC-SHA256 with the webhook signing
    secret. Raises :class:`ProviderEventVerificationError` on any mismatch,
    missing field, or a timestamp too far from ``now`` (replay defence).
    """
    parts = dict(
        item.split("=", 1) for item in signature_header.split(",") if "=" in item
    )
    timestamp = parts.get("t")
    if timestamp is None:
        raise ProviderEventVerificationError("stripe: signature header missing timestamp")
    # Collect every v1= value (Stripe may send more than one during secret rotation).
    v1_signatures = [
        item.split("=", 1)[1] for item in signature_header.split(",")
        if item.strip().startswith("v1=")
    ]
    if not v1_signatures:
        raise ProviderEventVerificationError("stripe: signature header missing v1 signature")
    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(
        webhook_secret.encode("utf-8"), signed_payload, hashlib.sha256,
    ).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in v1_signatures):
        raise ProviderEventVerificationError("stripe: signature does not match payload")
    now = now if now is not None else time.time()
    try:
        ts = int(timestamp)
    except ValueError as error:
        raise ProviderEventVerificationError("stripe: malformed timestamp") from error
    if abs(now - ts) > tolerance_seconds:
        raise ProviderEventVerificationError("stripe: signature timestamp outside tolerance (possible replay)")


@dataclass(slots=True)
class StripePaymentProvider:
    """A real Stripe adapter over the documented REST API.

    Fails closed at construction if the key is not clearly a test key -
    the same "might be live is the same as live" posture
    ``providers/duffel.py`` already established for Duffel test tokens.
    """

    secret_key: str
    webhook_secret: str = ""
    http: HttpClient | None = None
    allow_non_test_key: bool = False
    name: str = "stripe"

    def __post_init__(self) -> None:
        if not self.allow_non_test_key and not is_test_key(self.secret_key):
            raise StripeConfigurationError(
                "refusing to start: STRIPE_SECRET_KEY does not look like a test "
                f"key (expected a {TEST_KEY_PREFIX!r} prefix); refusing to send "
                "any request. Use a sandbox/test-mode key, or pass "
                "allow_non_test_key=True deliberately."
            )
        if self.http is None:
            from .http import UrllibHttpClient

            self.http = UrllibHttpClient()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_manual_capture=True,
            supports_partial_capture=True,
            supports_partial_refund=True,
            supports_customer_action=True,
            supports_webhooks=True,
            supports_idempotency_keys=True,
            unsupported_payment_methods=("bnpl", "crypto"),
        )

    # ------------------------------------------------------------------
    def _headers(self, *, idempotency_key: str | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Stripe-Version": API_VERSION,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _post(self, path: str, *, idempotency_key: str, data: dict) -> HttpResponse | None:
        assert self.http is not None
        body = urlencode({k: v for k, v in data.items() if v is not None})
        start = time.monotonic()
        try:
            response = self.http.request(
                "POST", f"{API_BASE}{path}", headers=self._headers(idempotency_key=idempotency_key),
                body=body,
            )
        except (TimeoutError, OSError):
            self._log_call(path, outcome="timeout", start=start)
            return None  # unknown outcome - the caller must treat this as UNKNOWN, never as failure
        self._log_call(path, outcome="ok" if response.ok else _classify_status(response.status), start=start, status=response.status)
        return response

    def _get(self, path: str) -> HttpResponse | None:
        assert self.http is not None
        start = time.monotonic()
        try:
            response = self.http.request("GET", f"{API_BASE}{path}", headers=self._headers())
        except (TimeoutError, OSError):
            self._log_call(path, outcome="timeout", start=start)
            return None
        self._log_call(path, outcome="ok" if response.ok else _classify_status(response.status), start=start, status=response.status)
        return response

    def _log_call(self, path: str, *, outcome: str, start: float, status: int | None = None) -> None:
        """Stripe call observability (V9 Limited Beta observability contract
        §9): status/timing/outcome only - never the request body (form-
        encoded amount + idempotency key), the ``Authorization`` header, or
        any response payload."""
        duration_ms = round((time.monotonic() - start) * 1000, 2)
        operation = path.strip("/").split("/")[0] or "root"
        _metrics.observe_provider_call(
            provider="stripe", operation=operation, outcome=outcome, duration_ms=duration_ms,
        )
        log_event(
            _logger, "provider_request_completed",
            provider="stripe", operation=operation, outcome=outcome, status=status,
            duration_ms=duration_ms,
        )

    # ------------------------------------------------------------------
    def authorize(
        self, *, idempotency_key: str, amount: float, currency: str, reference: str,
    ) -> ProviderResult:
        resp = self._post(
            "/payment_intents", idempotency_key=idempotency_key,
            data={
                "amount": str(_to_stripe_minor(amount)),
                "currency": currency.lower(),
                "capture_method": "manual",
                "confirm": "true",
                "metadata[detoura_reference]": reference,
                "automatic_payment_methods[enabled]": "true",
                "automatic_payment_methods[allow_redirects]": "never",
            },
        )
        if resp is None:
            return ProviderResult(ok=False, unknown=True, provider_reference=None, status="unknown",
                                  detail="stripe: authorize request timed out or connection failed")
        return self._parse_intent_result(resp)

    def capture(
        self, *, idempotency_key: str, provider_reference: str, amount: float | None = None,
    ) -> ProviderResult:
        data = {}
        if amount is not None:
            data["amount_to_capture"] = str(_to_stripe_minor(amount))
        resp = self._post(
            f"/payment_intents/{provider_reference}/capture",
            idempotency_key=idempotency_key, data=data,
        )
        if resp is None:
            return ProviderResult(ok=False, unknown=True, provider_reference=provider_reference,
                                  status="unknown", detail="stripe: capture request timed out")
        return self._parse_intent_result(resp)

    def cancel_authorization(self, *, idempotency_key: str, provider_reference: str) -> ProviderResult:
        resp = self._post(
            f"/payment_intents/{provider_reference}/cancel",
            idempotency_key=idempotency_key, data={},
        )
        if resp is None:
            return ProviderResult(ok=False, unknown=True, provider_reference=provider_reference,
                                  status="unknown", detail="stripe: cancel request timed out")
        return self._parse_intent_result(resp)

    def refund(
        self, *, idempotency_key: str, provider_reference: str, amount: float, reason: str = "",
    ) -> ProviderResult:
        resp = self._post(
            "/refunds", idempotency_key=idempotency_key,
            data={
                "payment_intent": provider_reference,
                "amount": str(_to_stripe_minor(amount)),
                "metadata[reason]": reason[:200],
            },
        )
        if resp is None:
            return ProviderResult(ok=False, unknown=True, provider_reference=provider_reference,
                                  status="unknown", detail="stripe: refund request timed out")
        try:
            body = resp.json()
        except ProviderHttpError as error:
            return ProviderResult(ok=False, unknown=True, provider_reference=provider_reference,
                                  status="unknown", detail=str(error))
        if not resp.ok:
            return ProviderResult(
                ok=False, unknown=False, provider_reference=provider_reference,
                status=body.get("error", {}).get("code", "failed"),
                detail=str(body.get("error", {}).get("message", "refund failed"))[:300],
            )
        return ProviderResult(
            ok=True, unknown=False, provider_reference=body.get("payment_intent") or provider_reference,
            status=body.get("status", "succeeded"),
            refunded_amount=_from_stripe_minor(int(body.get("amount", 0))),
            raw_status_only={"livemode": body.get("livemode")},
        )

    def retrieve(self, *, provider_reference: str) -> ProviderResult:
        resp = self._get(f"/payment_intents/{provider_reference}")
        if resp is None:
            return ProviderResult(ok=False, unknown=True, provider_reference=provider_reference,
                                  status="unknown", detail="stripe: retrieve request timed out")
        return self._parse_intent_result(resp)

    def _parse_intent_result(self, resp: HttpResponse) -> ProviderResult:
        try:
            body = resp.json()
        except ProviderHttpError as error:
            return ProviderResult(ok=False, unknown=True, provider_reference=None,
                                  status="unknown", detail=str(error))
        if not resp.ok:
            err = body.get("error", {})
            return ProviderResult(
                ok=False, unknown=False, provider_reference=body.get("id"),
                status=err.get("code", "failed"),
                detail=str(err.get("message", "request failed"))[:300],
            )
        status = body.get("status", "")
        ref = body.get("id")
        amount_capturable = body.get("amount_capturable")
        amount_received = body.get("amount_received")
        ok = status in ("requires_capture", "succeeded")
        return ProviderResult(
            ok=ok, unknown=False, provider_reference=ref, status=status,
            authorized_amount=(
                _from_stripe_minor(int(amount_capturable)) if amount_capturable is not None else None
            ),
            captured_amount=(
                _from_stripe_minor(int(amount_received)) if amount_received else None
            ),
            detail="" if ok else f"stripe: unexpected status {status!r}",
            raw_status_only={"livemode": body.get("livemode")},
        )

    def verify_event(self, *, payload: bytes, signature: str) -> ProviderEvent:
        if not self.webhook_secret:
            raise ProviderEventVerificationError(
                "stripe: no webhook signing secret configured; refusing to trust an unverifiable event"
            )
        verify_webhook_signature(
            payload=payload, signature_header=signature, webhook_secret=self.webhook_secret,
        )
        try:
            body = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ProviderEventVerificationError(f"stripe: unparseable payload ({error})") from error
        event_id = body.get("id")
        event_type = body.get("type")
        if not event_id or not event_type:
            raise ProviderEventVerificationError("stripe: event missing id/type")
        data_object = (body.get("data") or {}).get("object") or {}
        return ProviderEvent(
            provider_event_id=str(event_id), event_type=str(event_type),
            provider_reference=data_object.get("id"),
            status=str(data_object.get("status", "")),
            payload={"livemode": body.get("livemode"), "object_id": data_object.get("id")},
        )
