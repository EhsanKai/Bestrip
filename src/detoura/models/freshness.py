"""Price freshness (V5.3).

Real travel prices move. An engine that caches a quote for twenty minutes and
then presents it with the same confidence as one fetched a second ago is
telling the user something it does not know.

The rule: **the optimizer must never pretend stale information is fresh.** It
is allowed to *use* stale information - a price from ten minutes ago is far
better than no price - but the result has to say so, and the UI has to be able
to render "checked just now" differently from "this may have changed".

Four states rather than a timestamp, because a timestamp is not an answer to
the question a traveler is actually asking:

    FRESH    fetched moments ago; quote it plainly
    RECENT   fetched within the window a price usually holds
    STALE    old enough that it should be re-checked before booking
    UNKNOWN  no provenance at all - synthetic data, or a provider that does
             not say. Deliberately not the same as STALE: "we never knew" and
             "we knew a while ago" are different claims.

An itinerary's freshness is the **worst** of its parts. A trip is only as
current as its oldest quote, and averaging would let three fresh legs hide one
that expired.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

#: Fetched within this, a quote is quoted plainly.
FRESH_WINDOW = timedelta(minutes=5)

#: Within this, it is worth a gentle note. Beyond it, a warning.
#: Both are judgements about how fast fares move, not measurements, and they
#: are configurable for that reason.
RECENT_WINDOW = timedelta(minutes=30)


class PriceFreshness(str, Enum):
    """How much a quoted price should be trusted."""

    FRESH = "FRESH"
    RECENT = "RECENT"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"

    @property
    def rank(self) -> int:
        """Ordering for "worst of". Higher is worse.

        UNKNOWN ranks between RECENT and STALE: absent provenance is worse than
        a price we know is recent, and better than one we know is old.
        """
        return {"FRESH": 0, "RECENT": 1, "UNKNOWN": 2, "STALE": 3}[self.value]

    @property
    def is_quotable(self) -> bool:
        """Whether this price may be presented without a caveat."""
        return self in (PriceFreshness.FRESH, PriceFreshness.RECENT)


class PriceProvenance(BaseModel):
    """Where a price came from and when.

    Attached to an option by the provider that fetched it. The synthetic
    providers leave it unset, which is honest: their prices are fabricated and
    have no provenance to report.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    fetched_at: datetime
    expires_at: datetime | None = None
    """When the provider says the quote stops being valid, if it says."""

    currency: str = "EUR"
    converted_from: str | None = None
    """The original currency, when conversion happened at the boundary."""

    def freshness_at(
        self,
        moment: datetime,
        *,
        fresh_window: timedelta = FRESH_WINDOW,
        recent_window: timedelta = RECENT_WINDOW,
    ) -> PriceFreshness:
        """How fresh this quote is at ``moment``.

        An explicit ``expires_at`` from the provider always wins: the provider
        knows its own quote lifetime better than any window we would guess.
        """
        if self.expires_at is not None and moment >= self.expires_at:
            return PriceFreshness.STALE
        age = moment - self.fetched_at
        if age < timedelta(0):
            # A clock skew or a future-dated quote. Refusing to call it FRESH
            # is the conservative reading.
            return PriceFreshness.UNKNOWN
        if age <= fresh_window:
            return PriceFreshness.FRESH
        if age <= recent_window:
            return PriceFreshness.RECENT
        return PriceFreshness.STALE


def combine(*values: PriceFreshness | None) -> PriceFreshness:
    """The freshness of a thing assembled from several quotes.

    The worst of them, because a trip is only as current as its oldest price.
    An empty or all-``None`` input is ``UNKNOWN``, never ``FRESH``: nothing to
    go on is not evidence of currency.
    """
    present = [value for value in values if value is not None]
    if not present:
        return PriceFreshness.UNKNOWN
    return max(present, key=lambda freshness: freshness.rank)


class PriceChange(BaseModel):
    """What happened to a saved trip's price since it was last seen (V5.3.1)."""

    model_config = ConfigDict(frozen=True)

    previous: float
    current: float
    currency: str = "EUR"
    previous_checked_at: datetime | None = None
    checked_at: datetime | None = None
    confident: bool = True
    """False when either side lacked provenance.

    The spec's instruction - *"do not overstate precision when provider data is
    uncertain"* - lives here. A €14 rise computed from two prices of unknown
    vintage is not a €14 rise, it is two numbers that differ, and the UI needs
    to know which it has.
    """

    @property
    def delta(self) -> float:
        return round(self.current - self.previous, 2)

    @property
    def direction(self) -> str:
        if self.delta > 0:
            return "up"
        if self.delta < 0:
            return "down"
        return "same"

    @property
    def material(self) -> bool:
        """Whether the change is worth telling the user about.

        Under a euro, or under half a percent, is noise dressed as news.
        """
        if self.delta == 0:
            return False
        if abs(self.delta) < 1.0:
            return False
        return abs(self.delta) / max(self.previous, 1.0) >= 0.005

    def message(self) -> str | None:
        """The sentence to show, or ``None`` when there is nothing to say."""
        if not self.material:
            return None
        amount = f"{abs(self.delta):.0f} {self.currency}"
        if not self.confident:
            # Hedged deliberately: we are reporting a difference between two
            # figures, not a tracked price movement.
            return (
                f"This trip now prices at {self.current:.0f} {self.currency}, "
                f"about {amount} {'more' if self.delta > 0 else 'less'} than "
                "when you saved it."
            )
        if self.delta < 0:
            return f"Good news - this trip is now {amount} cheaper."
        return f"Price increased by {amount}."


class FreshnessSummary(BaseModel):
    """The freshness of one itinerary, ready for the API."""

    model_config = ConfigDict(frozen=True)

    status: PriceFreshness = PriceFreshness.UNKNOWN
    oldest_fetched_at: datetime | None = None
    providers: list[str] = Field(default_factory=list)

    @property
    def label(self) -> str:
        return {
            PriceFreshness.FRESH: "Price checked just now",
            PriceFreshness.RECENT: "Price checked recently",
            PriceFreshness.STALE: "Price may have changed",
            PriceFreshness.UNKNOWN: "Estimated price",
        }[self.status]
