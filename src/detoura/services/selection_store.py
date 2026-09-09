"""Server-side memory of what a traveller selected, so booking can revalidate it (V8 Phase 3).

The engine keeps a Duffel offer id on every real `TransportOption`, but the
product API deliberately never sends it to a client - "ids are internal and
never reach a client" (`api/v1.py`). So a client cannot hand an offer id back
to be revalidated. It hands back a *selection id* instead, and the server looks
up what it recorded.

This is the smallest store that makes that work: an in-memory, TTL-bounded,
size-bounded map from an opaque `selection_id` to the offers behind one
recommendation, with the discovered price and terms captured at search time.
Deliberately not the session store - that one is shaped around personalization
weights - and deliberately not Redis: a selection is valid only as long as its
offers are (Duffel offers die in ~30 minutes), so a process-local store with a
short TTL is the right size. A multi-worker deployment that needs this shared
can implement the same tiny interface over Redis, exactly as the session store
does.

What the store does **not** hold: anything the client could otherwise forge.
The discovered price and baggage live here because the server put them here at
search time, so revalidation compares against the server's own record, never
against a number in the revalidate request.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime

from ..models.baggage import BaggageStatus

#: Long enough to read a results page and pick a trip; shorter than a Duffel
#: offer's ~30-minute life, so a selection never outlives what it points at.
DEFAULT_SELECTION_TTL_SECONDS = 20 * 60

#: Process-local, so it needs a ceiling. Sized for a single worker serving many
#: concurrent searches, not for scale.
DEFAULT_MAX_SELECTIONS = 5_000


@dataclass(frozen=True, slots=True)
class SelectedOffer:
    """One bookable offer inside a selected recommendation, as discovered.

    `discovered_*` is the server's own record of what it showed the traveller.
    Revalidation re-fetches `offer_id` and compares the two.
    """

    offer_id: str
    provider: str
    origin: str
    destination: str
    leg_label: str
    travelers: int
    discovered_amount: float
    """Per the recommendation's currency (base), what the traveller was shown."""
    discovered_currency: str
    discovered_raw_amount: str | None = None
    """The provider's own decimal string, so a currency change is detectable."""
    discovered_raw_currency: str | None = None
    discovered_baggage_cabin: BaggageStatus | None = None
    discovered_baggage_checked: BaggageStatus | None = None
    discovered_hold_supported: bool | None = None
    discovered_expires_at: str | None = None
    discovered_departure: datetime | None = None
    discovered_arrival: datetime | None = None
    required: bool = True


@dataclass(frozen=True, slots=True)
class Selection:
    """Everything a `/revalidate` call needs, keyed by one opaque id."""

    selection_id: str
    recommendation_id: str
    trip_label: str
    currency: str
    discovered_total: float
    offers: tuple[SelectedOffer, ...]
    created_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class _Entry:
    selection: Selection
    expires_at: float


class SelectionStore:
    """In-memory, TTL- and size-bounded. Thread-safe for a single process."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_SELECTION_TTL_SECONDS,
        max_selections: int = DEFAULT_MAX_SELECTIONS,
        clock=time.monotonic,
        id_factory=lambda: "sel_" + secrets.token_urlsafe(18),
    ) -> None:
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._ttl = ttl_seconds
        self._max = max_selections
        self._clock = clock
        self._id_factory = id_factory
        self._lock = threading.Lock()

    def record(
        self,
        *,
        recommendation_id: str,
        trip_label: str,
        currency: str,
        discovered_total: float,
        offers: tuple[SelectedOffer, ...],
    ) -> str:
        """Store one recommendation's offers, return its selection id."""
        selection_id = self._id_factory()
        now = self._clock()
        entry = _Entry(
            selection=Selection(
                selection_id=selection_id,
                recommendation_id=recommendation_id,
                trip_label=trip_label,
                currency=currency,
                discovered_total=discovered_total,
                offers=tuple(offers),
            ),
            expires_at=now + self._ttl,
        )
        with self._lock:
            self._entries[selection_id] = entry
            self._entries.move_to_end(selection_id)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
        return selection_id

    def get(self, selection_id: str) -> Selection | None:
        """The recorded selection, or ``None`` if unknown or expired.

        An expired selection is dropped and returns ``None`` - the same as one
        that was never recorded, because the offers behind it are dead too.
        """
        now = self._clock()
        with self._lock:
            entry = self._entries.get(selection_id)
            if entry is None:
                return None
            if entry.expires_at <= now:
                del self._entries[selection_id]
                return None
            self._entries.move_to_end(selection_id)
            return entry.selection

    def purge_expired(self) -> int:
        now = self._clock()
        with self._lock:
            dead = [k for k, e in self._entries.items() if e.expires_at <= now]
            for key in dead:
                del self._entries[key]
        return len(dead)

    def __len__(self) -> int:
        return len(self._entries)


#: Process-wide default. The API layer uses this one; tests build their own.
_STORE: SelectionStore | None = None


def selection_store() -> SelectionStore:
    global _STORE
    if _STORE is None:
        ttl = os.getenv("DETOURA_SELECTION_TTL_SECONDS")
        _STORE = SelectionStore(
            ttl_seconds=float(ttl) if ttl else DEFAULT_SELECTION_TTL_SECONDS
        )
    return _STORE
