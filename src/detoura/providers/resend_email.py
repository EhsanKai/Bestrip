"""Resend production email adapter (V9 Production Transactional Email).

**Provider decision**: Resend is the selected first production transactional
email provider - a documented REST API, Bearer-token auth, and native
``Idempotency-Key`` header support that maps directly onto this codebase's
existing per-attempt idempotency keys (see
``services/communication_service.py::_attempt_provider_key``). This adapter
implements the same :class:`~detoura.providers.communication_provider.CommunicationProvider`
contract the sandbox does - nothing in ``communication_service.py`` or any
caller changes; only :func:`detoura.communication_config.resolve_communication_provider`
knows this class exists.

**Honesty about what has and has not been verified**: this environment has
no live Resend API key. This adapter is written against Resend's publicly
documented REST API (``POST /emails``, ``GET /emails/{id}``, Bearer auth,
``Idempotency-Key`` header, documented error status codes and rate-limit
headers) and is exercised by this project's test suite only through a fake
:class:`~detoura.providers.http.HttpClient` - never against Resend's real
servers. If a real ``RESEND_API_KEY`` is ever supplied and
``COMMUNICATION_PROVIDER=resend`` with ``COMMUNICATION_LIVE_SENDING_ENABLED=true``,
this adapter is what runs - but that has not happened in this sandbox; see
``docs/V9_PRODUCTION_TRANSACTIONAL_EMAIL_REPORT.md``.

**Never stores or logs the API key.** It is read once at construction
(``communication_config.py``), sent only as the ``Authorization: Bearer``
header on requests to the fixed, hardcoded ``https://api.resend.com`` host -
never a customer- or config-controlled destination - and never appears in a
log line or an exception message (mirrors ``stripe_payment.py``'s
``redact_key`` discipline exactly).

**Ambiguous-outcome classification** (the part this module has to get
right, per the communication truth invariants):

* A connection-level failure (timeout, dropped connection - no HTTP response
  at all) is always UNKNOWN. There is no way to tell whether Resend received
  the request.
* A definitive 2xx with a parseable ``{"id": ...}`` body is SENT.
* A 2xx we cannot parse, or with no ``id``, is treated as UNKNOWN, not
  success - "the provider returned a malformed/ambiguous response" is
  explicitly called out as an UNKNOWN case, never assumed to have worked.
* ``400``/``401``/``403``/``404``/``405``/``422``/``429`` are all responses
  Resend actually returned to us - i.e. the provider was reached and
  explicitly said "no" before doing anything - so these are definitive
  FAILED outcomes, safe for an Ops/explicit resend later (never auto-retried
  by this adapter itself; see ``communication_service.py``'s existing
  resend/reconciliation architecture, which this module does not change).
* ``409`` (Resend's idempotency-key-conflict status) and any ``5xx`` are
  **not** treated as definitive failure: a 5xx can occur after a request was
  already queued/accepted, and Resend's documented API does not expose a
  way to look up "what happened to the request behind idempotency key X"
  independently of the message id (which a truly ambiguous call never
  received). These stay UNKNOWN, exactly as the audit brief requires
  ("if Resend does not provide sufficient reconciliation for a particular
  ambiguous case, preserve UNKNOWN for Ops/manual resolution - do not
  guess"). This is a real, documented limitation, not an oversight: unlike
  the in-process sandbox (which can privately record a message even on its
  own scripted "unknown" outcome), a real ambiguous failure before any
  response gives this adapter no id to reconcile against at all.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from ..observability import log_event
from ..observability import metrics as _metrics
from .communication_provider import (
    CommunicationProviderCapabilities,
    CommunicationSendResult,
)
from .http import HttpClient, HttpResponse, ProviderHttpError

_logger = logging.getLogger(__name__)

API_BASE = "https://api.resend.com"
API_KEY_PREFIX = "re_"

#: Statuses Resend documents as definitive request-level rejections - the
#: provider was reached and explicitly declined, before anything was sent.
_DEFINITIVE_REJECTION_STATUSES = frozenset({400, 401, 403, 404, 405, 422, 429})

_STATUS_NAMES = {
    400: "invalid_request", 401: "unauthorized", 403: "forbidden",
    404: "not_found", 405: "method_not_allowed", 422: "validation_error",
    429: "rate_limited",
}

#: ``GET /emails/{id}``'s ``last_event`` values that mean "Resend actually
#: sent this" - including post-send delivery signals (bounced/complained
#: still mean the SEND itself succeeded; CommunicationStatus.SENT models
#: send-transaction truth, not final mailbox delivery - see
#: ``models/communication.py``'s own docstring).
_SENT_EVENTS = frozenset({
    "sent", "delivered", "delivery_delayed", "bounced", "complained",
    "opened", "clicked",
})


class ResendConfigurationError(RuntimeError):
    """The adapter refuses to run as configured. Never carries the key
    itself - only what shape it expected (mirrors
    ``stripe_payment.py::StripeConfigurationError``)."""


def redact_key(key: str | None) -> str:
    """Safe to log/print - never the real key."""
    if not key:
        return "<no key>"
    if key.startswith(API_KEY_PREFIX):
        return f"{API_KEY_PREFIX}<redacted>"
    return "<unrecognised key format>"


def _classify_status(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if 400 <= status < 500:
        return "client_error"
    if 500 <= status < 600:
        return "server_error"
    return "unexpected_status"


@dataclass(slots=True)
class ResendEmailProvider:
    """A real Resend adapter over the documented REST API.

    Fails closed at construction if the key does not even look like a
    Resend key, or the sender address is missing - the same
    "obviously-wrong configuration must not construct a half-working
    adapter" posture ``StripePaymentProvider`` already established.
    """

    api_key: str
    from_email: str
    from_name: str = "Detoura"
    reply_to: str = ""
    http: HttpClient | None = None
    name: str = "resend"

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_key.startswith(API_KEY_PREFIX):
            raise ResendConfigurationError(
                "refusing to start: RESEND_API_KEY is missing or does not look "
                f"like a Resend key (expected a {API_KEY_PREFIX!r} prefix); "
                "refusing to send any request."
            )
        if not self.from_email or "@" not in self.from_email:
            raise ResendConfigurationError(
                "refusing to start: RESEND_FROM_EMAIL is missing or not a "
                "valid-looking address."
            )
        if self.http is None:
            from .http import UrllibHttpClient

            self.http = UrllibHttpClient()

    def capabilities(self) -> CommunicationProviderCapabilities:
        return CommunicationProviderCapabilities(
            supports_delivery_events=True,
            # Resend's webhooks exist but are not wired in this slice - see
            # the production email report's "remaining gaps".
            supports_idempotency_keys=True,
            max_retries_recommended=2,
        )

    # ------------------------------------------------------------------
    def _sender(self) -> str:
        return f"{self.from_name} <{self.from_email}>" if self.from_name else self.from_email

    def _headers(self, *, idempotency_key: str) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }

    def _log(self, operation: str, *, outcome: str, start: float, status: int | None = None) -> None:
        """Resend call observability: status/timing/outcome only - never the
        request body (recipient, subject, message text), the
        ``Authorization`` header, or any response payload (mirrors
        ``stripe_payment.py::_log_call`` exactly)."""
        duration_ms = round((time.monotonic() - start) * 1000, 2)
        _metrics.observe_provider_call(
            provider="resend", operation=operation, outcome=outcome, duration_ms=duration_ms,
        )
        log_event(
            _logger, "provider_request_completed",
            provider="resend", operation=operation, outcome=outcome, status=status,
            duration_ms=duration_ms,
        )

    def _error_detail(self, response: HttpResponse) -> str:
        try:
            body = response.json()
        except ProviderHttpError:
            return f"status {response.status}"
        message = body.get("message") or body.get("name") or f"status {response.status}"
        return str(message)[:200]

    # ------------------------------------------------------------------
    def send(
        self, *, idempotency_key: str, recipient: str, subject: str, body_text: str, reference: str,
    ) -> CommunicationSendResult:
        assert self.http is not None
        payload = json.dumps({
            "from": self._sender(),
            "to": [recipient],
            "subject": subject,
            "text": body_text,
            "headers": {"X-Detoura-Reference": reference},
            **({"reply_to": self.reply_to} if self.reply_to else {}),
        })
        start = time.monotonic()
        try:
            response = self.http.request(
                "POST", f"{API_BASE}/emails",
                headers=self._headers(idempotency_key=idempotency_key), body=payload,
            )
        except (ProviderHttpError, TimeoutError, OSError):
            # No HTTP response was ever received - a genuine connection-level
            # ambiguity. Never assumed FAILED (module docstring).
            self._log("send", outcome="timeout", start=start)
            return CommunicationSendResult(
                ok=False, unknown=True, provider_message_id=None, status="unknown",
                detail="resend: send request timed out or connection failed",
            )
        return self._parse_send_response(response, start=start)

    def _parse_send_response(self, response: HttpResponse, *, start: float) -> CommunicationSendResult:
        outcome = "ok" if response.ok else _classify_status(response.status)
        self._log("send", outcome=outcome, start=start, status=response.status)

        if response.ok:
            try:
                body = response.json()
            except ProviderHttpError:
                return CommunicationSendResult(
                    ok=False, unknown=True, provider_message_id=None, status="unknown",
                    detail="resend: 2xx response body was not valid JSON",
                )
            message_id = body.get("id")
            if not message_id:
                return CommunicationSendResult(
                    ok=False, unknown=True, provider_message_id=None, status="unknown",
                    detail="resend: 2xx response missing message id",
                )
            return CommunicationSendResult(
                ok=True, unknown=False, provider_message_id=str(message_id), status="success",
                detail="resend: message accepted",
            )

        if response.status in _DEFINITIVE_REJECTION_STATUSES:
            return CommunicationSendResult(
                ok=False, unknown=False, provider_message_id=None,
                status=_STATUS_NAMES.get(response.status, f"rejected_{response.status}"),
                detail=f"resend: {self._error_detail(response)}",
            )
        # 409 (idempotency conflict) or any 5xx - see module docstring.
        return CommunicationSendResult(
            ok=False, unknown=True, provider_message_id=None, status="unknown",
            detail=f"resend: ambiguous response ({response.status}): {self._error_detail(response)}",
        )

    def retrieve(self, *, provider_message_id: str) -> CommunicationSendResult:
        """The only way UNKNOWN is ever resolved - reads current Resend
        truth via ``GET /emails/{id}``. Only reachable when a prior attempt
        actually received a message id (see module docstring on why a
        connection-level ambiguity has nothing to reconcile against)."""
        assert self.http is not None
        start = time.monotonic()
        try:
            response = self.http.request(
                "GET", f"{API_BASE}/emails/{provider_message_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except (ProviderHttpError, TimeoutError, OSError):
            self._log("retrieve", outcome="timeout", start=start)
            return CommunicationSendResult(
                ok=False, unknown=True, provider_message_id=provider_message_id, status="unknown",
                detail="resend: retrieve request timed out or connection failed",
            )
        self._log(
            "retrieve", outcome="ok" if response.ok else _classify_status(response.status),
            start=start, status=response.status,
        )

        if response.status == 404:
            return CommunicationSendResult(
                ok=False, unknown=False, provider_message_id=provider_message_id, status="not_found",
                detail="resend: no such message",
            )
        if not response.ok:
            return CommunicationSendResult(
                ok=False, unknown=True, provider_message_id=provider_message_id, status="unknown",
                detail=f"resend: retrieve returned {response.status}",
            )
        try:
            body = response.json()
        except ProviderHttpError:
            return CommunicationSendResult(
                ok=False, unknown=True, provider_message_id=provider_message_id, status="unknown",
                detail="resend: retrieve response was not valid JSON",
            )
        last_event = str(body.get("last_event") or "").strip().lower()
        if last_event in _SENT_EVENTS:
            return CommunicationSendResult(
                ok=True, unknown=False, provider_message_id=provider_message_id, status="success",
                detail=f"resend: last_event={last_event}",
            )
        if last_event == "failed":
            return CommunicationSendResult(
                ok=False, unknown=False, provider_message_id=provider_message_id, status="failed",
                detail="resend: last_event=failed",
            )
        return CommunicationSendResult(
            ok=False, unknown=True, provider_message_id=provider_message_id, status="unknown",
            detail=f"resend: unrecognised last_event {last_event!r}",
        )
