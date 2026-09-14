"""V9 Phase 6 slice 2 — account→booking ownership wiring (release-debt item).

Phase 2.6 built the "My Trips" ownership foundation
(``persistence.accounts.claim_trip`` / ``get_trip_owner`` /
``list_trip_ids_for_user``) as *additive* metadata a caller attaches to a
booking after the fact (see ``persistence/accounts.py``'s module docstring:
"nothing here is reachable from the search or booking path except through
the explicit ``claim_trip`` call a caller makes"). That is the right design
- booking must keep working anonymously - but it also means ownership is
only ever populated if *something* actually calls ``claim_trip``.

This test proves, against the real booking-intent HTTP flow (not a mock),
that nothing currently does: ``claim_trip`` has no caller anywhere in
``src/detoura`` outside its own definition and tests. A signed-in user who
creates a booking today gets a booking that is never recorded as theirs -
``GET /api/v1/me/trips`` will never list it, no matter how many times they
check. This is a functional gap (My Trips is unpopulatable), not an
authorization/security hole - nothing is over-exposed.

Marked ``xfail(strict=True)``: this documents the *desired* behavior. When
a future change wires an optional session into booking-intent creation and
calls ``claim_trip`` (see ``docs/V9_PHASE6_SECURITY_REPORT.md`` for the
suggested hook point and why it wasn't done in this same slice - it touches
`BookingRun`, the central orchestrator object shared with Phase 4/7/8), this
test will start passing and pytest will fail the run until the ``xfail``
marker is removed - turning it into a permanent regression test at that
point, rather than silently staying green either way.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import detoura.persistence.db as _db
    from detoura.api.app import create_app

    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "ownership.db"))
    monkeypatch.setattr(_db, "_DB", None)
    return TestClient(create_app())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "claim_trip() is never called from the booking-creation path yet - "
        "see tests/test_v9_phase6_ownership_wiring.py module docstring and "
        "docs/V9_PHASE6_SECURITY_REPORT.md ('Account -> booking ownership')."
    ),
)
def test_a_signed_in_users_booking_intent_is_claimed_as_theirs(tmp_path, monkeypatch):
    from detoura.persistence import accounts as store
    from detoura.persistence import get_db

    client = _client(tmp_path, monkeypatch)

    email, password = "traveler@example.com", "correct horse battery"
    client.post("/api/v1/auth/register", json={"email": email, "password": password})
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200
    user_id = r.json()["user_id"]

    dep = (datetime.now() + timedelta(days=20)).replace(microsecond=0)
    legs = [{
        "origin": "CGN", "destination": "PRG", "departure": dep.isoformat(),
        "arrival": (dep + timedelta(hours=1)).isoformat(), "carrier": "OK",
        "flight_number": "1", "price_per_person": 95.8, "cabin": "included", "checked": "unknown",
    }]
    # The TestClient carries the session cookie set by /login on this next
    # request automatically - this booking is created by an authenticated
    # caller in every sense the HTTP layer can express today.
    r = client.post("/api/v1/booking-intents", json={
        "demo_trip_label": "T", "demo_currency": "EUR", "demo_travelers": 1,
        "demo_legs": legs, "service_tier": "ALL_IN_ONE",
    })
    assert r.status_code == 201
    booking_id = r.json()["booking_id"]

    db = get_db()
    # Desired behavior: the booking this signed-in user just created is
    # recorded as theirs.
    assert store.get_trip_owner(db, booking_id) == user_id
    assert booking_id in store.list_trip_ids_for_user(db, user_id)


def test_claim_trip_has_no_production_caller_yet():
    """A static confirmation of the same gap, independent of the HTTP flow
    above: nothing in the non-test source tree calls ``claim_trip`` at all.
    Not xfail - this one is a plain fact about the current source tree and
    should stay green (and be revisited) whenever it stops being true."""
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
            elif isinstance(node, ast.Name) and node.id == "claim_trip":
                callers.append(str(path))
    assert callers == [], (
        f"claim_trip now has caller(s) {callers} - the wiring gap this test "
        "documents appears to be fixed; delete this test and remove the "
        "xfail marker on test_a_signed_in_users_booking_intent_is_claimed_as_theirs above."
    )
