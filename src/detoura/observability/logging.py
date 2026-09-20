"""Structured (JSON) application logging + request correlation.

Deliberately small and stdlib-only: one JSON formatter, one context-local
request id, and one ``log_event`` helper that every call site uses instead of
a free-form message, so operational logs are machine-parseable with a stable
``event`` name (see docs/V9_PRODUCTION_OBSERVABILITY_BASELINE_REPORT.md).

This module never changes application behaviour on failure (V9 Limited Beta
observability contract §18: "logging must be non-authoritative") - every
public function here swallows its own exceptions rather than letting a
logging bug become a payment or booking bug.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import uuid
from typing import Any

#: A request id - whether generated here or accepted from a caller - must be
#: short and boring: no CR/LF (log/header injection), no exotic characters, a
#: bounded length. Anything else is replaced with a fresh, safe one rather
#: than logged or echoed back as given (V9 Limited Beta observability
#: contract §5: "validate/sanitize any client-provided value").
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

_request_id_var: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "detoura_request_id", default=None
)

#: Field names a log event must never carry - defence in depth, not the
#: primary control. The primary control is simply that call sites never pass
#: these; see the privacy/redaction section of the observability report for
#: the full audit of what each instrumented call site does and does not log.
FORBIDDEN_FIELDS = frozenset({
    "password", "authorization", "cookie", "set-cookie", "csrf", "csrf_token",
    "client_secret", "stripe_secret", "webhook_secret", "signature",
    "card_number", "cvc", "cvv", "email", "traveler_party", "travelers",
    "passport", "passport_number", "birth_date", "date_of_birth", "dob",
    "access_token", "refresh_token", "api_key", "secret_key", "session",
    "session_token", "raw_body", "raw_payload", "body",
})


def current_request_id() -> str | None:
    return _request_id_var.get()


def bind_request_id(value: str | None) -> "contextvars.Token[str | None]":
    return _request_id_var.set(value)


def reset_request_id(token: "contextvars.Token[str | None]") -> None:
    _request_id_var.reset(token)


def new_request_id() -> str:
    return uuid.uuid4().hex


def sanitize_request_id(raw: str | None) -> str:
    """A client-supplied ``X-Request-Id`` is untrusted input. Accept it only
    if it is short and made of ordinary id characters; otherwise mint a fresh
    one rather than log or echo back something an attacker chose."""
    if raw and _REQUEST_ID_RE.match(raw):
        return raw
    return new_request_id()


def _safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if k.lower() not in FORBIDDEN_FIELDS}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or current_request_id()
        if request_id:
            payload["request_id"] = request_id
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(_safe_fields(fields))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, default=str, sort_keys=True)
        except Exception:
            return json.dumps({"level": "ERROR", "message": "log record could not be serialized"})


_configured = False


def configure_logging(level: str | None = None) -> None:
    """Install the JSON formatter on the root logger, once per process.

    Idempotent - safe to call from ``create_app()`` even if it runs more than
    once (multiple workers importing the module, or tests building the app
    repeatedly)."""
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.handlers[:] = [handler]
    root.setLevel((level or os.getenv("DETOURA_LOG_LEVEL", "INFO")).upper())
    _configured = True


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured operational event.

    ``fields`` become JSON keys alongside ``event``/``request_id`` - never
    pass PII, secrets, or a raw payload here (see ``FORBIDDEN_FIELDS``, and
    the observability report's privacy/redaction policy for the reasoning).
    A failure here is swallowed, never raised: an observability bug must
    never become a payment or booking bug.
    """
    try:
        logger.log(level, event, extra={"event": event, "fields": _safe_fields(fields)})
    except Exception:
        pass
