"""Configuration for the payment layer (V9 Phase 4 §V).

Every policy number and the provider mode decision are read from the
environment here, once - the same pattern as ``auth_config.py`` and
``search_intel_config.py``. Nothing here is a secret in itself; the actual
``STRIPE_SECRET_KEY``/``STRIPE_WEBHOOK_SECRET`` values are read once at
provider-construction time and never stored on this config object, so a
diagnostics dump of ``PaymentConfig`` can never leak one.

**Fail-closed mode determination** (§V): if the configured provider is
``stripe`` but the key's mode cannot be determined as clearly TEST, this
config refuses to resolve a usable provider at all rather than guessing -
see ``payment_provider_mode()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no")


DEFAULT_CHECKOUT_SNAPSHOT_TTL_SECONDS = 15 * 60
DEFAULT_RECONCILIATION_UNKNOWN_GRACE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class PaymentConfig:
    provider: str = "sandbox"
    """Which adapter ``payment_service`` resolves - ``"sandbox"`` (the
    deterministic in-process reference adapter, default and always safe) or
    ``"stripe"`` (only usable with a real TEST-mode key; refuses to run
    otherwise)."""
    live_charging_enabled: bool = False
    """Master kill switch (§V). ``False`` unconditionally forces the
    sandbox adapter regardless of ``provider`` - a developer setting
    ``PAYMENT_PROVIDER=stripe`` with a live key cannot create a live charge
    just by misconfiguring this one variable; a second, explicit flag has
    to also be true, and even then the Stripe adapter itself refuses a
    ``sk_live_`` key unless a third, separate override is passed at
    construction time (defence in depth, mirroring the Duffel test-token
    guard)."""
    checkout_snapshot_ttl_seconds: int = DEFAULT_CHECKOUT_SNAPSHOT_TTL_SECONDS
    reconciliation_unknown_grace_seconds: int = DEFAULT_RECONCILIATION_UNKNOWN_GRACE_SECONDS
    """How long a payment may sit at UNKNOWN before reconciliation treats
    the silence itself as something to flag (not auto-resolve, just flag)."""
    price_tolerance_absolute: float = 0.0
    price_tolerance_percentage: float = 0.0
    """How much the payable amount may drift between snapshot and
    authorization before the customer must reconfirm (§C) - mirrors
    ``models.booking.PriceTolerance``'s semantics exactly, kept as a
    separate config surface since payment tolerance may legitimately be
    stricter than booking-leg tolerance."""

    @classmethod
    def from_env(cls) -> "PaymentConfig":
        return cls(
            provider=os.getenv("PAYMENT_PROVIDER", "sandbox").strip().lower() or "sandbox",
            live_charging_enabled=_bool("PAYMENT_LIVE_CHARGING_ENABLED", False),
            checkout_snapshot_ttl_seconds=max(
                60, _int("PAYMENT_CHECKOUT_SNAPSHOT_TTL_SECONDS", DEFAULT_CHECKOUT_SNAPSHOT_TTL_SECONDS)
            ),
            reconciliation_unknown_grace_seconds=max(
                0, _int("PAYMENT_RECONCILIATION_UNKNOWN_GRACE_SECONDS",
                        DEFAULT_RECONCILIATION_UNKNOWN_GRACE_SECONDS)
            ),
            price_tolerance_absolute=max(0.0, _float("PAYMENT_PRICE_TOLERANCE_ABSOLUTE", 0.0)),
            price_tolerance_percentage=max(0.0, _float("PAYMENT_PRICE_TOLERANCE_PERCENTAGE", 0.0)),
        )


_CONFIG: PaymentConfig | None = None
_SANDBOX_PROVIDER = None  # type: SandboxPaymentProvider | None


def payment_config() -> PaymentConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = PaymentConfig.from_env()
    return _CONFIG


def reset_payment_config() -> None:
    global _CONFIG, _SANDBOX_PROVIDER
    _CONFIG = None
    _SANDBOX_PROVIDER = None


def _sandbox_provider():
    """The one process-wide sandbox provider instance.

    Critical: the sandbox is stateful in-process (it remembers which
    references were authorized, so a later capture/refund/reconcile call
    can find them - see ``providers/sandbox_payment.py``). A confirm and a
    later capture/refund/webhook are *always* separate calls to
    :func:`resolve_provider` in real use (separate HTTP requests, at least)
    - constructing a fresh ``SandboxPaymentProvider()`` on every call would
    silently hand each one an empty account table, so every capture/refund/
    reconcile after the first request would see ``not_found`` and fail,
    every time. One long-lived instance per process, exactly like
    ``booking_flow.booking_store()``, fixes that."""
    global _SANDBOX_PROVIDER
    if _SANDBOX_PROVIDER is None:
        from .providers.sandbox_payment import SandboxPaymentProvider

        _SANDBOX_PROVIDER = SandboxPaymentProvider()
    return _SANDBOX_PROVIDER


def resolve_provider(cfg: "PaymentConfig | None" = None):
    """The single seam every caller uses to get a
    :class:`~detoura.providers.payment_provider.PaymentProvider` - never
    construct :class:`~detoura.providers.stripe_payment.StripePaymentProvider`
    directly elsewhere.

    Fail-closed chain, in order:

    1. ``live_charging_enabled`` is ``False`` (the default) -> always the
       sandbox adapter, no matter what ``provider`` says.
    2. ``provider != "stripe"`` -> the sandbox adapter.
    3. ``provider == "stripe"`` and live charging is enabled, but
       ``STRIPE_SECRET_KEY`` is missing, not a recognisable key shape, or is
       a live key without an explicit, separate override -> raises rather
       than silently falling back (misconfiguration must be loud, not a
       quiet demotion to sandbox that a deployer mistakes for "it's using
       Stripe").
    """
    from .providers.payment_provider import PaymentProvider

    cfg = cfg or payment_config()
    if not cfg.live_charging_enabled or cfg.provider != "stripe":
        return _sandbox_provider()

    from .providers.stripe_payment import StripeConfigurationError, StripePaymentProvider

    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    if not key:
        raise StripeConfigurationError(
            "PAYMENT_PROVIDER=stripe and live charging is enabled, but "
            "STRIPE_SECRET_KEY is not set; refusing to fall back to the "
            "sandbox adapter silently - fix the configuration or disable "
            "PAYMENT_LIVE_CHARGING_ENABLED."
        )
    return StripePaymentProvider(secret_key=key, webhook_secret=webhook_secret)
