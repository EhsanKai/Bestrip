"""Shared session state across worker processes (V6.5).

The measurement that forced this: beam search is CPU-bound pure Python, so
concurrency only comes from worker *processes* (4 workers gave 2.4x
throughput). But session state was a module-level dict, which is per-process,
so four workers held four disagreeing copies of one traveller's profile.
Twelve sequential signals to one session reported:

    [1, 1, 1, 2, 3, 4, 5, 2, 6, 3, 7, 8]

Both store implementations are held to the same contract below, because the
whole point of the interface is that they are interchangeable. The tests that
genuinely need shared state across processes are marked and skip cleanly when
no Redis is reachable - but they are written to run, not to be decorative.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.profiles import COMPONENTS, ProfileName  # noqa: E402
from detoura.services import feedback  # noqa: E402
from detoura.services.feedback import (  # noqa: E402
    FeedbackAction,
    configure_sessions,
    get_session,
    record_feedback,
)
from detoura.services.session_store import (  # noqa: E402
    InMemorySessionStore,
    RedisSessionStore,
    SessionStore,
    decode,
    encode,
)

REDIS_URL = os.getenv("DETOURA_TEST_REDIS_URL", "redis://localhost:6379/15")
EVEN = {name: 1.0 / len(COMPONENTS) for name in COMPONENTS}
CHEAP = {**EVEN, "cost": 0.5}


def redis_available() -> bool:
    try:
        import redis

        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


needs_redis = pytest.mark.skipif(
    not redis_available(), reason="no Redis reachable at DETOURA_TEST_REDIS_URL"
)


# ---------------------------------------------------------------------------
# Both implementations, one contract
# ---------------------------------------------------------------------------
@pytest.fixture(params=["memory", "redis"])
def store(request) -> SessionStore:
    if request.param == "memory":
        yield InMemorySessionStore(ttl_seconds=60)
        return
    if not redis_available():
        pytest.skip("no Redis reachable")
    import redis

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield RedisSessionStore(REDIS_URL, ttl_seconds=60, client=client)
    client.flushdb()


@pytest.fixture
def wired(store):
    """Point the feedback service at the store under test."""
    previous = configure_sessions(store)
    yield store
    configure_sessions(previous)


def test_a_missing_session_is_none_not_an_error(store):
    assert store.get("never-seen") is None


def test_what_goes_in_comes_back(wired, store):
    written = record_feedback("s1", FeedbackAction.SAVED, EVEN)
    read = store.get("s1")

    assert read is not None
    assert read.signal_count == written.signal_count == 1
    assert read.observed == written.observed


def test_delete_forgets_a_session(wired, store):
    record_feedback("s1", FeedbackAction.SAVED, EVEN)
    store.delete("s1")

    assert store.get("s1") is None


def test_deleting_an_unknown_session_is_not_an_error(store):
    store.delete("never-seen")


def test_a_profile_survives_the_round_trip_exactly(wired):
    """Serialization must not quietly lose or round a weight."""
    original = record_feedback("s1", FeedbackAction.LIKED, CHEAP)
    restored = decode(encode(original))

    assert restored == original


# ---------------------------------------------------------------------------
# Repeated feedback and learning
# ---------------------------------------------------------------------------
def test_repeated_feedback_accumulates(wired):
    for _ in range(5):
        record_feedback("s1", FeedbackAction.SAVED, CHEAP)

    assert get_session("s1").signal_count == 5


def test_repeated_feedback_moves_the_learned_profile(wired):
    before = get_session("s1").observed.cost
    for _ in range(6):
        record_feedback("s1", FeedbackAction.SAVED, CHEAP)
    after = get_session("s1").observed.cost

    assert after > before, "saving cheap trips should raise the cost weight"


def test_explicit_preference_is_not_the_learned_one(wired):
    """`declared` moves only when the traveller says so, never from a click."""
    session = record_feedback(
        "s1", FeedbackAction.SAVED, CHEAP, declared_profile=ProfileName.CHEAPEST
    )
    declared_before = session.declared

    for _ in range(8):
        record_feedback("s1", FeedbackAction.SAVED, EVEN)
    after = get_session("s1")

    assert after.declared == declared_before, "clicks must not move the declaration"
    assert after.declared_profile is ProfileName.CHEAPEST
    assert after.observed != after.declared, "the learned profile did move"


def test_changing_the_declared_profile_is_respected(wired):
    record_feedback("s1", FeedbackAction.SAVED, EVEN,
                    declared_profile=ProfileName.CHEAPEST)
    updated = record_feedback("s1", FeedbackAction.SAVED, EVEN,
                              declared_profile=ProfileName.ADVENTURE)

    assert updated.declared_profile is ProfileName.ADVENTURE


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------
def test_two_travellers_never_share_state(wired):
    for _ in range(4):
        record_feedback("alice", FeedbackAction.SAVED, CHEAP)
    record_feedback("bob", FeedbackAction.DISLIKED, EVEN)

    assert get_session("alice").signal_count == 4
    assert get_session("bob").signal_count == 1
    assert get_session("alice").observed != get_session("bob").observed


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------
def test_an_idle_session_expires():
    clock = {"t": 1000.0}
    store = InMemorySessionStore(ttl_seconds=10, clock=lambda: clock["t"])
    previous = configure_sessions(store)
    try:
        record_feedback("s1", FeedbackAction.SAVED, EVEN)
        assert store.get("s1") is not None

        clock["t"] += 11
        assert store.get("s1") is None, "an idle session must not live forever"
    finally:
        configure_sessions(previous)


def test_activity_refreshes_the_lifetime():
    clock = {"t": 1000.0}
    store = InMemorySessionStore(ttl_seconds=10, clock=lambda: clock["t"])
    previous = configure_sessions(store)
    try:
        record_feedback("s1", FeedbackAction.SAVED, EVEN)
        clock["t"] += 8
        record_feedback("s1", FeedbackAction.SAVED, EVEN)  # touch
        clock["t"] += 8
        assert store.get("s1") is not None, "an active session must survive"
    finally:
        configure_sessions(previous)


def test_cleanup_reports_what_it_removed():
    clock = {"t": 1000.0}
    store = InMemorySessionStore(ttl_seconds=10, clock=lambda: clock["t"])
    for i in range(5):
        store.put(f"s{i}", _profile(f"s{i}"))
    clock["t"] += 11

    assert store.cleanup_expired() == 5
    assert len(store) == 0


@needs_redis
def test_redis_sets_a_ttl_so_sessions_do_not_leak():
    import redis

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    store = RedisSessionStore(REDIS_URL, ttl_seconds=120, client=client)
    store.put("s1", _profile("s1"))

    ttl = client.ttl("detoura:session:s1")
    assert 0 < ttl <= 120, f"expected a live TTL, got {ttl}"
    client.flushdb()


# ---------------------------------------------------------------------------
# Concurrency: the lost update
# ---------------------------------------------------------------------------
def test_concurrent_feedback_never_loses_a_signal(wired):
    """N concurrent signals must produce a count of N.

    Unlocked read-compute-write loses updates: every thread reads the same
    snapshot and the last writer wins. This is the one race in the service
    that corrupts a result rather than a counter.
    """
    signals = 48
    barrier = threading.Barrier(signals)

    def submit() -> None:
        barrier.wait()
        record_feedback("shared", FeedbackAction.SAVED, EVEN)

    threads = [threading.Thread(target=submit) for _ in range(signals)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert get_session("shared").signal_count == signals


def test_concurrency_does_not_bleed_between_sessions(wired):
    barrier = threading.Barrier(24)

    def submit(n: int) -> None:
        barrier.wait()
        record_feedback(f"u{n}", FeedbackAction.SAVED, EVEN)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(24):
        assert get_session(f"u{i}").signal_count == 1


# ---------------------------------------------------------------------------
# Across worker processes - the reason this module exists
# ---------------------------------------------------------------------------
def _signal_in_child(url: str, session_id: str, count: int) -> None:
    """Run in a fresh process, exactly as a uvicorn worker would start."""
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
    from detoura.profiles import COMPONENTS as _C
    from detoura.services.feedback import (
        FeedbackAction as _A,
        configure_sessions as _cfg,
        record_feedback as _rec,
    )
    from detoura.services.session_store import RedisSessionStore as _R

    _cfg(_R(url, ttl_seconds=60))
    even = {name: 1.0 / len(_C) for name in _C}
    for _ in range(count):
        _rec(session_id, _A.SAVED, even)


@needs_redis
def test_one_session_is_coherent_across_worker_processes():
    """The exact failure that blocked multi-worker deployment.

    Four processes, six signals each. With per-process state the counts
    interleave and collide ([1,1,1,2,3,...]); with a shared store the session
    must end at exactly 24.
    """
    import redis

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()

    ctx = mp.get_context("spawn")  # a fresh interpreter, like a real worker
    procs = [
        ctx.Process(target=_signal_in_child, args=(REDIS_URL, "cross", 6))
        for _ in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)

    assert all(p.exitcode == 0 for p in procs), "a worker process failed"

    store = RedisSessionStore(REDIS_URL, client=client)
    session = store.get("cross")
    assert session is not None
    assert session.signal_count == 24, (
        f"expected 24 signals across 4 processes, got {session.signal_count}"
    )
    client.flushdb()


@needs_redis
def test_the_in_memory_store_is_honestly_process_local():
    """Documents *why* Redis is required for multi-worker, rather than
    leaving the reader to trust the claim."""
    import redis

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    store = InMemorySessionStore()
    previous = configure_sessions(store)
    try:
        record_feedback("local", FeedbackAction.SAVED, EVEN)
        assert store.get("local").signal_count == 1
        # Nothing reached the shared store, which is exactly the defect.
        assert RedisSessionStore(REDIS_URL, client=client).get("local") is None
    finally:
        configure_sessions(previous)
        client.flushdb()


def _profile(session_id: str):
    from detoura.profiles import get_profile
    from detoura.services.feedback import SessionProfile

    weights = get_profile(ProfileName.BEST_VALUE).weights
    return SessionProfile(
        session_id=session_id,
        declared_profile=ProfileName.BEST_VALUE,
        declared=weights,
        observed=weights,
        baseline=weights,
    )


# ---------------------------------------------------------------------------
# Outage, corruption and configuration safety (V6.5 release review)
# ---------------------------------------------------------------------------
# Found by independent review, not by the implementation: a store configured
# for Redis started happily with Redis down, leaked raw redis exceptions from
# get() while wrapping them in update(), and two workers on a process-local
# store served [1,2,1,2,3,3,4,5,6,7] without a word of complaint.

from detoura.services.session_store import (  # noqa: E402
    SessionStoreError,
    declared_workers,
    store_from_env,
)

UNREACHABLE = "redis://127.0.0.1:6399/0"  # nothing listens here


def test_a_store_configured_for_redis_refuses_to_start_without_it():
    """Fail fast, not per-request.

    redis-py connects lazily, so without an explicit probe a deployment that
    asked for coherence and cannot have it starts looking perfectly healthy.
    """
    with pytest.raises(SessionStoreError, match="not reachable"):
        RedisSessionStore(UNREACHABLE)


@needs_redis
def test_a_read_failure_surfaces_as_a_store_error_not_a_driver_error():
    """One error contract across the interface.

    `update()` already wrapped its faults; `get()` raised redis-py's own
    exception, so a caller handling outages had to know two error types and
    would silently miss one.
    """
    import redis

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    store = RedisSessionStore(REDIS_URL, client=client)

    class Broken:
        def get(self, *_a, **_k):
            raise redis.ConnectionError("connection reset")

    store._redis = Broken()
    with pytest.raises(SessionStoreError, match="session read failed"):
        store.get("anything")


@needs_redis
@pytest.mark.parametrize(
    "payload", ["{not json", '{"session_id": "p"}', "", "null"]
)
def test_a_corrupt_entry_is_treated_as_absent_and_removed(payload):
    """Fail safely and self-heal.

    Raising would leave the traveller unable to use the endpoint at all, for
    state that is already unrecoverable. Treating it as absent resets
    personalization - small, explainable, self-correcting - and deleting the
    key stops the bad value poisoning every later read.
    """
    import redis

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    store = RedisSessionStore(REDIS_URL, client=client)
    client.set("detoura:session:rotten", payload)

    assert store.get("rotten") is None
    assert client.get("detoura:session:rotten") is None, "the bad key must go"
    client.flushdb()


@needs_redis
def test_a_corrupt_entry_does_not_break_an_update_either():
    import redis

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    store = RedisSessionStore(REDIS_URL, client=client)
    previous = configure_sessions(store)
    try:
        client.set("detoura:session:rotten", "{not json")
        session = record_feedback("rotten", FeedbackAction.SAVED, EVEN)
        assert session.signal_count == 1, "a fresh session should start cleanly"
    finally:
        configure_sessions(previous)
        client.flushdb()


# ---------------------------------------------------------------------------
# Configuration guard
# ---------------------------------------------------------------------------
@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        "DETOURA_SESSION_STORE",
        "DETOURA_REDIS_URL",
        "DETOURA_WORKERS",
        "WEB_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_a_single_worker_needs_no_configuration_at_all(clean_env):
    """The zero-configuration promise, asserted rather than assumed."""
    assert isinstance(store_from_env(), InMemorySessionStore)


def test_more_than_one_worker_without_a_shared_store_refuses_to_start(
    clean_env, monkeypatch
):
    """The blocker this guard exists for.

    Two workers on a process-local store answered twelve signals to one
    session with [1,2,1,2,3,3,4,5,6,7]. Nothing errored. A deployment cannot
    detect that, so the process must not start.
    """
    monkeypatch.setenv("WEB_CONCURRENCY", "4")

    with pytest.raises(SessionStoreError, match="process-local"):
        store_from_env()


def test_the_refusal_says_how_to_fix_it(clean_env, monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(SessionStoreError) as caught:
        store_from_env()

    message = str(caught.value)
    assert "DETOURA_SESSION_STORE=redis" in message
    assert "single worker" in message


@needs_redis
def test_more_than_one_worker_with_a_shared_store_is_allowed(
    clean_env, monkeypatch
):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("DETOURA_SESSION_STORE", "redis")
    monkeypatch.setenv("DETOURA_REDIS_URL", REDIS_URL)

    assert isinstance(store_from_env(), RedisSessionStore)


@pytest.mark.parametrize(
    "env,value,expected",
    [
        ("WEB_CONCURRENCY", "4", 4),
        ("DETOURA_WORKERS", "2", 2),
        ("WEB_CONCURRENCY", "not-a-number", 1),
        ("WEB_CONCURRENCY", "0", 1),
    ],
)
def test_worker_count_is_read_defensively(clean_env, monkeypatch, env, value, expected):
    """A malformed value must not disable the guard by accident."""
    monkeypatch.setenv(env, value)
    assert declared_workers() == expected
