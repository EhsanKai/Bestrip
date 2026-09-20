"""Configuration for the communication layer (V9 Phase 5; production Resend
adapter added V9 Production Transactional Email).

Mirrors ``payment_config.py`` exactly, including its most important fix:
the resolved provider MUST be a process-wide singleton, never constructed
fresh on every call. Phase 4 shipped ``resolve_provider()`` calling
``SandboxPaymentProvider()`` directly the first time round, and it was a
real, severe bug - the sandbox is stateful in-process (it must remember a
sent message so a later ``retrieve()``/reconciliation call can find it),
so two different HTTP requests each getting their own empty provider
instance silently broke reconciliation across requests. Built correctly
here from the start. ``ResendEmailProvider`` is stateless (a pure HTTP
adapter, like ``StripePaymentProvider``), so it is constructed fresh per
call - no singleton needed for it.

**Fail-closed provider resolution**, exactly mirroring
``payment_config.py::resolve_provider``'s chain:

1. ``live_sending_enabled`` is ``False`` (the default) -> always the
   sandbox adapter, no matter what ``provider`` says.
2. ``provider == "sandbox"`` -> the sandbox adapter, even with live sending
   enabled (an explicit choice, e.g. a staging environment).
3. ``provider == "resend"`` and live sending is enabled, but
   ``RESEND_API_KEY``/``RESEND_FROM_EMAIL`` is missing or malformed ->
   raises rather than silently falling back to sandbox (misconfiguration
   must be loud, never a quiet demotion a deployer mistakes for "it's
   using Resend").
4. Any other ``provider`` value -> raises; there is no silent default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no")


class CommunicationConfigurationError(RuntimeError):
    """The communication layer refuses to resolve a usable provider as
    configured. Never carries a secret value."""


@dataclass(frozen=True, slots=True)
class CommunicationConfig:
    provider: str = "sandbox"
    live_sending_enabled: bool = False
    """Master kill switch, exactly like ``PaymentConfig.live_charging_enabled``
    - ``False`` unconditionally forces the sandbox adapter regardless of
    ``provider``, so a misconfigured ``COMMUNICATION_PROVIDER=<real>``
    cannot cause a live send by itself."""
    resend_from_name: str = "Detoura"
    """Sender display name - not a secret, kept on this config object
    (unlike ``RESEND_API_KEY``/``RESEND_FROM_EMAIL`` - see
    ``resolve_communication_provider``, which reads those directly from the
    environment at provider-construction time only, mirroring
    ``payment_config.py``'s exact reasoning: a diagnostics dump of this
    config object must never be able to leak a secret)."""
    resend_reply_to: str = ""

    @classmethod
    def from_env(cls) -> "CommunicationConfig":
        return cls(
            provider=os.getenv("COMMUNICATION_PROVIDER", "sandbox").strip().lower() or "sandbox",
            live_sending_enabled=_bool("COMMUNICATION_LIVE_SENDING_ENABLED", False),
            resend_from_name=os.getenv("RESEND_FROM_NAME", "Detoura").strip() or "Detoura",
            resend_reply_to=os.getenv("RESEND_REPLY_TO", "").strip(),
        )


_CONFIG: CommunicationConfig | None = None
_SANDBOX_PROVIDER = None  # type: object | None


def communication_config() -> CommunicationConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = CommunicationConfig.from_env()
    return _CONFIG


def reset_communication_config() -> None:
    global _CONFIG, _SANDBOX_PROVIDER
    _CONFIG = None
    _SANDBOX_PROVIDER = None


def _sandbox_provider():
    """The one process-wide sandbox email provider instance - see the
    module docstring for why this must never be constructed fresh per call."""
    global _SANDBOX_PROVIDER
    if _SANDBOX_PROVIDER is None:
        from .providers.sandbox_email import SandboxEmailProvider

        _SANDBOX_PROVIDER = SandboxEmailProvider()
    return _SANDBOX_PROVIDER


def resolve_communication_provider(cfg: "CommunicationConfig | None" = None):
    """The single seam every caller uses to get a
    :class:`~detoura.providers.communication_provider.CommunicationProvider`
    - never construct :class:`~detoura.providers.resend_email.ResendEmailProvider`
    directly elsewhere. See the module docstring for the fail-closed chain."""
    cfg = cfg or communication_config()
    if not cfg.live_sending_enabled or cfg.provider == "sandbox":
        return _sandbox_provider()

    if cfg.provider == "resend":
        from .providers.resend_email import ResendConfigurationError, ResendEmailProvider

        api_key = os.getenv("RESEND_API_KEY", "").strip()
        from_email = os.getenv("RESEND_FROM_EMAIL", "").strip()
        if not api_key or not from_email:
            raise ResendConfigurationError(
                "COMMUNICATION_PROVIDER=resend and live sending is enabled, but "
                "RESEND_API_KEY and/or RESEND_FROM_EMAIL is not set; refusing to "
                "fall back to the sandbox adapter silently - fix the "
                "configuration or disable COMMUNICATION_LIVE_SENDING_ENABLED."
            )
        return ResendEmailProvider(
            api_key=api_key, from_email=from_email,
            from_name=cfg.resend_from_name, reply_to=cfg.resend_reply_to,
        )

    raise CommunicationConfigurationError(
        f"unknown COMMUNICATION_PROVIDER {cfg.provider!r}; expected 'sandbox' or 'resend'"
    )
