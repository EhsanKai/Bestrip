"""Provider-neutral communication provider interface (V9 Phase 5).

Nothing in domain/orchestration code ever imports a specific provider's SDK
objects or field names. Every provider - the sandbox reference adapter, a
real Postmark/Resend/SendGrid adapter - implements this same surface, and
every result comes back as one of the frozen dataclasses below, never a raw
provider payload.

Provider capabilities genuinely differ - not every provider supports delivery
events, not every provider models "idempotency key" the same way.
:class:`CommunicationProviderCapabilities` makes those differences an explicit,
queryable fact instead of assumptions baked into orchestration code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CommunicationProviderCapabilities:
    """Truthful declaration of what this provider can do."""

    supports_delivery_events: bool
    """Whether this provider sends webhooks for delivery confirmation."""
    supports_idempotency_keys: bool
    """Whether the provider itself deduplicates by an idempotency key we
    supply (in addition to our own local idempotency, which always applies
    regardless of this flag)."""
    max_retries_recommended: int
    """Suggested maximum number of send retries before giving up."""


@dataclass(frozen=True, slots=True)
class CommunicationSendResult:
    """One provider call's outcome. ``status`` is the provider's OWN
    vocabulary (e.g. Postmark's ``success``/``error``) - the communication
    service, not the provider, decides what :class:`~detoura.models.communication.CommunicationStatus`
    that maps to. ``unknown`` means the call's outcome could not be
    determined (timeout, dropped connection, ambiguous response) - never
    silently treated as failure."""

    ok: bool
    """The provider accepted/confirmed the send."""
    unknown: bool
    """The outcome could not be determined - requires reconciliation."""
    provider_message_id: str | None
    """The provider's own reference for this message, if available."""
    status: str
    """The provider's status vocabulary (e.g. 'success', 'rejected', 'unknown')."""
    detail: str = ""
    """Human-readable detail for debugging/logging."""


@runtime_checkable
class CommunicationProvider(Protocol):
    """The contract every communication provider adapter implements."""

    name: str
    """The provider's identifier (e.g. 'sandbox', 'postmark', 'resend')."""

    def capabilities(self) -> CommunicationProviderCapabilities:
        """Declare what this provider supports."""
        ...

    def send(
        self,
        *,
        idempotency_key: str,
        recipient: str,
        subject: str,
        body_text: str,
        reference: str,
    ) -> CommunicationSendResult:
        """Send an email.

        Args:
            idempotency_key: Unique key for deduplication - a repeated call with
                           the same key MUST return the exact same result
                           (either from cache or from the provider's own
                           idempotency mechanism). ``reference`` is Detoura's own
                           communication_id, threaded through for provider-side
                           traceability - never the other way around.
            recipient: Email address to send to.
            subject: Email subject line.
            body_text: Email body (plain text, not HTML).
            reference: Detoura's own communication_id, threaded through for
                      provider-side traceability.

        Returns:
            :class:`CommunicationSendResult` with outcome.
        """
        ...

    def retrieve(self, *, provider_message_id: str) -> CommunicationSendResult:
        """Fetch current provider truth - the only way UNKNOWN is ever
        resolved (reconciliation). Never guessed, never inferred from a
        timeout.

        Args:
            provider_message_id: The provider's own reference for a message.

        Returns:
            :class:`CommunicationSendResult` with current provider status.
        """
        ...
