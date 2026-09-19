"""V9 Financial Document Download API slice.

Closes the gap the V9 Limited Beta Reality Audit and Phase 5 Final Report
both named explicitly: financial documents (receipt/invoice/credit note)
are generated and persisted immutably, but their PDF bytes were never
reachable through a consumer route (`api/me_trips.py`'s own stale comment:
"Agent 6: wire actual PDF serving once module is merged.").

This file tests the completed route:

    GET /api/v1/me/trips/{booking_id}/documents/{document_id}/download

against the two-level ownership check it shares with the pre-existing
metadata route (`get_document` in `api/me_trips.py`), and against the
invariants the financial-document domain itself already enforces
(immutability, no recomputation, no cross-user/cross-booking leakage).

Booking/payment/document state is seeded directly via the persistence and
service layers — the same pattern `tests/test_v9_phase5_integration.py`
uses — rather than replaying the full search->offer->checkout HTTP flow,
which is orthogonal to what this slice changes. The HTTP layer under test
(the download route's auth/ownership/response handling) is always
exercised for real, through a real `TestClient` and a real session cookie.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from detoura.communication_config import reset_communication_config
from detoura.models.commercial import CommercialQuote, PriceBreakdown, PricingPolicyRef, ServiceTier
from detoura.models.financial_document import FinancialDocumentType
from detoura.payment_config import reset_payment_config, resolve_provider
from detoura.persistence import accounts, bookings, economics, get_db
from detoura.services import financial_document_service as fds
from detoura.services import payment_service as ps


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_payment_config()
    reset_communication_config()
    yield
    reset_payment_config()
    reset_communication_config()


def _client() -> TestClient:
    from detoura.api.app import create_app

    return TestClient(create_app())


def _register_and_login(client: TestClient, email: str, password: str = "correct horse battery") -> str:
    client.post("/api/v1/auth/register", json={"email": email, "password": password})
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["user_id"]


def _quote(total: float) -> CommercialQuote:
    return CommercialQuote(
        service_tier=ServiceTier.BASIC,
        breakdown=PriceBreakdown(
            currency="EUR", supplier_transport=total - 20.0,
            detoura_service_fee=10.0, detoura_markup=10.0,
        ),
        markup_policy=PricingPolicyRef(policy_id="default", version=1),
    )


def _seed_paid_booking(db, *, booking_id: str, user_id: str | None, total: float = 150.0) -> None:
    """Seed a COMPLETE booking with a captured payment, mirroring
    ``test_v9_phase5_integration.py::_seed_paid_booking``. When ``user_id``
    is given, the trip is claimed by that account (real ownership wiring,
    V9 Phase 6) - when ``None``, the booking is left anonymous on purpose,
    to exercise the "anonymous booking never becomes globally readable"
    contract.
    """
    bookings.upsert(db, bookings.BookingRecord(
        booking_id=booking_id, journey_reference=f"jr_{booking_id}",
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        mode="DEMO_ONLY", phase="complete", recovery_state="",
        party_size=1, lead_name="Ada Lovelace", lead_email="lead@example.com",
        customer_total=total, currency="EUR", service_tier="BASIC",
    ))
    if user_id:
        accounts.claim_trip(db, user_id=user_id, booking_id=booking_id, journey_reference=f"jr_{booking_id}")
    quote = _quote(total)
    economics.write_snapshot(db, booking_id=booking_id, journey_reference=f"jr_{booking_id}", quote=quote, snapshot={})
    provider = resolve_provider()
    snap = ps.freeze_checkout_snapshot(
        db, booking_id=booking_id, journey_reference=f"jr_{booking_id}",
        user_id=user_id, service_tier="BASIC", quote=quote,
    )
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name="sandbox", idempotency_key=f"idem_{booking_id}")
    payment = ps.authorize_payment(db, payment=payment, provider=provider, snapshot=snap)
    ps.request_capture(db, payment=payment, provider=provider)


def _issue_receipt(db, *, booking_id: str) -> str:
    doc = fds.issue_receipt_or_invoice(
        db, booking_id=booking_id, document_type=FinancialDocumentType.RECEIPT,
        idempotency_key=f"docidem_{booking_id}",
    )
    return doc.document_id


def _download(client: TestClient, booking_id: str, document_id: str):
    return client.get(f"/api/v1/me/trips/{booking_id}/documents/{document_id}/download")


# ======================================================================
# 1-5: happy path - list, download, correct bytes/type/filename
# ======================================================================
def test_owner_can_list_own_booking_documents():
    client = _client()
    user_id = _register_and_login(client, "alice@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_list", user_id=user_id)
    document_id = _issue_receipt(db, booking_id="bk_list")

    r = client.get("/api/v1/me/trips/bk_list/documents")
    assert r.status_code == 200
    docs = r.json()["documents"]
    assert len(docs) == 1
    assert docs[0]["document_id"] == document_id
    assert docs[0]["download_available"] is True
    assert docs[0]["download_url"] == f"/api/v1/me/trips/bk_list/documents/{document_id}/download"


def test_owner_can_download_own_pdf_with_correct_type_and_bytes():
    client = _client()
    user_id = _register_and_login(client, "alice2@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_dl", user_id=user_id)
    document_id = _issue_receipt(db, booking_id="bk_dl")

    r = _download(client, "bk_dl", document_id)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")

    from detoura.persistence import financial_documents as store
    stored_bytes = store.get_document_pdf(db, document_id)
    assert r.content == stored_bytes


def test_content_disposition_is_safe_and_attachment():
    client = _client()
    user_id = _register_and_login(client, "alice3@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_cd", user_id=user_id)
    document_id = _issue_receipt(db, booking_id="bk_cd")

    r = _download(client, "bk_cd", document_id)
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "detoura-receipt-RCPT-" in disposition
    assert "\r" not in disposition and "\n" not in disposition
    # No raw internal document_id (a random opaque token) leaked into the
    # filename when a public document_number already exists for that.
    assert document_id not in disposition


def test_download_response_is_not_cacheable():
    client = _client()
    user_id = _register_and_login(client, "alice4@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_cache", user_id=user_id)
    document_id = _issue_receipt(db, booking_id="bk_cache")

    r = _download(client, "bk_cache", document_id)
    assert "no-store" in r.headers["cache-control"]


# ======================================================================
# 6-9: IDOR - cross-user, cross-booking, and combined substitution
# ======================================================================
def test_booking_a_owner_cannot_download_booking_b_document():
    client = _client()
    alice = _register_and_login(client, "alice5@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_a", user_id=alice)
    doc_a = _issue_receipt(db, booking_id="bk_a")

    client.post("/api/v1/auth/logout")
    _register_and_login(client, "bob@example.com")
    _seed_paid_booking(db, booking_id="bk_b", user_id="someone-else")

    r = _download(client, "bk_a", doc_a)
    assert r.status_code == 404


def test_document_id_substitution_across_own_bookings_fails():
    """Alice owns two bookings; a document from one must not be servable
    under the other booking_id in the URL, even though both are hers."""
    client = _client()
    alice = _register_and_login(client, "alice6@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_x", user_id=alice)
    _seed_paid_booking(db, booking_id="bk_y", user_id=alice, total=200.0)
    doc_y = _issue_receipt(db, booking_id="bk_y")

    r = _download(client, "bk_x", doc_y)
    assert r.status_code == 404


def test_booking_id_substitution_with_a_real_other_users_booking_fails():
    client = _client()
    alice = _register_and_login(client, "alice7@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_mine", user_id=alice)
    doc_mine = _issue_receipt(db, booking_id="bk_mine")

    client.post("/api/v1/auth/logout")
    bob = _register_and_login(client, "bob2@example.com")
    _seed_paid_booking(db, booking_id="bk_bobs", user_id=bob)

    r = _download(client, "bk_bobs", doc_mine)
    assert r.status_code == 404


def test_booking_and_document_substitution_together_fails():
    client = _client()
    alice = _register_and_login(client, "alice8@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_both_a", user_id=alice)
    doc_a = _issue_receipt(db, booking_id="bk_both_a")

    client.post("/api/v1/auth/logout")
    bob = _register_and_login(client, "bob3@example.com")
    _seed_paid_booking(db, booking_id="bk_both_b", user_id=bob)
    doc_b = _issue_receipt(db, booking_id="bk_both_b")

    # Bob tries every combination of the two ids; only his own pairing works.
    assert _download(client, "bk_both_a", doc_a).status_code == 404
    assert _download(client, "bk_both_a", doc_b).status_code == 404
    assert _download(client, "bk_both_b", doc_a).status_code == 404
    assert _download(client, "bk_both_b", doc_b).status_code == 200


# ======================================================================
# 10-13: authentication model, anonymous booking, no alternate identities
# ======================================================================
def test_unauthenticated_request_is_rejected_matching_existing_security_model():
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_anon_auth", user_id="someone")
    document_id = _issue_receipt(db, booking_id="bk_anon_auth")

    anon_client = _client()
    r = _download(anon_client, "bk_anon_auth", document_id)
    assert r.status_code == 401


def test_anonymous_booking_document_is_not_globally_downloadable():
    """A booking with no claimed owner must not become readable by an
    authenticated stranger just because ownership is null - no anonymous
    retrieval mechanism exists, and none is invented here."""
    client = _client()
    _register_and_login(client, "stranger@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_anonymous", user_id=None)
    document_id = _issue_receipt(db, booking_id="bk_anonymous")

    r = _download(client, "bk_anonymous", document_id)
    assert r.status_code == 404


def test_traveler_or_client_supplied_identity_headers_grant_no_access():
    """The route resolves identity only from the server-issued session
    cookie. A spoofed header claiming another identity must have zero
    effect - there is no code path that reads it."""
    client = _client()
    alice = _register_and_login(client, "alice9@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_spoof", user_id="the-real-owner")
    document_id = _issue_receipt(db, booking_id="bk_spoof")

    r = client.get(
        f"/api/v1/me/trips/bk_spoof/documents/{document_id}/download",
        headers={"X-User-Id": "the-real-owner", "X-Traveler-Email": "lead@example.com"},
    )
    assert r.status_code == 404
    assert alice != "the-real-owner"


# ======================================================================
# 14-16: immutability, refunds, credit notes
# ======================================================================
def test_historical_document_bytes_are_stable_after_a_credit_note_is_issued():
    client = _client()
    user_id = _register_and_login(client, "alice10@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_refund", user_id=user_id, total=150.0)
    receipt_id = _issue_receipt(db, booking_id="bk_refund")

    before = _download(client, "bk_refund", receipt_id).content

    fds.issue_credit_note(
        db, booking_id="bk_refund", refund_amount=150.0,
        original_document_id=receipt_id, idempotency_key="cn_idem_1",
    )

    after = _download(client, "bk_refund", receipt_id).content
    assert before == after


def test_credit_note_is_separately_retrievable():
    client = _client()
    user_id = _register_and_login(client, "alice11@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_cn", user_id=user_id, total=150.0)
    receipt_id = _issue_receipt(db, booking_id="bk_cn")
    credit_note = fds.issue_credit_note(
        db, booking_id="bk_cn", refund_amount=150.0,
        original_document_id=receipt_id, idempotency_key="cn_idem_2",
    )

    r = _download(client, "bk_cn", credit_note.document_id)
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")
    assert r.content != _download(client, "bk_cn", receipt_id).content

    listed = client.get("/api/v1/me/trips/bk_cn/documents").json()["documents"]
    assert {d["document_id"] for d in listed} == {receipt_id, credit_note.document_id}


# ======================================================================
# 17-18: safe error semantics
# ======================================================================
def test_missing_document_returns_safe_404():
    client = _client()
    user_id = _register_and_login(client, "alice12@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_missing", user_id=user_id)

    r = _download(client, "bk_missing", "findoc_does_not_exist")
    assert r.status_code == 404
    assert "traceback" not in r.text.lower()
    assert "sqlite" not in r.text.lower()


def test_corrupt_or_missing_artifact_fails_safely_not_500():
    client = _client()
    user_id = _register_and_login(client, "alice13@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_corrupt", user_id=user_id)
    document_id = _issue_receipt(db, booking_id="bk_corrupt")

    with db.write() as conn:
        conn.execute(
            "UPDATE financial_documents SET pdf_blob=NULL WHERE document_id=?",
            (document_id,),
        )

    r = _download(client, "bk_corrupt", document_id)
    assert r.status_code == 404
    assert "traceback" not in r.text.lower()


# ======================================================================
# 19-21: no side effects, idempotent repeated download
# ======================================================================
def test_download_does_not_mutate_booking_or_payment_state():
    client = _client()
    user_id = _register_and_login(client, "alice14@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_sideeffect", user_id=user_id)
    document_id = _issue_receipt(db, booking_id="bk_sideeffect")

    from detoura.persistence import payments as payment_store

    before_phase = bookings.get(db, "bk_sideeffect").phase
    before_payment_status = [
        p.status for p in payment_store.list_payments_for_booking(db, "bk_sideeffect")
    ]

    for _ in range(3):
        assert _download(client, "bk_sideeffect", document_id).status_code == 200

    after_phase = bookings.get(db, "bk_sideeffect").phase
    after_payment_status = [
        p.status for p in payment_store.list_payments_for_booking(db, "bk_sideeffect")
    ]
    assert before_phase == after_phase
    assert before_payment_status == after_payment_status


def test_repeated_download_is_byte_identical():
    client = _client()
    user_id = _register_and_login(client, "alice15@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_repeat", user_id=user_id)
    document_id = _issue_receipt(db, booking_id="bk_repeat")

    first = _download(client, "bk_repeat", document_id).content
    second = _download(client, "bk_repeat", document_id).content
    assert first == second


def test_concurrent_downloads_do_not_modify_artifact_or_state():
    client = _client()
    user_id = _register_and_login(client, "alice16@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_concurrent", user_id=user_id)
    document_id = _issue_receipt(db, booking_id="bk_concurrent")

    results: list[int] = []
    bodies: list[bytes] = []
    lock = threading.Lock()

    def _hit():
        r = _download(client, "bk_concurrent", document_id)
        with lock:
            results.append(r.status_code)
            bodies.append(r.content)

    threads = [threading.Thread(target=_hit) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [200] * 20
    assert len(set(bodies)) == 1

    from detoura.persistence import financial_documents as store
    assert store.get_document_pdf(db, document_id) == bodies[0]


# ======================================================================
# 22-23: no supplier/internal data leakage, filename sanitization
# ======================================================================
def test_document_metadata_never_leaks_internal_or_supplier_fields():
    client = _client()
    user_id = _register_and_login(client, "alice17@example.com")
    db = get_db()
    _seed_paid_booking(db, booking_id="bk_privacy", user_id=user_id)
    document_id = _issue_receipt(db, booking_id="bk_privacy")

    r = client.get(f"/api/v1/me/trips/bk_privacy/documents/{document_id}")
    body = r.json()
    forbidden = {
        "pdf_blob", "provider_order_id", "offer_id", "provider",
        "idempotency_key", "file_path", "storage_path",
    }
    assert forbidden.isdisjoint(body.keys())


def test_filename_sanitizer_strips_header_and_path_injection_characters():
    from detoura.api.me_trips import _safe_filename_component

    malicious = 'RCPT"\r\nSet-Cookie: evil=1\n../../etc/passwd'
    cleaned = _safe_filename_component(malicious)
    assert "\r" not in cleaned
    assert "\n" not in cleaned
    assert '"' not in cleaned
    assert "/" not in cleaned
    assert re.fullmatch(r"[A-Za-z0-9._-]+", cleaned)
