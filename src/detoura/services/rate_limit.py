"""A small, generic abuse/rate-limit primitive (V9 Phase 2.6 §A7; bounded
and made process-local-honest in V9 Phase 6 slice 1).

A fixed-window counter per ``(scope, key)``, in-memory: correct for the
single-process deployment this project already defaults to elsewhere (see
:mod:`detoura.services.session_store`'s ``InMemorySessionStore`` for the
same reasoning), with the same shape a Redis-backed version could later
drop in behind. **This is process-local.** Running more than one API worker
process means each process enforces its own independent budget - real for
this project's current single-process deployment target (see
``render.yaml``), but the seam to a shared store already exists in the
constructor signature below (swap this class for one backed by Redis
``INCR``/``EXPIRE`` without touching a single call site) should the
deployment ever grow multi-process before a shared store is wired.

Deliberately simple: this is abuse-control, not a precise SLA - a fixed
window undercounts slightly at window boundaries compared to a sliding
window, which only ever makes it *more* permissive, never a security hole.

Bounded: the window dict is capped at ``max_entries``. An LRU-style
``OrderedDict`` evicts the least-recently-touched key once at capacity, and
a best-effort periodic sweep proactively drops naturally-expired windows so
ordinary traffic essentially never reaches the hard cap. Both are safety
nets against a high-cardinality flood of distinct keys (e.g. one email or
one spoofed identifier per request) turning this into a memory-exhaustion
vector - the *count* budget per key is the actual abuse control; the cap
below only bounds what an attacker can make this dict cost to hold.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

#: How often (in the caller's `now` clock - monotonic seconds in
#: production) an `allow()` call may trigger a proactive sweep of
#: naturally-expired windows. A sweep is an O(n) scan of the tracked keys,
#: so this keeps its amortized cost negligible without adding a background
#: thread to a process that otherwise has none.
DEFAULT_SWEEP_INTERVAL_SECONDS = 30.0

#: Hard ceiling on tracked (scope, key) pairs, process-wide. Generous for
#: real traffic; bounded against an attacker who tries to grow this dict
#: without limit by cycling through unique identifiers (emails, spoofed
#: "IPs", etc). Once at capacity, the least-recently-touched entry is
#: evicted to make room for a new one - functionally a bounded LRU cache,
#: never an unbounded dict.
DEFAULT_MAX_ENTRIES = 50_000


@dataclass
class _Window:
    window_start: float
    window_seconds: float
    count: int = 0

    def expired_at(self, now: float) -> bool:
        return now - self.window_start >= self.window_seconds


class RateLimiter:
    """``allow(scope, key)`` — ``True`` if this call may proceed under the
    configured ``max_calls`` per ``window_seconds``. A denied call does not
    increment the counter — it must not itself count against a *future*
    window's budget."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        sweep_interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._windows: OrderedDict[tuple[str, str], _Window] = OrderedDict()
        self._max_entries = max_entries
        self._sweep_interval_seconds = sweep_interval_seconds
        self._last_sweep = 0.0

    def allow(
        self, scope: str, key: str, *, max_calls: int, window_seconds: float,
        now: float | None = None,
    ) -> bool:
        now = now if now is not None else time.monotonic()
        k = (scope, key)
        with self._lock:
            self._maybe_sweep(now)
            w = self._windows.get(k)
            if w is None or w.expired_at(now):
                if w is None:
                    # Only a genuinely new key can grow the dict - refreshing
                    # an existing (even if expired) slot does not, so only
                    # this branch needs to make room.
                    self._evict_if_full()
                w = _Window(window_start=now, window_seconds=window_seconds, count=0)
                self._windows[k] = w
            self._windows.move_to_end(k)
            if w.count >= max_calls:
                return False
            w.count += 1
            return True

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep < self._sweep_interval_seconds:
            return
        self._last_sweep = now
        expired = [k for k, w in self._windows.items() if w.expired_at(now)]
        for k in expired:
            del self._windows[k]

    def _evict_if_full(self) -> None:
        while len(self._windows) >= self._max_entries:
            self._windows.popitem(last=False)  # oldest / least-recently-touched

    def reset(self, scope: str, key: str) -> None:
        """Clears one key's window — used after a successful action that
        should not keep counting against a limit meant for *failed*
        attempts (e.g. a successful login resets that (ip, email) pair's
        failed-login counter)."""
        with self._lock:
            self._windows.pop((scope, key), None)

    def clear(self) -> None:
        """Test-only: drop all state."""
        with self._lock:
            self._windows.clear()
            self._last_sweep = 0.0

    def size(self) -> int:
        """Test/observability hook: current tracked-key count."""
        with self._lock:
            return len(self._windows)


_LIMITER = RateLimiter()


def rate_limiter() -> RateLimiter:
    """Process-wide limiter instance."""
    return _LIMITER
