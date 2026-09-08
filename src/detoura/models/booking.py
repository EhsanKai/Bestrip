"""One confirmation, several tickets (V7.5 foundation).

The experience Detoura is building toward: a traveller picks a journey, enters
their details once, confirms once - and Detoura books whatever underlying
tickets that takes. Four flights on three airlines through two providers is an
implementation detail they should never have to project-manage.

This module is the domain shape for that, and **nothing here books anything**.
There is no provider call, no payment, no order. V7.5 builds the state model so
V8 can execute against something already reasoned about, because the hard part
of multi-ticket booking is not the happy path - it is the third ticket failing
after two succeeded.

That case drives the whole design. A journey is confirmed only when every
required item is, and :meth:`JourneyBookingIntent.can_confirm` refuses
otherwise; partial failure is a first-class state that records what succeeded,
what failed and what was never attempted. No automatic rollback is modelled,
because airlines do not universally support it and inventing a rollback that
silently fails would be worse than admitting a human has to intervene.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .money import BASE_CURRENCY
from .provider_reference import ProviderOfferReference


class BookingState(str, Enum):
    """Where one item, or a whole journey, currently stands."""

    DRAFT = "DRAFT"
    REVALIDATING = "REVALIDATING"
    READY = "READY"
    USER_CONFIRMED = "USER_CONFIRMED"
    BOOKING = "BOOKING"
    CONFIRMED = "CONFIRMED"

    # --- things that went wrong, kept apart because they need different
    # --- responses from the traveller and from us
    PRICE_CHANGED = "PRICE_CHANGED"
    UNAVAILABLE = "UNAVAILABLE"
    EXPIRED = "EXPIRED"
    TIMEOUT = "TIMEOUT"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    """Some tickets exist and some do not. Never a success."""
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    """A person has to decide. Reached when money moved and the journey did
    not complete - the one state that must never resolve itself silently."""
    FAILED = "FAILED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    """Never tried, because something earlier stopped the run. Distinct from
    FAILED: nothing was sent, so nothing needs undoing."""


#: The only transitions the domain allows.
#:
#: Written as data rather than as scattered ``if`` statements so that "can this
#: become CONFIRMED?" has exactly one answer, in one place, that a test can
#: enumerate. A state machine spread across call sites is one where the illegal
#: transition is always in the branch nobody read.
ALLOWED_TRANSITIONS: dict[BookingState, frozenset[BookingState]] = {
    BookingState.DRAFT: frozenset({BookingState.REVALIDATING, BookingState.FAILED}),
    BookingState.REVALIDATING: frozenset({
        BookingState.READY, BookingState.PRICE_CHANGED, BookingState.UNAVAILABLE,
        BookingState.EXPIRED, BookingState.TIMEOUT, BookingState.PROVIDER_FAILURE,
        BookingState.FAILED,
    }),
    BookingState.READY: frozenset({
        BookingState.USER_CONFIRMED, BookingState.EXPIRED,
        BookingState.PRICE_CHANGED, BookingState.UNAVAILABLE, BookingState.FAILED,
    }),
    BookingState.USER_CONFIRMED: frozenset({
        BookingState.BOOKING, BookingState.EXPIRED, BookingState.FAILED,
    }),
    BookingState.BOOKING: frozenset({
        BookingState.CONFIRMED, BookingState.PARTIAL_FAILURE,
        BookingState.PROVIDER_FAILURE, BookingState.TIMEOUT,
        BookingState.UNAVAILABLE, BookingState.PRICE_CHANGED, BookingState.FAILED,
    }),
    # A price change or an expiry mid-flow sends the journey back to be
    # re-priced and re-confirmed. It never shortcuts to CONFIRMED, because the
    # traveller agreed to a number that is no longer the number.
    BookingState.PRICE_CHANGED: frozenset({
        BookingState.REVALIDATING, BookingState.USER_CONFIRMED, BookingState.FAILED,
    }),
    BookingState.EXPIRED: frozenset({BookingState.REVALIDATING, BookingState.FAILED}),
    BookingState.TIMEOUT: frozenset({
        BookingState.REVALIDATING, BookingState.RECOVERY_REQUIRED, BookingState.FAILED,
    }),
    BookingState.UNAVAILABLE: frozenset({BookingState.REVALIDATING, BookingState.FAILED}),
    BookingState.PROVIDER_FAILURE: frozenset({
        BookingState.REVALIDATING, BookingState.RECOVERY_REQUIRED, BookingState.FAILED,
    }),
    # Terminal until a person acts. Deliberately cannot reach CONFIRMED.
    BookingState.PARTIAL_FAILURE: frozenset({BookingState.RECOVERY_REQUIRED}),
    BookingState.RECOVERY_REQUIRED: frozenset({BookingState.FAILED}),
    BookingState.CONFIRMED: frozenset(),
    BookingState.FAILED: frozenset(),
    BookingState.NOT_ATTEMPTED: frozenset({BookingState.DRAFT, BookingState.FAILED}),
}

#: States in which a ticket may exist at a provider and money may have moved.
SETTLED_STATES = frozenset({BookingState.CONFIRMED})


class InvalidTransition(ValueError):
    """A state change the domain forbids.

    Raised rather than logged. The transition this exists to stop is a journey
    reaching CONFIRMED while one of its flights did not, and a warning in a log
    file is not a defence against that.
    """


def can_transition(current: BookingState, target: BookingState) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


class PriceTolerance(BaseModel):
    """How much the price may move before the traveller must agree again.

    Exists so that a two-euro tax rounding does not force somebody back through
    a confirmation screen, while a fifty-euro jump does. Both bounds are
    checked: a percentage alone is too loose on an expensive trip and too tight
    on a cheap one.
    """

    model_config = ConfigDict(frozen=True)

    absolute: float = Field(default=0.0, ge=0.0)
    percentage: float = Field(default=0.0, ge=0.0, le=100.0)

    def accepts(self, quoted: float, revalidated: float) -> bool:
        """Whether this movement can proceed without asking again.

        A price that went **down** always passes: nobody needs re-consent to be
        charged less. Only increases are measured.
        """
        if revalidated <= quoted:
            return True
        increase = revalidated - quoted
        allowed = max(self.absolute, quoted * self.percentage / 100.0)
        return increase <= allowed + 1e-9


class BookingItem(BaseModel):
    """One ticket that has to be bought for the journey to happen.

    Provider-neutral: it carries a :class:`ProviderOfferReference`, not a
    Duffel offer. Which system sells it is the booking layer's business.
    """

    model_config = ConfigDict(frozen=True)

    item_id: str = Field(min_length=1, max_length=200)
    provider_ref: ProviderOfferReference
    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    travelers: int = Field(ge=1)
    quoted_price: float = Field(ge=0.0)
    currency: str = BASE_CURRENCY
    state: BookingState = BookingState.DRAFT
    required: bool = True
    """Whether the journey is impossible without it.

    Every flight is required. The flag exists so a future optional extra - a
    seat, a bag, a transfer - can fail without destroying a journey that is
    otherwise complete.
    """
    detail: str = ""
    """What happened, for a human reading a failure. Never provider payload."""

    def with_state(self, target: BookingState, *, detail: str = "") -> "BookingItem":
        if not can_transition(self.state, target):
            raise InvalidTransition(
                f"{self.item_id}: {self.state.value} -> {target.value} is not allowed"
            )
        return self.model_copy(update={"state": target, "detail": detail})

    @property
    def is_settled(self) -> bool:
        return self.state in SETTLED_STATES


class RevalidationResult(BaseModel):
    """What re-checking every offer found, immediately before confirming."""

    model_config = ConfigDict(frozen=True)

    original_total: float = Field(ge=0.0)
    revalidated_total: float = Field(ge=0.0)
    currency: str = BASE_CURRENCY
    items_changed: tuple[str, ...] = ()
    items_unavailable: tuple[str, ...] = ()
    items_expired: tuple[str, ...] = ()
    checked_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    expires_at: datetime | None = None

    @property
    def absolute_delta(self) -> float:
        return round(self.revalidated_total - self.original_total, 2)

    @property
    def percentage_delta(self) -> float:
        if self.original_total <= 0:
            return 0.0
        return round(self.absolute_delta / self.original_total * 100.0, 4)

    @property
    def is_bookable(self) -> bool:
        """Whether every part of the journey can still be bought at all.

        Price is a separate question, answered by :class:`PriceTolerance`. This
        one is about existence.
        """
        return not self.items_unavailable and not self.items_expired


class JourneyBookingIntent(BaseModel):
    """One journey the traveller confirms once, and the tickets beneath it."""

    model_config = ConfigDict(frozen=True)

    journey_id: str = Field(min_length=1, max_length=200)
    trip_id: str = ""
    items: tuple[BookingItem, ...] = ()
    quoted_total: float = Field(default=0.0, ge=0.0)
    currency: str = BASE_CURRENCY
    travelers: int = Field(default=1, ge=1)
    state: BookingState = BookingState.DRAFT
    price_tolerance: PriceTolerance = Field(default_factory=PriceTolerance)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _check_items(self) -> "JourneyBookingIntent":
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("booking items must have distinct ids")
        return self

    @property
    def expires_at(self) -> datetime | None:
        """When the first of these offers dies.

        The journey is only as bookable as its shortest-lived ticket, so the
        earliest expiry governs - not the latest, and not an average.
        """
        deadlines = [
            item.provider_ref.expires_at
            for item in self.items
            if item.provider_ref.expires_at is not None
        ]
        return min(deadlines) if deadlines else None

    @property
    def required_items(self) -> tuple[BookingItem, ...]:
        return tuple(item for item in self.items if item.required)

    @property
    def can_confirm(self) -> bool:
        """Whether this journey may legitimately become CONFIRMED.

        The invariant the whole module exists for: **every required item must
        be settled**. One unbooked flight means the traveller cannot make the
        trip, however many of the others succeeded, and calling that CONFIRMED
        would be the single worst thing this system could tell somebody.
        """
        return bool(self.required_items) and all(
            item.is_settled for item in self.required_items
        )

    @property
    def outcome(self) -> BookingState:
        """What actually happened, derived from the items rather than asserted.

        Deliberately computed. A journey-level flag that could drift from its
        own tickets is how a partial failure gets reported as a success.
        """
        required = self.required_items
        if not required:
            return BookingState.FAILED
        settled = [item for item in required if item.is_settled]
        if len(settled) == len(required):
            return BookingState.CONFIRMED
        if settled:
            # Money moved and the journey did not complete. A person decides.
            return BookingState.PARTIAL_FAILURE
        return BookingState.FAILED

    def with_state(self, target: BookingState) -> "JourneyBookingIntent":
        """Move the journey, refusing anything the domain forbids.

        There is deliberately no ``force`` escape hatch. An override on this
        method would exist precisely to mark a journey CONFIRMED when its
        tickets say otherwise, which is the one outcome this type is built to
        make impossible.
        """
        if not can_transition(self.state, target):
            raise InvalidTransition(
                f"{self.journey_id}: {self.state.value} -> {target.value} is not allowed"
            )
        if target is BookingState.CONFIRMED and not self.can_confirm:
            unsettled = [
                item.item_id for item in self.required_items if not item.is_settled
            ]
            raise InvalidTransition(
                f"{self.journey_id}: cannot confirm a journey whose required "
                f"items are not all booked: {unsettled}"
            )
        return self.model_copy(update={"state": target})

    def with_items(self, items: tuple[BookingItem, ...]) -> "JourneyBookingIntent":
        return self.model_copy(update={"items": items})

    def settled_items(self) -> tuple[BookingItem, ...]:
        return tuple(item for item in self.items if item.is_settled)

    def failed_items(self) -> tuple[BookingItem, ...]:
        return tuple(
            item
            for item in self.items
            if item.state in (
                BookingState.FAILED, BookingState.PROVIDER_FAILURE,
                BookingState.TIMEOUT, BookingState.UNAVAILABLE, BookingState.EXPIRED,
            )
        )

    def unattempted_items(self) -> tuple[BookingItem, ...]:
        """Never sent anywhere - so nothing needs undoing for these."""
        return tuple(
            item
            for item in self.items
            if item.state in (BookingState.NOT_ATTEMPTED, BookingState.DRAFT,
                              BookingState.READY, BookingState.USER_CONFIRMED)
        )
