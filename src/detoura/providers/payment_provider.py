"""Provider-neutral payment provider interface (V9 Phase 4 §L).

Nothing in domain/orchestration code ever imports a specific provider's SDK
objects or field names. Every provider - the sandbox reference adapter, a
real Stripe adapter - implements this same surface, and every result comes
back as one of the frozen dataclasses below, never a raw provider payload.

Provider capabilities genuinely differ (§F) - not every payment method
supports manual capture, not every provider models "requires customer
action" the same way. :class:`ProviderCapabilities` makes those differences
an explicit, queryable fact instead of an assumption baked into
orchestration code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    supports_manual_capture: bool
    """Whether ``authorize`` then a separate ``capture`` is possible for this
    provider/method - if not, "authorize" and "capture" happen together and
    the orchestrator must not assume it can hold funds uncaptured (§F)."""
    supports_partial_capture: bool
    supports_partial_refund: bool
    supports_customer_action: bool
    """Whether this provider/method can report REQUIRES_CUSTOMER_ACTION
    (SCA/3DS or equivalent) rather than a flat success/failure (§N)."""
    supports_webhooks: bool
    supports_idempotency_keys: bool
    """Whether the provider itself deduplicates by an idempotency key we
    supply (in addition to our own local idempotency, which always applies
    regardless of this flag)."""
    unsupported_payment_methods: tuple[str, ...] = field(default_factory=tuple)
    """Payment methods this integration does not (yet) support - reported,
    never silently accepted and mishandled."""


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """One provider call's outcome. ``status`` is the provider's OWN
    vocabulary (e.g. Stripe's ``requires_action``/``succeeded``) - the
    payment service, not the provider, decides what
    :class:`~detoura.models.payment.PaymentStatus` that maps to. ``unknown``
    means the call's outcome could not be determined (timeout, dropped
    connection, ambiguous response) - never silently treated as failure."""

    ok: bool
    unknown: bool
    provider_reference: str | None
    status: str
    authorized_amount: float | None = None
    captured_amount: float | None = None
    refunded_amount: float | None = None
    detail: str = ""
    raw_status_only: dict = field(default_factory=dict)
    """Non-sensitive provider status fields only (e.g. ``{"livemode": False}``)
    - never the full provider payload, never a card/payment-method object."""


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    """A verified, parsed provider webhook/event - never the raw signed
    body past this point."""

    provider_event_id: str
    event_type: str
    provider_reference: str | None
    status: str
    payload: dict = field(default_factory=dict)


class ProviderEventVerificationError(ValueError):
    """A webhook failed signature verification, or the mode (test/live)
    could not be determined - fail closed, never process it (§O, §V)."""


@runtime_checkable
class PaymentProvider(Protocol):
    """The contract every payment provider adapter implements."""

    name: str

    def capabilities(self) -> ProviderCapabilities: ...

    def authorize(
        self, *, idempotency_key: str, amount: float, currency: str, reference: str,
        payment_method: str | None = None,
    ) -> ProviderResult:
        """Create + authorize (or, for a provider with no manual capture,
        create + capture in one step) a payment. ``reference`` is Detoura's
        own payment_id, threaded through for provider-side traceability -
        never the other way around. ``payment_method`` is an opaque
        provider-issued token identifying an already-tokenized payment
        instrument (e.g. a Stripe ``PaymentMethod`` id produced client-side
        by Stripe.js/Elements) - never raw card data. A provider that has no
        concept of it (the sandbox) simply ignores it."""

    def capture(
        self, *, idempotency_key: str, provider_reference: str, amount: float | None = None,
    ) -> ProviderResult:
        """``amount=None`` captures the full authorized amount."""

    def cancel_authorization(
        self, *, idempotency_key: str, provider_reference: str,
    ) -> ProviderResult: ...

    def refund(
        self, *, idempotency_key: str, provider_reference: str, amount: float, reason: str = "",
    ) -> ProviderResult: ...

    def retrieve(self, *, provider_reference: str) -> ProviderResult:
        """Fetch current provider truth - the only way UNKNOWN is ever
        resolved (§K, §P). Never guessed, never inferred from a timeout."""

    def verify_event(self, *, payload: bytes, signature: str) -> ProviderEvent:
        """Verify and parse a webhook body. Raises
        :class:`ProviderEventVerificationError` on any failure - an
        unverifiable event is never processed (§O)."""
