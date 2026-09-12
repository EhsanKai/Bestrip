"""Communication domain (V9 Phase 5).

Customer communication (email, SMS, push) lifecycle and state machine.
Nothing here mutates booking or payment state - communication is purely
downstream and observational. One logical communication per booking+type,
with multiple attempts tracked separately (resends are new attempts, not
new communications).

Mirrors the explicit state-machine style of models/payment.py exactly -
ALLOWED_TRANSITIONS as data, not scattered conditionals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CommunicationChannel(str, Enum):
    """Communication delivery channel. Currently EMAIL only; structure
    allows trivial addition of SMS, PUSH later without changing core
    architecture."""

    EMAIL = "EMAIL"
    # SMS = "SMS"  # future
    # PUSH = "PUSH"  # future


class CommunicationType(str, Enum):
    """Semantic category of communication. Currently BOOKING_CONFIRMATION
    only; structured for forward compatibility."""

    BOOKING_CONFIRMATION = "BOOKING_CONFIRMATION"
    # BOOKING_RECEIPT = "BOOKING_RECEIPT"  # future
    # PAYMENT_RECEIPT = "PAYMENT_RECEIPT"  # future
    # ITINERARY_REMINDER = "ITINERARY_REMINDER"  # future


class CommunicationStatus(str, Enum):
    """Where one communication (one logical send per booking+type) currently
    stands. A COMMUNICATION tracks intent and overall status; individual
    ATTEMPTS are the retry mechanism.

    Terminal states (no outgoing transitions): ``SENT``, ``FAILED``,
    ``UNKNOWN`` (only via explicit reconciliation to SENT/FAILED).
    """

    PENDING = "PENDING"
    """Communication created, no send attempt made yet."""
    SENDING = "SENDING"
    """A send attempt is in progress or has been dispatched to the provider."""
    SENT = "SENT"
    """Provider confirmed delivery/acceptance (terminal)."""
    FAILED = "FAILED"
    """No attempt succeeded; all retries exhausted or impossible (terminal)."""
    UNKNOWN = "UNKNOWN"
    """A send attempt's outcome could not be determined (timeout, dropped
    connection) - never converted to SENT or FAILED by assumption. Resolved
    only by retrieving provider truth (reconciliation)."""


#: Same pattern as models.payment.ALLOWED_TRANSITIONS - data, not
#: scattered conditionals, so "can this transition happen" has one answer
#: a test can enumerate exhaustively.
ALLOWED_TRANSITIONS: dict[CommunicationStatus, frozenset[CommunicationStatus]] = {
    CommunicationStatus.PENDING: frozenset({
        CommunicationStatus.SENDING, CommunicationStatus.FAILED,
        CommunicationStatus.UNKNOWN,
    }),
    CommunicationStatus.SENDING: frozenset({
        CommunicationStatus.SENT, CommunicationStatus.FAILED,
        CommunicationStatus.UNKNOWN,
    }),
    CommunicationStatus.SENT: frozenset(),  # terminal
    CommunicationStatus.FAILED: frozenset(),  # terminal
    CommunicationStatus.UNKNOWN: frozenset({
        # UNKNOWN only ever resolves through reconciliation discovering the
        # provider's real state - never a blind retry.
        CommunicationStatus.SENT, CommunicationStatus.FAILED,
    }),
}


class InvalidCommunicationTransition(ValueError):
    """A communication state change the domain forbids."""


def can_transition_communication(
    current: CommunicationStatus, target: CommunicationStatus,
) -> bool:
    if current == target:
        return True  # idempotent no-op
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


class CustomerCommunication(BaseModel):
    """One logical communication per booking+type. Multiple SEND ATTEMPTS
    reference the same communication row (attempt_number increments);
    a resend never creates a new communication, only a new attempt."""

    model_config = ConfigDict(frozen=True)

    communication_id: str = Field(min_length=1, max_length=64)
    booking_id: str = Field(min_length=1, max_length=200)
    journey_reference: str = Field(min_length=1, max_length=200)
    user_id: str | None = None
    channel: CommunicationChannel
    communication_type: CommunicationType
    status: CommunicationStatus = CommunicationStatus.PENDING
    recipient_address: str = Field(min_length=1, max_length=200)
    """The email address (or phone number for SMS, token for PUSH).
    Not included in event data payloads (PII discipline)."""
    idempotency_key: str = Field(min_length=8, max_length=200)
    """One logical communication per booking+type, keyed by
    (booking_id, communication_type) UNIQUE constraint in persistence."""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = Field(default=1, ge=1)
    """Optimistic-concurrency token - a write must supply the version it
    read, or it is rejected (compare-and-swap, never read-then-write)."""

    def with_status(
        self, target: CommunicationStatus, *, now: datetime | None = None,
    ) -> "CustomerCommunication":
        if not can_transition_communication(self.status, target):
            raise InvalidCommunicationTransition(
                f"{self.communication_id}: {self.status.value} -> {target.value} is not allowed"
            )
        return self.model_copy(update={
            "status": target, "updated_at": now or datetime.now(timezone.utc),
            "version": self.version + 1,
        })


class CommunicationAttempt(BaseModel):
    """One send attempt on a communication. attempt_number increments on
    retries; each attempt stands as its own row for audit/ledger purposes."""

    model_config = ConfigDict(frozen=True)

    attempt_id: str = Field(min_length=1, max_length=64)
    communication_id: str = Field(min_length=1, max_length=64)
    attempt_number: int = Field(ge=1)
    """1, 2, 3... - explicit retry counter, never a new communication row."""
    status: str = Field(min_length=1, max_length=40)
    """Mirrors a subset of CommunicationStatus relevant to one attempt:
    PENDING/SENDING/SENT/FAILED/UNKNOWN. The communication itself may
    aggregate these to its own status."""
    provider_name: str = Field(min_length=1, max_length=40)
    """The provider (sandbox, postmark, resend, etc.) that handled this attempt."""
    provider_message_id: str | None = None
    """The provider's own reference for this send, if available."""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    """Set when the attempt reaches a terminal state (SENT, FAILED, or
    UNKNOWN is reconciled)."""


class CommunicationEvent(BaseModel):
    """One row of the append-only communication ledger. Never mutated once
    inserted - a complete, immutable audit trail."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=64)
    communication_id: str = Field(min_length=1, max_length=64)
    attempt_id: str | None = None
    """The attempt this event is associated with, if any."""
    event_type: str = Field(min_length=1, max_length=64)
    """COMMUNICATION_CREATED, EMAIL_SEND_REQUESTED, EMAIL_SENT,
    EMAIL_FAILED, EMAIL_OUTCOME_UNKNOWN, EMAIL_RESEND_REQUESTED, etc."""
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    detail: str = Field(default="", max_length=500)
    data: dict = Field(default_factory=dict)
    """Structured, non-sensitive detail only - NEVER the recipient email,
    full provider response, or PII (§Y). Reference by communication_id
    instead."""

    @field_validator("data", mode="before")
    @classmethod
    def validate_no_pii(cls, v: dict) -> dict:
        """Defensive check: ensure no @ symbol (proxy for email) in data."""
        if isinstance(v, dict):
            data_str = str(v).lower()
            if "@" in data_str:
                raise ValueError("PII detected in event data - no email addresses allowed")
        return v


def render_booking_confirmation_email(
    *,
    journey_reference: str,
    traveler_names: list[str],
    party_size: int,
    itinerary_lines: list[str],
    customer_total: float,
    currency: str,
    service_tier: str,
    confirmation_status: str,
) -> tuple[str, str]:
    """Render a transactional booking confirmation email.

    Args:
        journey_reference: Detoura journey reference (e.g., "JRN-ABC123")
        traveler_names: List of traveler names
        party_size: Total number of travelers
        itinerary_lines: List of leg descriptions (e.g., ["NYC → Paris, AA100, 10:00-22:00"])
        customer_total: Total customer amount (currency in float form)
        currency: ISO 4217 code (e.g., "EUR")
        service_tier: Service tier description (e.g., "Standard", "Premium")
        confirmation_status: Status indicator - CRITICAL: if this is not "CONFIRMED",
                            the email MUST use truthful, non-misleading language.
                            Possible values: "CONFIRMED", "PARTIAL_RECOVERY", "RECOVERY_REQUIRED",
                            "PAYMENT_UNKNOWN", "REFUND_PENDING", etc.

    Returns:
        (subject, body_text) tuple - plain text, no HTML formatting.

    Truthfulness requirement: the content MUST never produce misleading language
    when confirmation_status is anything other than "CONFIRMED". For example:
    - "PARTIAL_RECOVERY" → use "we're finishing your booking" not "confirmed"
    - "PAYMENT_UNKNOWN" → use "we're verifying payment" not "payment successful"
    - "REFUND_PENDING" → use "refund in progress" not "refunded"
    """

    # Subject line (truthful for any status)
    if confirmation_status == "CONFIRMED":
        subject = f"Your Detoura Booking Confirmation - {journey_reference}"
    else:
        subject = f"Your Detoura Booking Status - {journey_reference}"

    # Build body
    lines = []
    lines.append("Dear Traveler,")
    lines.append("")

    # Status-dependent greeting
    if confirmation_status == "CONFIRMED":
        lines.append(
            f"Your booking is confirmed! Journey reference: {journey_reference}"
        )
    elif confirmation_status in ("PARTIAL_RECOVERY", "RECOVERY_REQUIRED"):
        lines.append(
            f"We're finishing your booking. Journey reference: {journey_reference}"
        )
        lines.append("We're working on one or more parts of your trip.")
        lines.append("")
        lines.append(
            "→ Check your My Trips dashboard for details and next steps."
        )
    elif confirmation_status == "PAYMENT_UNKNOWN":
        lines.append(
            f"We're verifying your payment. Journey reference: {journey_reference}"
        )
        lines.append("")
        lines.append(
            "Your booking is being finalized. We'll send an update once payment is confirmed."
        )
    elif confirmation_status == "REFUND_PENDING":
        lines.append(
            f"A refund is in progress for your booking. Journey reference: {journey_reference}"
        )
        lines.append("You'll see the funds back in your account shortly.")
    else:
        lines.append(
            f"Your booking status update. Journey reference: {journey_reference}"
        )

    lines.append("")
    lines.append("---")
    lines.append("")

    # Travelers
    lines.append(f"Travelers ({party_size}):")
    for name in traveler_names:
        lines.append(f"  • {name}")
    lines.append("")

    # Itinerary (only show if confirmed - for UNKNOWN/RECOVERY states, don't
    # display itinerary as if it's final)
    if confirmation_status == "CONFIRMED":
        lines.append("Your Itinerary:")
        for leg in itinerary_lines:
            lines.append(f"  • {leg}")
        lines.append("")

    # Pricing (only show if confirmed or if the charge has been made)
    if confirmation_status == "CONFIRMED":
        lines.append(f"Total: {currency} {customer_total:.2f}")
        lines.append(f"Service Tier: {service_tier}")
        lines.append("")
    elif confirmation_status == "PAYMENT_UNKNOWN":
        lines.append(f"Quoted Total: {currency} {customer_total:.2f} (pending confirmation)")
        lines.append("")
    # For REFUND_PENDING, we'd include refund details if we had them - but
    # this render fn doesn't get them, so keep it simple.

    # Footer with support/next steps
    lines.append("---")
    lines.append("")
    if confirmation_status == "CONFIRMED":
        lines.append("Next Steps:")
        lines.append("  1. Review your itinerary details.")
        lines.append("  2. Check your Travel Pass availability in My Trips.")
        lines.append("  3. Contact Support if you need to make changes.")
    else:
        lines.append("Need Help?")
        lines.append("  Visit My Trips to check booking status:")
        lines.append("  → https://detoura.app/my-trips")
        lines.append("")
        lines.append("Questions? Contact support@detoura.app")

    lines.append("")
    lines.append("Best regards,")
    lines.append("The Detoura Team")

    body = "\n".join(lines)
    return subject, body
