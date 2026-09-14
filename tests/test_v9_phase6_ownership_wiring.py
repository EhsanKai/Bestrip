"""V9 Phase 6 — account -> booking ownership wiring.

**Before this change**: ``persistence.accounts.claim_trip`` - the only
thing that ever populates ``trip_ownership`` - had no caller anywhere in
``src/detoura`` outside its own definition. A signed-in user's booking was
never recorded as theirs; ``GET /api/v1/me/trips`` could never list it, no
matter how many times they checked. Confirmed with an AST-based static scan
of every production source file and an end-to-end test through the real
HTTP booking-creation flow - both are preserved below, now asserting the
fixed behavior instead of documenting the gap.

**After this change**: ``BookingRun`` carries an ``owner_user_id`` (set
once, at booking-intent creation, from the server-resolved session -
``api/v1.py::create_booking_intent`` - never from anything in the request
body). ``services/booking_persistence.persist_run`` - the single place a
``BookingRun`` is mirrored into durable storage, called on every state-
changing booking-intent endpoint starting with creation itself - claims the
trip for that owner every time it runs. ``claim_trip`` is idempotent for
the same user, silently refuses (never raises, never reassigns) a
different user, and (this change) does the existence-check and the insert
inside one transaction so concurrent callers can't race each other into an
unhandled ``IntegrityError`` - see ``persistence/accounts.py``.

Ownership is deliberately independent of booking/payment truth (a
RECOVERY_REQUIRED or FAILED journey is still owned by whoever created it -
My Trips needs to show recovery state, not hide it) and independent of the
traveler's email (a `Traveler` is not a `UserAccount`; the owner is always
the session's account id, never anything from the traveler-details step).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest


def _client(tmp_path, monkeypatch, db_name="ownership.db"):
    from fastapi.testclient import TestClient

    import detoura.persistence.db as _db
    from detoura.api.app import create_app

    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / db_name))
    monkeypatch.setattr(_db, "_DB", None)
    return TestClient(create_app())


def _register_and_login(client, email, password="correct horse battery"):
    client.post("/api/v1/auth/register", json={"email": email, "password": password})
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["user_id"]


def _demo_booking_body(label="T", *, traveler_email="rider@example.com"):
    dep = (datetime.now() + timedelta(days=20)).replace(microsecond=0)
    return {
        "demo_trip_label": label, "demo_currency": "EUR", "demo_travelers": 1,
        "demo_legs": [{
            "origin": "CGN", "destination": "PRG", "departure": dep.isoformat(),
            "arrival": (dep + timedelta(hours=1)).isoformat(), "carrier": "OK",
            "flight_number": "1", "price_per_person": 95.8,
            "cabin": "included", "checked": "unknown",
        }],
        "service_tier": "ALL_IN_ONE",
    }


# ======================================================================
# Authenticated flow: creation attaches ownership, My Trips reflects it
# ======================================================================
def test_a_signed_in_users_booking_intent_is_claimed_as_theirs(tmp_path, monkeypatch):
    from detoura.persistence import accounts as store
    from detoura.persistence import get_db

    client = _client(tmp_path, monkeypatch)
    user_id = _register_and_login(client, "traveler@example.com")

    r = client.post("/api/v1/booking-intents", json=_demo_booking_body())
    assert r.status_code == 201
    booking_id = r.json()["booking_id"]

    db = get_db()
    assert store.get_trip_owner(db, booking_id) == user_id
    assert booking_id in store.list_trip_ids_for_user(db, user_id)


def test_owned_trip_appears_in_my_trips_for_that_user_only(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _register_and_login(client, "owner@example.com")

    r = client.post("/api/v1/booking-intents", json=_demo_booking_body())
    booking_id = r.json()["booking_id"]

    r = client.get("/api/v1/me/trips")
    assert r.status_code == 200
    assert booking_id in {t["booking_id"] for t in r.json()["trips"]}

    r = client.get(f"/api/v1/me/trips/{booking_id}")
    assert r.status_code == 200
    assert r.json()["booking_id"] == booking_id


def test_another_users_my_trips_does_not_show_it_and_owner_only_fetch_is_idor_safe(tmp_path, monkeypatch):
    """IDOR: a random/guessed booking_id and someone else's real booking_id
    must be indistinguishable (both 404) - the existing anti-enumeration
    contract in api/me_trips.py, exercised here against a *real* owned
    booking rather than a stub."""
    client = _client(tmp_path, monkeypatch)
    _register_and_login(client, "alice@example.com")
    r = client.post("/api/v1/booking-intents", json=_demo_booking_body("Alice's trip"))
    booking_id = r.json()["booking_id"]
    client.post("/api/v1/auth/logout")

    _register_and_login(client, "bob@example.com")
    r = client.get("/api/v1/me/trips")
    assert booking_id not in {t["booking_id"] for t in r.json()["trips"]}

    r_other = client.get(f"/api/v1/me/trips/{booking_id}")
    r_missing = client.get("/api/v1/me/trips/bk_does_not_exist_at_all")
    assert r_other.status_code == r_missing.status_code == 404
    assert r_other.json() == r_missing.json()


def test_ownership_does_not_come_from_traveler_email(tmp_path, monkeypatch):
    """A Traveler is not a UserAccount (invariant D). The signed-in owner is
    bob@example.com; the trip's *traveler* details name someone else
    entirely - ownership must track the session, never the traveler email
    submitted in a later step."""
    from detoura.persistence import accounts as store
    from detoura.persistence import get_db

    client = _client(tmp_path, monkeypatch)
    user_id = _register_and_login(client, "bob@example.com")
    r = client.post("/api/v1/booking-intents", json=_demo_booking_body())
    booking_id = r.json()["booking_id"]

    client.post(f"/api/v1/booking-intents/{booking_id}/travelers", json={"travelers": [{
        "given_name": "Someone", "family_name": "Else", "born_on": "1990-01-01",
        "email": "totally-different-person@example.com", "phone": "+1 555 0100",
    }]})

    db = get_db()
    assert store.get_trip_owner(db, booking_id) == user_id
    # And no account was ever created for the traveler's email.
    from detoura.services.email_normalization import normalize_email
    assert store.get_user_by_email(db, normalize_email("totally-different-person@example.com")) is None


# ======================================================================
# Anonymous flow: unaffected, no owner, no account auto-created
# ======================================================================
def test_anonymous_booking_creation_still_works_and_stays_unowned(tmp_path, monkeypatch):
    from detoura.persistence import accounts as store
    from detoura.persistence import get_db

    client = _client(tmp_path, monkeypatch)  # no register/login at all
    r = client.post("/api/v1/booking-intents", json=_demo_booking_body())
    assert r.status_code == 201
    booking_id = r.json()["booking_id"]

    db = get_db()
    assert store.get_trip_owner(db, booking_id) is None


def test_anonymous_trip_does_not_appear_in_an_unrelated_accounts_my_trips(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/v1/booking-intents", json=_demo_booking_body())
    anon_booking_id = r.json()["booking_id"]

    _register_and_login(client, "someone@example.com")
    r = client.get("/api/v1/me/trips")
    assert anon_booking_id not in {t["booking_id"] for t in r.json()["trips"]}


def test_no_account_is_auto_created_for_an_anonymous_booking(tmp_path, monkeypatch):
    from detoura.persistence import accounts as store
    from detoura.persistence import get_db

    client = _client(tmp_path, monkeypatch)
    client.post("/api/v1/booking-intents", json=_demo_booking_body(traveler_email="nobody@example.com"))
    db = get_db()
    assert db.query("SELECT user_id FROM user_accounts") == []
    assert store.list_trip_ids_for_user(db, "anonymous") == []


# ======================================================================
# Idempotency / retries at the persistence layer directly
# ======================================================================
def test_same_owner_retry_is_idempotent(tmp_path, monkeypatch):
    from detoura.persistence import accounts as store
    from detoura.persistence.db import Database

    db = Database(str(tmp_path / "idem.db"))
    assert store.claim_trip(db, user_id="u1", booking_id="bk1") is True
    assert store.claim_trip(db, user_id="u1", booking_id="bk1") is True  # same user, again
    assert store.get_trip_owner(db, "bk1") == "u1"


def test_persist_run_called_repeatedly_does_not_duplicate_or_error(tmp_path, monkeypatch):
    """Mirrors the real lifecycle: persist_run runs on every state-changing
    endpoint, not just creation. Calling it many times for the same owned
    run must stay idempotent - no duplicate rows, no exception."""
    from detoura.persistence import accounts as store
    from detoura.persistence.db import Database
    from detoura.services.booking_flow import create_run_demo
    from detoura.services.booking_persistence import persist_run

    db = Database(str(tmp_path / "retry.db"))
    run = create_run_demo(
        trip_label="T", currency="EUR",
        legs=[{"origin": "CGN", "destination": "PRG", "departure": "2026-11-01T08:00:00",
               "arrival": "2026-11-01T09:00:00", "price_per_person": 90.0}],
        owner_user_id="u1",
    )
    for _ in range(5):
        persist_run(run, db)
    assert store.get_trip_owner(db, run.booking_id) == "u1"
    assert store.list_trip_ids_for_user(db, "u1") == [run.booking_id]


# ======================================================================
# Cross-user claim denial / first-valid-owner-wins
# ======================================================================
def test_cross_user_claim_is_denied_not_reassigned(tmp_path, monkeypatch):
    from detoura.persistence import accounts as store
    from detoura.persistence.db import Database

    db = Database(str(tmp_path / "cross.db"))
    assert store.claim_trip(db, user_id="alice", booking_id="bk1") is True
    assert store.claim_trip(db, user_id="bob", booking_id="bk1") is False
    assert store.get_trip_owner(db, "bk1") == "alice"  # unchanged


def test_owner_cannot_be_spoofed_from_the_booking_creation_request(tmp_path, monkeypatch):
    """Ownership is server-derived (invariant A): nothing in the request
    body can name an owner, authenticated or not."""
    from fastapi.testclient import TestClient

    from detoura.api.app import create_app
    from detoura.persistence import accounts as store
    from detoura.persistence import get_db

    signed_in_client = _client(tmp_path, monkeypatch, db_name="spoof.db")
    victim_id = _register_and_login(signed_in_client, "victim@example.com")

    # A genuinely separate, unauthenticated client (its own cookie jar) -
    # against the same database - rather than trying to log the first one
    # out (which itself requires a CSRF round-trip unrelated to this test).
    anon_client = TestClient(create_app())
    body = _demo_booking_body()
    body["owner_user_id"] = victim_id  # not a real field - must be ignored/rejected, not honored
    body["user_id"] = victim_id
    r = anon_client.post("/api/v1/booking-intents", json=body)
    assert r.status_code == 201
    booking_id = r.json()["booking_id"]
    assert "detoura_session" not in anon_client.cookies  # confirms this really was an anonymous call

    db = get_db()
    assert store.get_trip_owner(db, booking_id) is None  # anonymous caller, no session -> no owner


# ======================================================================
# Concurrency
# ======================================================================
def test_concurrent_same_user_claims_produce_one_logical_ownership(tmp_path, monkeypatch):
    from detoura.persistence import accounts as store
    from detoura.persistence.db import Database

    db = Database(str(tmp_path / "conc_same.db"))
    results: list[bool] = []
    lock = threading.Lock()

    def _claim():
        ok = store.claim_trip(db, user_id="alice", booking_id="bk_shared")
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=_claim) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(results)  # every call from the true owner reports success
    assert store.get_trip_owner(db, "bk_shared") == "alice"


def test_concurrent_different_user_claims_have_one_deterministic_winner(tmp_path, monkeypatch):
    """The old check-then-insert race (fixed this slice - see
    persistence/accounts.claim_trip) could raise a raw IntegrityError from
    the losing thread instead of a clean False. This drives real
    contention: many threads for TWO different candidate owners racing the
    same booking_id."""
    from detoura.persistence import accounts as store
    from detoura.persistence.db import Database

    db = Database(str(tmp_path / "conc_diff.db"))
    outcomes: list[tuple[str, bool]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _claim(user_id: str):
        try:
            ok = store.claim_trip(db, user_id=user_id, booking_id="bk_contested")
        except BaseException as exc:  # noqa: BLE001 - the exact thing being tested for
            with lock:
                errors.append(exc)
            return
        with lock:
            outcomes.append((user_id, ok))

    threads = [
        threading.Thread(target=_claim, args=(f"user-{i % 2}",))
        for i in range(40)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"claim_trip must never raise under contention, got: {errors}"
    winner = store.get_trip_owner(db, "bk_contested")
    assert winner in ("user-0", "user-1")
    # Every call attributed to the winner reports True; every call for the
    # other candidate reports False. No corruption, no double-claim.
    for user_id, ok in outcomes:
        assert ok == (user_id == winner)


def test_booking_retry_after_ownership_exists_does_not_duplicate(tmp_path, monkeypatch):
    """A booking-intent poll/retry (same booking_id, same owner) after
    ownership is already recorded must stay a no-op, not an error and not
    a second row."""
    from detoura.persistence import accounts as store
    from detoura.persistence import get_db

    client = _client(tmp_path, monkeypatch)
    user_id = _register_and_login(client, "retry@example.com")
    r = client.post("/api/v1/booking-intents", json=_demo_booking_body())
    booking_id = r.json()["booking_id"]

    # Poll the intent a few more times - each GET/POST on it re-persists.
    for _ in range(3):
        client.get(f"/api/v1/booking-intents/{booking_id}")

    db = get_db()
    assert store.list_trip_ids_for_user(db, user_id) == [booking_id]  # exactly one row


# ======================================================================
# Recovery / partial-failure state: ownership is independent of booking truth
# ======================================================================
def test_recovery_required_journey_is_still_owned_and_visible(tmp_path, monkeypatch):
    """Ownership must not be coupled to CONFIRMED (invariant F). A journey
    whose phase reflects a problem must still surface for its owner -
    My Trips needs to show recovery state, not hide it."""
    from detoura.persistence import accounts as store
    from detoura.persistence import get_db
    from detoura.services.booking_orchestrator import BookingPhase

    client = _client(tmp_path, monkeypatch)
    user_id = _register_and_login(client, "recovery@example.com")
    r = client.post("/api/v1/booking-intents", json=_demo_booking_body())
    booking_id = r.json()["booking_id"]

    # Force the in-memory run into a non-terminal-success phase and
    # re-persist, the same way the real orchestrator would on a problem -
    # without touching payment/provider code, which is out of scope here.
    from detoura.services.booking_flow import booking_store
    from detoura.services.booking_persistence import persist_run

    run = booking_store().get(booking_id)
    run.phase = BookingPhase.PARTIAL_FAILURE
    persist_run(run, get_db())

    db = get_db()
    # Ownership survives the phase change untouched.
    assert store.get_trip_owner(db, booking_id) == user_id
    assert booking_id in store.list_trip_ids_for_user(db, user_id)
    r = client.get(f"/api/v1/me/trips/{booking_id}")
    assert r.status_code == 200


# ======================================================================
# Static confirmation the wiring gap is closed (was: proof it existed)
# ======================================================================
def test_claim_trip_now_has_a_production_caller():
    import ast
    import pathlib

    src_root = pathlib.Path(__file__).resolve().parents[1] / "src" / "detoura"
    callers = []
    for path in src_root.rglob("*.py"):
        if path.name == "accounts.py" and path.parent.name == "persistence":
            continue  # the definition site itself
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "claim_trip":
                callers.append(str(path))
    assert callers, (
        "claim_trip has no production caller again - the ownership-wiring "
        "regression this test guards against has come back."
    )
