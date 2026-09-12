"""Configuration for the communication layer (V9 Phase 5).

Mirrors ``payment_config.py`` exactly, including its most important fix:
the resolved provider MUST be a process-wide singleton, never constructed
fresh on every call. Phase 4 shipped ``resolve_provider()`` calling
``SandboxPaymentProvider()`` directly the first time round, and it was a
real, severe bug - the sandbox is stateful in-process (it must remember a
sent message so a later ``retrieve()``/reconciliation call can find it),
so two different HTTP requests each getting their own empty provider
instance silently broke reconciliation across requests. Built correctly
here from the start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no")


@dataclass(frozen=True, slots=True)
class CommunicationConfig:
    provider: str = "sandbox"
    live_sending_enabled: bool = False
    """Master kill switch, exactly like ``PaymentConfig.live_charging_enabled``
    - ``False`` unconditionally forces the sandbox adapter regardless of
    ``provider``, so a misconfigured ``COMMUNICATION_PROVIDER=<real>``
    cannot cause a live send by itself."""

    @classmethod
    def from_env(cls) -> "CommunicationConfig":
        return cls(
            provider=os.getenv("COMMUNICATION_PROVIDER", "sandbox").strip().lower() or "sandbox",
            live_sending_enabled=_bool("COMMUNICATION_LIVE_SENDING_ENABLED", False),
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
    :class:`~detoura.providers.communication_provider.CommunicationProvider`.
    No real (non-sandbox) provider is wired in this phase at all - a real
    adapter, if one existed, would only ever be reachable with
    ``live_sending_enabled=True`` AND ``provider`` naming it explicitly,
    exactly mirroring the payment kill-switch chain."""
    cfg = cfg or communication_config()
    if not cfg.live_sending_enabled or cfg.provider != "sandbox":
        return _sandbox_provider()
    return _sandbox_provider()
