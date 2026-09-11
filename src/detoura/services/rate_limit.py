"""A small, generic abuse/rate-limit primitive (V9 Phase 2.6 §A7).

Detoura had no shared rate-limit infrastructure before this phase — this is
new, reusable infrastructure other endpoints can adopt later, not a
one-off bolted onto auth. A fixed-window counter per ``(scope, key)``,
in-memory: correct for the single-process deployment this project already
defaults to elsewhere (see :mod:`detoura.services.session_store`'s
``InMemorySessionStore`` for the same reasoning), with the same shape a
Redis-backed version could later drop in behind.

Deliberately simple: this is abuse-control, not a precise SLA — a fixed
window undercounts slightly at window boundaries compared to a sliding
window, which only ever makes it *more* permissive, never a security hole.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Window:
    window_start: float
    count: int = 0


class RateLimiter:
    """``allow(scope, key)`` — ``True`` if this call may proceed under the
    configured ``max_calls`` per ``window_seconds``, incrementing the
    counter either way is wrong (a denied call must not itself count against
    a *future* window's budget), so a denied call does not increment."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[tuple[str, str], _Window] = {}

    def allow(self, scope: str, key: str, *, max_calls: int, window_seconds: float,
              now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        k = (scope, key)
        with self._lock:
            w = self._windows.get(k)
            if w is None or now - w.window_start >= window_seconds:
                w = _Window(window_start=now, count=0)
                self._windows[k] = w
            if w.count >= max_calls:
                return False
            w.count += 1
            return True

    def reset(self, scope: str, key: str) -> None:
        """Clears one key's window — used after a successful action that
        should not keep counting against a limit meant for *failed*
        attempts (e.g. a successful login resets that email's failed-login
        counter)."""
        with self._lock:
            self._windows.pop((scope, key), None)

    def clear(self) -> None:
        """Test-only: drop all state."""
        with self._lock:
            self._windows.clear()


_LIMITER = RateLimiter()


def rate_limiter() -> RateLimiter:
    """Process-wide limiter instance."""
    return _LIMITER
