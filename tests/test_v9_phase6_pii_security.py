"""V9 Phase 6 PII / Privacy / Logging Security adversarial slice.

Fresh attack pass over Detoura's handling of personal/sensitive data: the
`Traveler` redaction interface (defined but, before this slice, never
exercised by any test or consumer), fresh cross-user IDOR attacks on
financial documents / confirmations / communications through the REAL
`/me/trips/*` HTTP endpoints (not the stub-based tests in
test_v9_phase5_me_trips_api.py, which predate the real modules being
merged and never actually attack the live code path), the small, already-
identified set of real logger call sites in the codebase, and the account
audit trail's own deliberate PII minimization.

Does not re-litigate what earlier Phase 6 slices already covered
end-to-end (payment/booking ownership IDOR) - see
test_v9_phase6_ownership_wiring.py, test_v9_phase6_payment_security.py.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


# ======================================================================
# Shared HTTP fixtures (same pattern as test_v9_phase6_ownership_wiring.py)
# ======================================================================
def _client(tmp_path, monkeypatch, db_name="pii.db"):
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


def _demo_booking_body(label="T"):
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


def _csrf_headers(client) -> dict:
    csrf = client.cookies.get("detoura_csrf")
    return {"X-CSRF-Token": csrf} if csrf else {}


def _new_booking(client, label="T") -> str:
    r = client.post(
        "/api/v1/booking-intents", json=_demo_booking_body(label), headers=_csrf_headers(client),
    )
    assert r.status_code == 201, r.text
    return r.json()["booking_id"]


# ======================================================================
# Group A - Traveler.safe_summary()/public_summary(), the redaction
# interface (V9 Phase 6 finding: defined, documented as "what the API
# layer uses to keep PERSONAL/SENSITIVE values out of logs, analytics,
# URLs and provider metrics", but never actually called anywhere in
# src/detoura outside its own definition - confirmed via grep. No current
# call site was found to leak SENSITIVE fields regardless (each manually
# avoids them), but the interface itself was entirely untested. Locking in
# its correctness here so it is ready and proven if/when it is wired up,
# and so a future edit to it cannot silently start leaking DOB/passport.)
# ======================================================================
def _traveler(**overrides):
    from detoura.models.traveler import Traveler

    base = dict(
        given_name="Ada", family_name="Lovelace", born_on=date(1990, 1, 1),
        email="ada@example.com", phone="+491511234567",
        nationality="GB", passport_number="P1234567", passport_expiry=date(2030, 1, 1),
        document_type="passport",
    )
    base.update(overrides)
    return Traveler(**base)


def test_safe_summary_excludes_dob_passport_nationality():
    t = _traveler()
    summary = t.safe_summary()
    assert summary == {"name": "Ada Lovelace", "email": "ada@example.com", "has_travel_document": True}
    blob = str(summary)
    for forbidden in ("1990", "P1234567", "GB", "2030"):
        assert forbidden not in blob


def test_public_summary_excludes_even_email():
    t = _traveler()
    summary = t.public_summary()
    assert summary == {"name": "Ada Lovelace"}
    assert "email" not in summary
    assert "ada@example.com" not in str(summary)


def test_safe_summary_has_travel_document_reflects_real_state_without_the_document_itself():
    with_doc = _traveler()
    assert with_doc.safe_summary()["has_travel_document"] is True
    without_doc = _traveler(passport_number=None, passport_expiry=None, nationality=None)
    summary = without_doc.safe_summary()
    assert summary["has_travel_document"] is False
    assert "passport_number" not in summary and "passport_expiry" not in summary


def test_sensitivity_classification_matches_the_fields_the_summaries_actually_exclude():
    """Independent cross-check: every field SENSITIVITY marks SENSITIVE
    must be absent from safe_summary()'s keys, and PERSONAL fields other
    than name/email must be absent from public_summary()'s keys - proving
    the classification dict and the actual redaction methods agree, not
    just that each looks reasonable in isolation."""
    from detoura.models.traveler import DataSensitivity, SENSITIVITY

    t = _traveler()
    safe_keys = set(t.safe_summary().keys())
    public_keys = set(t.public_summary().keys())
    for field, sensitivity in SENSITIVITY.items():
        if sensitivity is DataSensitivity.SENSITIVE:
            assert field not in safe_keys, f"{field} is SENSITIVE but safe_summary() exposes it"
        assert field not in public_keys or field in ("given_name", "family_name")


def test_sensitivity_dict_covers_every_traveler_field():
    """Independent-review finding: `SENSITIVITY` omitted
    `passport_issuing_country`/`document_type` entirely - contradicting the
    module's own "Every field is classified" claim. The previous test
    above only ever iterates `SENSITIVITY.items()`, so it cannot catch a
    field simply MISSING from the dict; this test checks the dict's
    key set against the model's own field set directly, so a future field
    added to `Traveler` without a matching classification fails loudly."""
    from detoura.models.traveler import SENSITIVITY, Traveler

    assert set(SENSITIVITY.keys()) == set(Traveler.model_fields.keys())


# ======================================================================
# Group B - fresh cross-user IDOR through the REAL /me/trips/* HTTP
# endpoints. test_v9_phase5_me_trips_api.py predates the real
# confirmation/document/communication modules being merged and tests
# stub objects directly, never the live API - "do not assume previous
# ownership/IDOR work is sufficient" applies here specifically.
# ======================================================================
def _issue_document(db, *, booking_id, journey_reference, user_id):
    from detoura.models.financial_document import FinancialDocument, FinancialDocumentType
    from detoura.persistence import financial_documents as doc_store

    doc = FinancialDocument(
        document_id=doc_store.new_id("findoc") if hasattr(doc_store, "new_id") else f"findoc_{booking_id}",
        document_type=FinancialDocumentType.RECEIPT, document_number=f"R-{booking_id}",
        booking_id=booking_id, journey_reference=journey_reference, user_id=user_id,
        currency="EUR", customer_total=150.0, captured_amount=150.0, issued_at=NOW,
        supplier_transport=120.0, supplier_baggage=0.0, supplier_fees=0.0,
        detoura_markup=20.0, detoura_service_fee=10.0, discount=0.0, tax=0.0,
    )
    stored, _ = doc_store.issue_document(db, document=doc, idempotency_key=f"findoc:{booking_id}:receipt")
    return stored


def _create_confirmation(db, *, booking_id, journey_reference, user_id):
    from detoura.models.confirmation import ConfirmationStatus, JourneyConfirmation
    from detoura.persistence import confirmations as conf_store

    conf = JourneyConfirmation(
        confirmation_id=f"conf_{booking_id}", booking_id=booking_id,
        journey_reference=journey_reference, user_id=user_id,
        status=ConfirmationStatus.CONFIRMED, service_tier="ALL_IN_ONE",
        booking_phase="COMPLETE", party_size=1, lead_name="Ada Lovelace",
        created_at=NOW,
    )
    stored, _ = conf_store.create_confirmation(db, confirmation=conf)
    return stored


def _create_communication(db, *, booking_id, journey_reference, user_id):
    from detoura.models.communication import (
        CommunicationChannel, CommunicationStatus, CommunicationType, CustomerCommunication,
    )
    from detoura.persistence import communications as comm_store

    comm = CustomerCommunication(
        communication_id=f"comm_{booking_id}", booking_id=booking_id,
        journey_reference=journey_reference, user_id=user_id,
        channel=CommunicationChannel.EMAIL, communication_type=CommunicationType.BOOKING_CONFIRMATION,
        status=CommunicationStatus.SENT, recipient_address="ada@example.com",
        idempotency_key=f"comm:{booking_id}:confirmation",
    )
    stored, _ = comm_store.create_communication(db, communication=comm)
    return stored


def test_document_cross_user_idor_blocked_at_trip_ownership(tmp_path, monkeypatch):
    from detoura.persistence import get_db

    client = _client(tmp_path, monkeypatch)
    user_a = _register_and_login(client, "docowner@example.com")
    booking_a = _new_booking(client, "A's trip")

    client_b = type(client)(client.app)  # a genuinely separate cookie jar
    user_b = _register_and_login(client_b, "docattacker@example.com")

    db = get_db()
    doc = _issue_document(db, booking_id=booking_a, journey_reference=f"jr_{booking_a}", user_id=user_a)

    # Owner: succeeds.
    r = client.get(f"/api/v1/me/trips/{booking_a}/documents/{doc.document_id}")
    assert r.status_code == 200
    assert r.json()["document_id"] == doc.document_id

    # A different authenticated user, same document_id + owner's booking_id: blocked.
    r = client_b.get(f"/api/v1/me/trips/{booking_a}/documents/{doc.document_id}")
    assert r.status_code == 404

    # Booking-id substitution: user B owns their OWN booking, but tries the
    # SAME document_id under it - must still fail (the document's own
    # user_id, not just the booking's, is checked).
    booking_b = _new_booking(client_b, "B's trip")
    r = client_b.get(f"/api/v1/me/trips/{booking_b}/documents/{doc.document_id}")
    assert r.status_code == 404

    # Listing must not leak the document into user B's own trip's list either.
    r = client_b.get(f"/api/v1/me/trips/{booking_b}/documents")
    assert r.status_code == 200
    assert doc.document_id not in {d["document_id"] for d in r.json()["documents"]}

    # Anonymous: rejected before any ownership logic runs.
    anon = type(client)(client.app)
    r = anon.get(f"/api/v1/me/trips/{booking_a}/documents/{doc.document_id}")
    assert r.status_code in (401, 403)


def test_confirmation_cross_user_idor_blocked(tmp_path, monkeypatch):
    from detoura.persistence import get_db

    client = _client(tmp_path, monkeypatch)
    user_a = _register_and_login(client, "confowner@example.com")
    booking_a = _new_booking(client, "A's trip")

    client_b = type(client)(client.app)
    _register_and_login(client_b, "confattacker@example.com")

    db = get_db()
    _create_confirmation(db, booking_id=booking_a, journey_reference=f"jr_{booking_a}", user_id=user_a)

    r = client.get(f"/api/v1/me/trips/{booking_a}/confirmation")
    assert r.status_code == 200
    assert r.json()["status"] == "CONFIRMED"

    r = client_b.get(f"/api/v1/me/trips/{booking_a}/confirmation")
    assert r.status_code == 404

    anon = type(client)(client.app)
    r = anon.get(f"/api/v1/me/trips/{booking_a}/confirmation")
    assert r.status_code in (401, 403)


def test_communication_resend_cross_user_idor_blocked(tmp_path, monkeypatch):
    """resend_confirmation is a MUTATING action (triggers a real send) -
    verify a non-owner cannot trigger it against someone else's booking,
    and that it is CSRF-protected for the legitimate owner too."""
    from detoura.persistence import get_db

    client = _client(tmp_path, monkeypatch)
    user_a = _register_and_login(client, "commowner@example.com")
    booking_a = _new_booking(client, "A's trip")

    client_b = type(client)(client.app)
    _register_and_login(client_b, "commattacker@example.com")

    db = get_db()
    _create_communication(db, booking_id=booking_a, journey_reference=f"jr_{booking_a}", user_id=user_a)

    # Non-owner: blocked before CSRF is even checked (ownership first).
    r = client_b.post(f"/api/v1/me/trips/{booking_a}/confirmation/resend")
    assert r.status_code == 404

    # Owner without a CSRF header: rejected too (mutating action).
    r = client.post(f"/api/v1/me/trips/{booking_a}/confirmation/resend")
    assert r.status_code == 403

    anon = type(client)(client.app)
    r = anon.post(f"/api/v1/me/trips/{booking_a}/confirmation/resend")
    assert r.status_code in (401, 403)


def test_my_trips_list_never_includes_another_users_trip(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _register_and_login(client, "lister_a@example.com")
    booking_a = _new_booking(client, "A's trip")

    client_b = type(client)(client.app)
    _register_and_login(client_b, "lister_b@example.com")
    booking_b = _new_booking(client_b, "B's trip")

    ids_a = {t["booking_id"] for t in client.get("/api/v1/me/trips").json()["trips"]}
    ids_b = {t["booking_id"] for t in client_b.get("/api/v1/me/trips").json()["trips"]}
    assert booking_a in ids_a and booking_a not in ids_b
    assert booking_b in ids_b and booking_b not in ids_a


# ======================================================================
# Group C - the account audit trail's own deliberate PII minimization
# (verified by reading the code; locked in here with a real end-to-end
# assertion against the actual persisted audit rows).
# ======================================================================
def test_failed_login_audit_trail_never_records_the_attempted_email(tmp_path, monkeypatch):
    from detoura.persistence import audit as audit_store
    from detoura.persistence import get_db

    client = _client(tmp_path, monkeypatch)
    victim_email = "victim-does-not-exist@example.com"
    r = client.post("/api/v1/auth/login", json={"email": victim_email, "password": "whatever123"})
    assert r.status_code == 401

    db = get_db()
    events = audit_store.recent(db, limit=50)
    login_failures = [e for e in events if e.action == "login_failure"]
    assert login_failures, "expected a login_failure audit event"
    blob = str([e.model_dump() if hasattr(e, "model_dump") else e.__dict__ for e in login_failures])
    assert victim_email not in blob
    assert "no such account" in blob  # the real reason, but never the attempted address


def test_account_created_audit_trail_records_only_the_email_domain(tmp_path, monkeypatch):
    from detoura.persistence import audit as audit_store
    from detoura.persistence import get_db

    client = _client(tmp_path, monkeypatch)
    email = "quite-specific-person@corp-example.com"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "correct horse battery"})
    assert r.status_code == 200

    db = get_db()
    events = [e for e in audit_store.recent(db, limit=50) if e.action == "account_created"]
    assert events
    blob = str([e.model_dump() if hasattr(e, "model_dump") else e.__dict__ for e in events])
    assert email not in blob  # the full address never appears
    assert "corp-example.com" in blob  # only the domain does


# ======================================================================
# Group D - password/session leakage through consumer API responses.
# ======================================================================
def test_login_response_body_never_contains_the_raw_session_token_or_password_hash(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    email = "sessioncheck@example.com"
    password = "correct horse battery"
    client.post("/api/v1/auth/register", json={"email": email, "password": password})
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"user_id"}
    raw_session_cookie = client.cookies.get("detoura_session")
    assert raw_session_cookie  # the cookie IS how the token travels
    assert raw_session_cookie not in r.text  # never duplicated into the JSON body
    assert password not in r.text
    for forbidden in ("password_hash", "argon2", "$argon2"):
        assert forbidden not in r.text.lower()


def test_register_response_never_echoes_the_password(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    password = "correct horse battery unique 42"
    r = client.post("/api/v1/auth/register", json={"email": "noecho@example.com", "password": password})
    assert r.status_code == 200
    assert password not in r.text


# ======================================================================
# Group E - the small, real logging surface (verified by inspection:
# exactly 3 files in src/detoura use the stdlib logging module with an
# actual log/logger call - providers/duffel.py, services/
# booking_persistence.py, services/post_booking_finalizer.py). Attack each
# with a caplog assertion proving no PII beyond internal identifiers
# reaches the log record.
# ======================================================================
def test_claim_trip_failure_log_contains_only_internal_identifiers(caplog):
    from detoura.persistence.db import Database
    from detoura.services import booking_persistence
    from detoura.services.booking_orchestrator import BookingPhase, BookingRun
    from detoura.models.booking import PriceTolerance
    from detoura.models.travel_pass import PassMode
    from detoura.models.traveler import Traveler, TravelerParty

    db = Database(":memory:")
    db.close()  # a closed db forces claim_trip to raise, exercising the log line

    party = TravelerParty(travelers=(Traveler(
        given_name="Grace", family_name="Hopper", born_on=date(1985, 1, 1),
        email="grace@example.com", phone="+491511234568",
    ),))
    run = BookingRun(
        booking_id="bk_logtest", journey_reference="jr_logtest", mode=PassMode.DEMO_ONLY,
        trip_label="t", route_cities=("A", "B"), currency="EUR", discovered_total=10.0,
        tolerance=PriceTolerance(), items=[], party=party, phase=BookingPhase.COMPLETE,
        owner_user_id="usr_logtest_owner",
    )
    with caplog.at_level(logging.WARNING, logger="detoura.services.booking_persistence"):
        booking_persistence.persist_run(run, db)  # both writes fail on the closed db; must not raise

    log_text = caplog.text
    assert "bk_logtest" in log_text or "claim_trip failed" in log_text
    for forbidden in ("Grace", "Hopper", "grace@example.com", "1985"):
        assert forbidden not in log_text


def test_document_and_communication_failure_logs_never_contain_recipient_or_traveler_name(caplog):
    from detoura.persistence.db import Database
    from detoura.services import post_booking_finalizer as finalizer

    db = Database(":memory:")

    class _FakeBooking:
        booking_id = "bk_findoclog"
        journey_reference = "jr_findoclog"
        lead_email = "should-not-appear@example.com"
        lead_name = "Should Not Appear"
        items = []
        party_size = 1
        currency = "EUR"
        customer_total = 100.0
        service_tier = "ALL_IN_ONE"

    class _FakeConfirmation:
        user_id = None
        status = type("S", (), {"value": "CONFIRMED"})()

    with caplog.at_level(logging.WARNING, logger="detoura.services.post_booking_finalizer"):
        finalizer._issue_document_safely(db, booking_id="bk_findoclog", now=NOW)
        finalizer._send_communication_safely(
            db, booking=_FakeBooking(), confirmation=_FakeConfirmation(), now=NOW,
        )

    log_text = caplog.text
    assert "bk_findoclog" in log_text
    for forbidden in ("should-not-appear@example.com", "Should Not Appear"):
        assert forbidden not in log_text


def test_duffel_offer_parsing_log_never_contains_traveler_or_passenger_data(caplog):
    """The offer-mapping debug/info log lines only ever run over search-
    result (pre-booking) data, which carries no traveler identity - this
    proves it directly against a payload shaped to carry a passenger-like
    field, in case a future offer shape adds one."""
    from detoura.providers.duffel import DuffelTransportProvider

    provider = DuffelTransportProvider(access_token="duffel_test_logcheck")
    body = {
        "data": {
            "offers": [
                {"id": "off_bad", "slices": [], "passengers": [{"given_name": "Should Not Log"}]},
            ]
        }
    }
    with caplog.at_level(logging.DEBUG, logger="detoura.providers.duffel"):
        provider.parse_offers(body, "BER", "LHR", travelers=1)

    assert "Should Not Log" not in caplog.text
