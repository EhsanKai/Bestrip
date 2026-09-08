"""What we keep about where an offer came from (V7.5).

A real fare is not a number. It is a *quote*, made by a named provider, valid
until a stated moment, and bookable only through an identifier that provider
issued. V7's synthetic fares had none of that, because a fabricated price never
expires and nobody can book it.

This is the seam that carries those facts without letting the provider's own
vocabulary into the engine. The optimizer may read ``expires_at`` and compare it
to a clock; it may not know what a Duffel slice is. Everything provider-shaped
stays behind :attr:`raw_segments`, which is opaque by contract - it exists so a
future booking call can reconstruct what to book, not so domain code can reason
about it.

The rule this module exists to hold: **absent is unknown, never favourable.**
``expires_at=None`` means the provider did not say when the price dies, which
is emphatically not "it never does". ``hold_supported=None`` means we do not
know whether the fare can be held, which is not "it can". The same discipline
V7 applied to baggage, applied to time and to booking capability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class OfferFreshness(str, Enum):
    """Whether a quoted offer can still be trusted."""

    FRESH = "FRESH"
    EXPIRING_SOON = "EXPIRING_SOON"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    """The provider never said when this dies. Not the same as fresh."""


#: How close to expiry an offer has to be before it is worth warning about.
#: Chosen to be longer than a person plausibly takes to read a result page and
#: decide, so "expiring soon" is a useful warning rather than a permanent label.
EXPIRING_SOON_SECONDS = 300


class ProviderOfferReference(BaseModel):
    """Everything needed to find this offer again, and nothing more.

    Frozen, because an offer reference that could be edited after the quote was
    made is a way to book something other than what was shown.
    """

    model_config = ConfigDict(frozen=True)

    provider: str = Field(min_length=1, max_length=40)
    """Which system issued this. ``"duffel"``, ``"amadeus"``, ``"synthetic"``."""
    offer_id: str = Field(min_length=1, max_length=200)
    """Opaque to every layer above the adapter."""

    expires_at: datetime | None = None
    """When the provider says this quote dies. ``None`` means it did not say."""
    hold_supported: bool | None = None
    """Whether the fare can be held without instant payment. ``None`` = unknown.

    Airlines are inconsistent about this and Duffel reports it per offer, so a
    tri-state is the truthful shape. Multi-ticket booking must not be designed
    as though hold were guaranteed.
    """
    hold_until: datetime | None = None

    owner_iata: str | None = None
    """The airline that owns the fare, for display and for booking rules."""
    raw_segments: tuple[dict, ...] = ()
    """Provider-shaped segment detail, kept opaque.

    Carried so a future booking call can reconstruct the journey. Domain code
    must never read inside these: the moment it does, the provider's schema has
    escaped the adapter and changing providers becomes an engine change.
    """

    def freshness_at(self, moment: datetime | None = None) -> OfferFreshness:
        """How much life this quote has left.

        ``UNKNOWN`` when the provider stated no expiry - deliberately not
        ``FRESH``. A quote whose lifetime nobody declared has not been shown to
        be current; it has only failed to be shown stale.
        """
        if self.expires_at is None:
            return OfferFreshness.UNKNOWN
        now = moment or datetime.now(timezone.utc)
        expires = self.expires_at
        # Compare like with like. A naive provider timestamp read against an
        # aware clock raises, and an offer that crashes the comparison would be
        # treated as unusable for a reason that has nothing to do with the fare.
        if (expires.tzinfo is None) != (now.tzinfo is None):
            expires = (
                expires.replace(tzinfo=timezone.utc)
                if expires.tzinfo is None
                else expires.astimezone(timezone.utc)
            )
            now = (
                now.replace(tzinfo=timezone.utc)
                if now.tzinfo is None
                else now.astimezone(timezone.utc)
            )
        remaining = (expires - now).total_seconds()
        if remaining <= 0:
            return OfferFreshness.EXPIRED
        if remaining <= EXPIRING_SOON_SECONDS:
            return OfferFreshness.EXPIRING_SOON
        return OfferFreshness.FRESH

    def is_expired_at(self, moment: datetime | None = None) -> bool:
        """Whether this must not be offered any more.

        An unknown expiry is **not** treated as expired: refusing every offer a
        provider declined to timestamp would discard usable fares on a
        technicality. It is instead reported as ``UNKNOWN`` freshness so the
        caller can decide, which is the honest division of responsibility.
        """
        return self.freshness_at(moment) is OfferFreshness.EXPIRED
