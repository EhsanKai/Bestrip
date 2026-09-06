"""Where a session's personalization state lives (V6.5).

The problem this solves is a deployment one, not a modelling one. Beam search
is CPU-bound pure Python, so concurrent searches serialize on the GIL and only
separate worker *processes* run them in parallel - measured at 2.4x throughput
across four workers. But the session state was a module-level dict, which is
per-process, so four workers meant four disagreeing copies of one traveller's
profile. A twelve-signal session reported ``[1,1,1,2,3,4,5,2,6,3,7,8]``.

So the store is an interface, and the concurrency guarantee lives inside it
rather than in the caller:

    update(session_id, mutate)

``mutate`` is a pure function from the current profile (or ``None``) to the
next one. The store is responsible for applying it atomically. That is the
whole design: a caller cannot get the locking wrong, because a caller never
does the locking. Read-compute-write, the shape that loses updates, is not
expressible through this API.

Two implementations:

* :class:`InMemorySessionStore` - the default. Correct for a single worker,
  and what tests and local development use. Requires no service.
* :class:`RedisSessionStore` - shared across processes and hosts, with expiry
  handled by Redis itself. Opt-in, because the product's deployment promise is
  one image and no configuration; a store that demanded a second service by
  default would break that for every user who does not need it.

Redis rather than a relational store because this repository has no database
infrastructure at all - a session is a small, expiring, key-addressed blob,
which is the exact shape Redis is for, and a schema plus migrations would be
disproportionate to it.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from ..profiles import ProfileName, TravelValueWeights

#: Default lifetime of an idle session.
DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 14

#: Ceiling for the in-memory store. Redis is bounded by its own eviction
#: policy and by TTL, so this applies only to the process-local one.
DEFAULT_MAX_SESSIONS = 10_000

#: How many times an optimistic update retries before giving up.
#:
#: Sized from how WATCH/MULTI/EXEC actually behaves, not from intuition. Each
#: round exactly one contending writer commits and the rest abort, so a writer
#: racing N others can lose up to N-1 rounds. A handful of attempts therefore
#: fails not under pathological load but under ordinary burst load - measured:
#: 48 concurrent signals to one session exhausted a budget of 8 immediately.
#:
#: Real traffic contends at 1-2 writers per session (one person clicking), so
#: this ceiling exists for correctness under test and burst, not for the
#: common case, where the first attempt almost always commits.
MAX_UPDATE_ATTEMPTS = 64

#: Backoff between contended attempts. Without it, aborted writers retry in
#: lockstep and collide again - the retry itself becomes the contention.
#: Full jitter spreads them; the cap keeps worst-case latency bounded.
RETRY_BASE_SECONDS = 0.002
RETRY_MAX_SECONDS = 0.040


class SessionStoreError(RuntimeError):
    """The store could not complete an operation."""


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
# Kept here rather than on the profile so the domain object stays unaware of
# where it is stored. Deliberately explicit rather than pickling: the bytes go
# into a shared service that other versions of this application will read.

def encode(profile) -> str:
    return json.dumps(
        {
            "session_id": profile.session_id,
            "declared_profile": profile.declared_profile.value,
            "declared": profile.declared.model_dump(),
            "observed": profile.observed.model_dump(),
            "baseline": profile.baseline.model_dump(),
            "signal_count": profile.signal_count,
            "last_action": (
                profile.last_action.value if profile.last_action else None
            ),
        },
        sort_keys=True,
    )


def decode(raw: str):
    from .feedback import FeedbackAction, SessionProfile

    data = json.loads(raw)
    return SessionProfile(
        session_id=data["session_id"],
        declared_profile=ProfileName(data["declared_profile"]),
        declared=TravelValueWeights(**data["declared"]),
        observed=TravelValueWeights(**data["observed"]),
        baseline=TravelValueWeights(**data["baseline"]),
        signal_count=data["signal_count"],
        last_action=(
            FeedbackAction(data["last_action"]) if data["last_action"] else None
        ),
    )


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------
@runtime_checkable
class SessionStore(Protocol):
    """Where session personalization state is kept."""

    def get(self, session_id: str):
        """The stored profile, or ``None`` if unknown or expired."""

    def put(self, session_id: str, profile) -> None:
        """Store a profile, replacing any existing one, and refresh its TTL."""

    def update(self, session_id: str, mutate: Callable):
        """Apply ``mutate`` to the current profile atomically.

        ``mutate`` receives the current profile or ``None`` and returns the
        next one. It may be called more than once if another writer wins a
        race, so it must be pure - no side effects, no I/O.
        """

    def delete(self, session_id: str) -> None:
        """Forget a session. Absent sessions are not an error."""

    def cleanup_expired(self) -> int:
        """Drop expired entries; return how many were removed."""


# ---------------------------------------------------------------------------
# In memory
# ---------------------------------------------------------------------------
@dataclass
class _Entry:
    profile: object
    expires_at: float


class InMemorySessionStore:
    """Process-local store. The default, and correct for a single worker.

    Bounded and locked. The bound matters because the key is client-controlled
    and an absent session id mints a new one per request, so unbounded growth
    is reachable from ordinary traffic. The lock matters because sync route
    handlers run concurrently in a threadpool.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()

    # -- internals ---------------------------------------------------------
    def _live(self, session_id: str) -> _Entry | None:
        entry = self._entries.get(session_id)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[session_id]
            return None
        self._entries.move_to_end(session_id)
        return entry

    def _store(self, session_id: str, profile) -> None:
        self._entries[session_id] = _Entry(profile, self._clock() + self._ttl)
        self._entries.move_to_end(session_id)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    # -- interface ---------------------------------------------------------
    def get(self, session_id: str):
        with self._lock:
            entry = self._live(session_id)
            return entry.profile if entry else None

    def put(self, session_id: str, profile) -> None:
        with self._lock:
            self._store(session_id, profile)

    def update(self, session_id: str, mutate: Callable):
        # The lock spans read, compute and write. Holding it across only the
        # two dict operations would still lose the update, because the value
        # written is derived from the value read.
        with self._lock:
            entry = self._live(session_id)
            updated = mutate(entry.profile if entry else None)
            self._store(session_id, updated)
            return updated

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._entries.pop(session_id, None)

    def cleanup_expired(self) -> int:
        now = self._clock()
        with self._lock:
            dead = [k for k, e in self._entries.items() if e.expires_at <= now]
            for key in dead:
                del self._entries[key]
            return len(dead)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
class RedisSessionStore:
    """Shared across worker processes and hosts.

    Atomicity uses WATCH/MULTI/EXEC rather than a lock. An optimistic
    transaction aborts if the key changed while we were computing, and we
    recompute from the new value - so a concurrent writer causes a retry, not
    a lost signal, and no process ever holds a lock it could die inside.

    Expiry is Redis's own: every write sets a TTL, so an idle session is
    reclaimed without this application sweeping anything.
    """

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        namespace: str = "detoura:session:",
        client=None,
    ) -> None:
        self._ttl = ttl_seconds
        self._ns = namespace
        if client is not None:
            self._redis = client
            return
        try:
            import redis  # imported lazily: an optional dependency
        except ImportError as error:  # pragma: no cover - environment specific
            raise SessionStoreError(
                "RedisSessionStore needs the 'redis' package: "
                "pip install 'detoura[session]'"
            ) from error
        self._redis = redis.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=5
        )
        # Fail fast, at construction.
        #
        # redis-py connects lazily, so without this a deployment that asked
        # for a shared store and cannot reach one starts perfectly happily and
        # then fails on every request that touches a session. A process that
        # cannot honour the coherence it was configured for should refuse to
        # start, not serve degraded personalization while looking healthy.
        try:
            self._redis.ping()
        except Exception as error:  # noqa: BLE001
            raise SessionStoreError(
                f"session store is configured for Redis at {url!r} but it is "
                f"not reachable: {error}"
            ) from error

    def _key(self, session_id: str) -> str:
        return f"{self._ns}{session_id}"

    def get(self, session_id: str):
        key = self._key(session_id)
        try:
            raw = self._redis.get(key)
        except Exception as error:  # noqa: BLE001
            # One error contract across the interface. Without this a caller
            # would have to catch SessionStoreError from update() and a raw
            # redis exception from get(), which is how outage handling gets
            # missed.
            raise SessionStoreError(f"session read failed: {error}") from error
        if raw is None:
            return None
        # An empty value is corruption, not absence: encode() never produces
        # one. Conflating the two left the junk key in place to be re-read
        # forever, so the two cases are separated here.
        try:
            return decode(raw)
        except Exception:  # noqa: BLE001
            # A corrupt entry is treated as absent and removed.
            #
            # Raising would leave the traveller permanently unable to use the
            # endpoint until an operator intervened, for state that is already
            # unrecoverable. Deleting means the next signal starts a fresh
            # session and the bad value cannot poison later reads. Personalization
            # resets, which is a small and self-correcting harm; the alternative
            # is an unexplained 500 loop.
            try:
                self._redis.delete(key)
            except Exception:  # noqa: BLE001
                pass
            return None

    def put(self, session_id: str, profile) -> None:
        try:
            self._redis.set(self._key(session_id), encode(profile), ex=self._ttl)
        except Exception as error:  # noqa: BLE001
            raise SessionStoreError(f"session write failed: {error}") from error

    def update(self, session_id: str, mutate: Callable):
        key = self._key(session_id)
        for attempt in range(MAX_UPDATE_ATTEMPTS):
            with self._redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    try:
                        current = decode(raw) if raw else None
                    except Exception:  # noqa: BLE001
                        current = None  # same self-healing rule as get()
                    updated = mutate(current)
                    pipe.multi()
                    pipe.set(key, encode(updated), ex=self._ttl)
                    pipe.execute()
                    return updated
                except Exception as error:  # noqa: BLE001
                    # redis.WatchError means someone else wrote first; anything
                    # else is a real fault. Compared by name so this module
                    # does not import redis at definition time.
                    if type(error).__name__ != "WatchError":
                        raise SessionStoreError(str(error)) from error
            # Full jitter: sleep anywhere in [0, capped exponential]. Sleeping
            # the *same* backoff in every loser would just re-synchronise the
            # collision we are backing off from.
            ceiling = min(RETRY_BASE_SECONDS * (2 ** attempt), RETRY_MAX_SECONDS)
            time.sleep(random.uniform(0.0, ceiling))
        raise SessionStoreError(
            f"session {session_id!r} stayed contended for "
            f"{MAX_UPDATE_ATTEMPTS} attempts; a writer is likely stuck"
        )

    def delete(self, session_id: str) -> None:
        try:
            self._redis.delete(self._key(session_id))
        except Exception as error:  # noqa: BLE001
            raise SessionStoreError(f"session delete failed: {error}") from error

    def cleanup_expired(self) -> int:
        # Redis expires keys itself; there is nothing for us to sweep. Zero is
        # the honest answer, not a stub - no entry was removed *by us*.
        return 0


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def declared_workers() -> int:
    """How many worker processes this deployment says it runs.

    Read from the environment rather than detected, because a uvicorn worker
    child has no reliable in-process signal that it is one of several. This is
    the honest limitation of the guard below: it protects the documented and
    tooled path (the Dockerfile exports WEB_CONCURRENCY alongside --workers),
    and it cannot catch someone passing --workers by hand with no environment
    variable set. That case is called out in DEPLOY.md.
    """
    for name in ("DETOURA_WORKERS", "WEB_CONCURRENCY"):
        raw = os.getenv(name, "").strip()
        if raw:
            try:
                return max(1, int(raw))
            except ValueError:
                continue
    return 1


def store_from_env() -> SessionStore:
    """Build the store this deployment is configured for.

    In-memory by default. The product ships as one image that needs no
    configuration, and demanding a Redis for a single-worker deployment that
    does not need one would break that promise for everybody.

    Set ``DETOURA_SESSION_STORE=redis`` (and ``DETOURA_REDIS_URL``) when
    running more than one worker, where a process-local store is not merely
    suboptimal but wrong.
    """
    kind = os.getenv("DETOURA_SESSION_STORE", "memory").strip().lower()
    ttl = int(os.getenv("DETOURA_SESSION_TTL_SECONDS", DEFAULT_TTL_SECONDS))
    if kind in ("", "memory", "inmemory", "local"):
        workers = declared_workers()
        if workers > 1:
            # Refuse to start rather than serve silent incoherence.
            #
            # Measured: two workers on a process-local store answered twelve
            # signals to one session with [1,2,1,2,3,3,4,5,6,7]. Nothing
            # errors and nothing looks wrong - the numbers are simply not the
            # traveller's. A deployment cannot notice that, so it must not be
            # allowed to happen quietly.
            raise SessionStoreError(
                f"{workers} workers are configured but the session store is "
                "process-local, which makes personalization incoherent across "
                "them. Set DETOURA_SESSION_STORE=redis (and DETOURA_REDIS_URL), "
                "or run a single worker."
            )
        return InMemorySessionStore(ttl_seconds=ttl)
    if kind == "redis":
        url = os.getenv("DETOURA_REDIS_URL", "redis://localhost:6379/0")
        return RedisSessionStore(url, ttl_seconds=ttl)
    raise SessionStoreError(
        f"unknown DETOURA_SESSION_STORE {kind!r}; expected 'memory' or 'redis'"
    )
