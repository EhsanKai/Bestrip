"""What re-checking a selected itinerary against the provider found (V8 Phase 3).

Search results are *discovery data*. They were true when the snapshot was
taken and say nothing about now. Booking must run on *revalidated data*, and
this module is the shape of that revalidation: for every bookable offer in a
selected itinerary, what it was at discovery, what it is now, and whether the
difference is small enough to proceed or large enough that the traveller has to
agree again.

Two rules the design holds:

**Discovery truth is never overwritten.** `discovered_*` and `current_*` are
separate fields. A revalidation that found a price rise records both numbers;
it does not update the offer in place, because the delta *is* the answer.

**Unavailable, expired and provider-error stay apart.** Collapsing them loses
the one distinction that matters: an expired offer might be re-acquirable, a
deleted one is gone, and a provider timeout is a statement about us, not the
offer. `services/recheck.py` holds the same line for saved trips.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .baggage import BaggageStatus
from .booking import PriceTolerance
from .money import BASE_CURRENCY


class OfferRevalidationStatus(str, Enum):
    """What became of one offer when it was re-fetched."""

    UNCHANGED = "UNCHANGED"
    """Re-fetched, still there, price and terms within noise."""
    PRICE_CHANGED = "PRICE_CHANGED"
    """Still bookable, still the same journey and terms - the number moved."""
    TERMS_CHANGED = "TERMS_CHANGED"
    """Baggage, hold, or itinerary detail changed. May also have moved on price."""
    UNAVAILABLE = "UNAVAILABLE"
    """The provider answered and this offer was not in the answer (404/410)."""
    EXPIRED = "EXPIRED"
    """Re-fetched, but its stated expiry is in the past."""
    PROVIDER_ERROR = "PROVIDER_ERROR"
    """We could not complete the check. A fact about us, not the offer."""

    @property
    def is_bookable(self) -> bool:
        """Whether this offer can still be part of a booking at all.

        Price and terms movement are questions for the tolerance check; these
        three are not - the offer cannot be bought.
        """
        return self not in (
            OfferRevalidationStatus.UNAVAILABLE,
            OfferRevalidationStatus.EXPIRED,
            OfferRevalidationStatus.PROVIDER_ERROR,
        )


class RevalidationStatus(str, Enum):
    """What became of the whole selected itinerary."""

    READY = "READY"
    """Every offer bookable, nothing changed beyond noise (or prices fell)."""
    READY_WITH_MINOR_CHANGE = "READY_WITH_MINOR_CHANGE"
    """Every offer bookable; a price rose but stayed within the configured
    tolerance and no term was downgraded. Disclosed, not blocking."""
    USER_RECONFIRMATION_REQUIRED = "USER_RECONFIRMATION_REQUIRED"
    """Bookable, but something the traveller agreed to is no longer true: a
    price beyond tolerance, a baggage downgrade, hold withdrawn, an itinerary
    detail moved, or a quote whose freshness we cannot establish."""
    NOT_BOOKABLE = "NOT_BOOKABLE"
    """At least one required offer is unavailable, expired, errored, or in a
    currency we will not relabel. The itinerary cannot proceed as selected."""

    @property
    def may_proceed_to_confirmation(self) -> bool:
        return self in (
            RevalidationStatus.READY,
            RevalidationStatus.READY_WITH_MINOR_CHANGE,
        )


class ChangeSeverity(str, Enum):
    INFO = "INFO"
    """Disclosed for honesty; does not affect bookability. A price drop, a bag
    that became included, hold that became available."""
    MINOR = "MINOR"
    """Within tolerance. Proceed, but say so."""
    BLOCKING = "BLOCKING"
    """Requires renewed consent or stops the booking."""


class OfferChange(BaseModel):
    """One thing that is different between the discovered offer and now."""

    model_config = ConfigDict(frozen=True)

    field: str
    """`total_price`, `currency`, `cabin_bag`, `checked_bag`, `hold`,
    `expiry`, `departure`, `arrival`, `availability`."""
    discovered: str
    """The value at discovery, rendered for display. Never null - "unknown" is
    a string here, so the change is always legible."""
    current: str
    severity: ChangeSeverity
    detail: str = ""


class RevalidatedOffer(BaseModel):
    """One bookable offer, discovered value beside current value."""

    model_config = ConfigDict(frozen=True)

    offer_id: str
    """The provider's own id, preserved for the booking call that may follow."""
    provider: str = "duffel"
    origin: str
    destination: str
    leg_label: str = ""

    status: OfferRevalidationStatus
    changes: tuple[OfferChange, ...] = ()

    discovered_amount: float
    """Per the itinerary total's currency (base). What the traveller was shown."""
    current_amount: float | None = None
    """`None` when the offer could not be re-priced (unavailable/expired/error)."""
    discovered_currency: str = BASE_CURRENCY
    current_currency: str | None = None

    discovered_baggage_cabin: BaggageStatus | None = None
    current_baggage_cabin: BaggageStatus | None = None
    discovered_baggage_checked: BaggageStatus | None = None
    current_baggage_checked: BaggageStatus | None = None

    discovered_hold_supported: bool | None = None
    current_hold_supported: bool | None = None

    current_expires_at: datetime | None = None
    detail: str = ""

    @property
    def amount_delta(self) -> float | None:
        if self.current_amount is None:
            return None
        return round(self.current_amount - self.discovered_amount, 2)

    @property
    def has_blocking_change(self) -> bool:
        return any(c.severity is ChangeSeverity.BLOCKING for c in self.changes)


class OfferRevalidationResult(BaseModel):
    """The whole selected itinerary, re-checked immediately before booking."""

    model_config = ConfigDict(frozen=True)

    selection_id: str = ""
    status: RevalidationStatus
    bookable: bool
    """Every required offer can still be bought. Separate from `status`: an
    itinerary can be `bookable` and still `USER_RECONFIRMATION_REQUIRED`."""

    discovered_total: float
    current_total: float | None = None
    """`None` when any offer could not be re-priced - a total assembled from a
    subset would read as a bargain rather than as an incomplete answer."""
    currency: str = BASE_CURRENCY

    tolerance: PriceTolerance = Field(default_factory=PriceTolerance)
    offers: tuple[RevalidatedOffer, ...] = ()
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    notes: tuple[str, ...] = ()

    @property
    def total_delta(self) -> float | None:
        if self.current_total is None:
            return None
        return round(self.current_total - self.discovered_total, 2)

    @property
    def total_delta_pct(self) -> float | None:
        if self.current_total is None or self.discovered_total <= 0:
            return None
        return round((self.current_total - self.discovered_total) / self.discovered_total * 100, 2)

    @property
    def changed_offers(self) -> tuple[RevalidatedOffer, ...]:
        return tuple(o for o in self.offers if o.status is not OfferRevalidationStatus.UNCHANGED)


#: Named tolerance presets. The spec is explicit that tolerance is configuration,
#: not a hidden constant - these give a request three honest choices.
NO_INCREASE = PriceTolerance(absolute=0.0, percentage=0.0)
"""Any increase at all requires renewed confirmation."""


def absolute_eur(amount: float) -> PriceTolerance:
    """Tolerate an increase up to a fixed number of euros."""
    return PriceTolerance(absolute=max(amount, 0.0), percentage=0.0)


def percentage(pct: float) -> PriceTolerance:
    """Tolerate an increase up to a percentage of the discovered total."""
    return PriceTolerance(absolute=0.0, percentage=max(pct, 0.0))
