"""The Detoura Test Travel Pass (V8 Phase 4).

A provider-neutral artifact generated from the actual journey: the confirmed
`BookingItem`s, the traveller who was entered, the revalidated itinerary. Not a
mockup and not a fixture - every field here is derived from real state, so two
different journeys produce two different passes.

Three things this type keeps distinct, because collapsing them is the failure
the spec is most emphatic about:

1. **A Duffel Test Order** (`ord_...`) - a sandbox record at the provider.
2. **This pass** - Detoura's own artifact, marked TEST/DEMO.
3. **A real airline ticket** - which this build never issues.

`PassMode` says which of the first two actually happened:

- ``SANDBOX_BOOKED`` - a Duffel Test Order was created for every leg.
- ``DEMO_ONLY``      - no Duffel Order was created; the pass is built from the
  selected/revalidated journey data alone, and says so.

A pass is only ``ready`` when every required leg reached a confirmed state. A
journey with one failed leg produces a ``recovery_required`` pass, never a
successful one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .booking import BookingState


class PassMode(str, Enum):
    SANDBOX_BOOKED = "sandbox_booked"
    """A Duffel Test Mode Order was created for every leg."""
    DEMO_ONLY = "demo_only"
    """No Duffel Order was created. The pass is data-driven from the journey."""


class PassStatus(str, Enum):
    READY = "ready"
    """Every required leg confirmed. The pass is a (test) success."""
    RECOVERY_REQUIRED = "recovery_required"
    """Some legs confirmed and some did not. Not a success."""
    FAILED = "failed"
    """Nothing was confirmed."""


class PassTicket(BaseModel):
    """One leg on the pass, from one `BookingItem`."""

    model_config = ConfigDict(frozen=True)

    sequence: int
    origin_city: str
    origin_airport: str
    destination_city: str
    destination_airport: str
    departure: datetime
    arrival: datetime
    carrier: str = ""
    flight_number: str = ""
    cabin_baggage: str = "unknown"
    checked_baggage: str = "unknown"
    price_per_person: float
    currency: str = "EUR"
    booking_state: BookingState
    provider_order_id: str | None = None
    """The Duffel Test Order id for this leg, when one was created. Shown only
    in a labelled technical section - never as a ticket number."""

    @property
    def confirmed(self) -> bool:
        return self.booking_state is BookingState.CONFIRMED


class PassDisclaimer(BaseModel):
    model_config = ConfigDict(frozen=True)

    test_mode: bool = True
    valid_boarding_pass: bool = False
    payment_collected: bool = False
    note: str = "Detoura test journey. Not a valid boarding pass. No payment collected."


class DetouraTravelPass(BaseModel):
    """Everything the success screen renders, owned by the server."""

    model_config = ConfigDict(frozen=True)

    journey_reference: str
    """`DTR-V8-...`. Server-generated, never derived from PII or secrets."""
    booking_id: str
    mode: PassMode
    status: PassStatus

    traveler_name: str
    """Lead traveller's full name. The only PII on the pass."""
    party_size: int

    route_cities: tuple[str, ...]
    """`("Cologne", "Prague", "Vienna", "Cologne")` - for the big visual."""
    travel_dates: tuple[str, ...]
    tickets: tuple[PassTicket, ...]

    trip_total: float
    currency: str = "EUR"
    baggage_complete: bool = False
    unknowns: tuple[str, ...] = ()

    provider_order_ids: tuple[str, ...] = ()
    """All Duffel Test Order ids, for the technical detail section. Empty in
    DEMO_ONLY."""
    disclaimer: PassDisclaimer = Field(default_factory=PassDisclaimer)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def tickets_prepared(self) -> int:
        return sum(1 for t in self.tickets if t.confirmed)
