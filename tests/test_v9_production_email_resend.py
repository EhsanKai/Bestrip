"""V9 Production Transactional Email - Resend adapter + fail-closed
configuration + failure-injection tests.

No real Resend API key is used or required anywhere in this file - every
scenario is driven by a deterministic fake :class:`HttpClient`, mirroring
how ``tests/test_v9_phase4_stripe_provider.py``-style adapter tests (and
this project's own ``providers/stripe_payment.py``) are exercised without
a live provider.
"""

from __future__ import annotations

import json
import threading

import pytest

from detoura.communication_config import (
    CommunicationConfig,
    CommunicationConfigurationError,
    reset_communication_config,
    resolve_communication_provider,
)
from detoura.models.communication import CommunicationStatus
from detoura.providers.communication_provider import CommunicationSendResult
from detoura.providers.http import HttpResponse, ProviderHttpError
from detoura.providers.resend_email import (
    ResendConfigurationError,
    ResendEmailProvider,
    redact_key,
)
from detoura.providers.sandbox_email import SandboxEmailProvider

VALID_KEY = "re_test_1234567890abcdef"


class FakeHttpClient:
    """A scriptable :class:`~detoura.providers.http.HttpClient`.

    ``responses`` is a list of either an :class:`HttpResponse` or an
    exception instance to raise, consumed in order (one per call); the last
    entry repeats for any call beyond the list's length. Every call is
    recorded in ``calls`` (method, url, headers, body) for assertions -
    never asserted upon for header VALUES that would be a secret in a real
    log, but tests do inspect them directly here since this is a fake
    transport, not a log line.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "body": body})
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        outcome = self.responses[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _ok_response(message_id: str = "msg_abc123") -> HttpResponse:
    return HttpResponse(status=200, body=json.dumps({"id": message_id}), headers={})


def _error_response(status: int, *, name: str = "error", message: str = "failure") -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps({"name": name, "message": message}), headers={})


@pytest.fixture(autouse=True)
def _reset_config():
    reset_communication_config()
    yield
    reset_communication_config()


def _app_client(tmp_path, monkeypatch, db_name="resend_readyz.db"):
    from fastapi.testclient import TestClient

    import detoura.persistence.db as _db
    from detoura.api.app import create_app

    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / db_name))
    monkeypatch.setattr(_db, "_DB", None)
    return TestClient(create_app())


# ======================================================================
# Readiness: config-only check, never a live provider call (§22)
# ======================================================================
class TestReadinessReflectsCommunicationMisconfiguration:
    def test_ready_when_sandbox_default(self, tmp_path, monkeypatch):
        client = _app_client(tmp_path, monkeypatch)
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json() == {"status": "ready"}

    def test_ready_when_resend_fully_configured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COMMUNICATION_PROVIDER", "resend")
        monkeypatch.setenv("COMMUNICATION_LIVE_SENDING_ENABLED", "true")
        monkeypatch.setenv("RESEND_API_KEY", VALID_KEY)
        monkeypatch.setenv("RESEND_FROM_EMAIL", "bookings@detoura.app")
        client = _app_client(tmp_path, monkeypatch)
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json() == {"status": "ready"}

    def test_not_ready_when_resend_enabled_but_api_key_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COMMUNICATION_PROVIDER", "resend")
        monkeypatch.setenv("COMMUNICATION_LIVE_SENDING_ENABLED", "true")
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        monkeypatch.setenv("RESEND_FROM_EMAIL", "bookings@detoura.app")
        client = _app_client(tmp_path, monkeypatch)
        r = client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["status"] == "not_ready"

    def test_readiness_check_makes_no_real_network_call(self, tmp_path, monkeypatch):
        """A misconfigured-but-present key must not cause /readyz to try
        reaching api.resend.com - construction only validates shape."""
        import detoura.providers.http as http_module

        def _boom(*args, **kwargs):
            raise AssertionError("readiness must never make a real HTTP request")

        monkeypatch.setattr(http_module.UrllibHttpClient, "request", _boom)
        monkeypatch.setenv("COMMUNICATION_PROVIDER", "resend")
        monkeypatch.setenv("COMMUNICATION_LIVE_SENDING_ENABLED", "true")
        monkeypatch.setenv("RESEND_API_KEY", VALID_KEY)
        monkeypatch.setenv("RESEND_FROM_EMAIL", "bookings@detoura.app")
        client = _app_client(tmp_path, monkeypatch)
        r = client.get("/readyz")
        assert r.status_code == 200


# ======================================================================
# Construction / fail-closed configuration
# ======================================================================
class TestResendConstructionFailsClosed:
    def test_rejects_missing_key(self):
        with pytest.raises(ResendConfigurationError):
            ResendEmailProvider(api_key="", from_email="bookings@detoura.app")

    def test_rejects_key_without_expected_prefix(self):
        with pytest.raises(ResendConfigurationError):
            ResendEmailProvider(api_key="sk_not_a_resend_key", from_email="bookings@detoura.app")

    def test_rejects_missing_from_email(self):
        with pytest.raises(ResendConfigurationError):
            ResendEmailProvider(api_key=VALID_KEY, from_email="")

    def test_rejects_malformed_from_email(self):
        with pytest.raises(ResendConfigurationError):
            ResendEmailProvider(api_key=VALID_KEY, from_email="not-an-email")

    def test_accepts_valid_configuration(self):
        provider = ResendEmailProvider(
            api_key=VALID_KEY, from_email="bookings@detoura.app",
            http=FakeHttpClient([_ok_response()]),
        )
        assert provider.name == "resend"

    def test_capabilities_declared_truthfully(self):
        provider = ResendEmailProvider(
            api_key=VALID_KEY, from_email="bookings@detoura.app",
            http=FakeHttpClient([_ok_response()]),
        )
        caps = provider.capabilities()
        assert caps.supports_idempotency_keys is True
        assert caps.max_retries_recommended >= 0

    def test_conforms_to_communication_provider_protocol(self):
        from detoura.providers.communication_provider import CommunicationProvider

        provider = ResendEmailProvider(
            api_key=VALID_KEY, from_email="bookings@detoura.app",
            http=FakeHttpClient([_ok_response()]),
        )
        assert isinstance(provider, CommunicationProvider)


class TestRedactKey:
    def test_redacts_real_key(self):
        assert redact_key(VALID_KEY) == "re_<redacted>"
        assert VALID_KEY not in redact_key(VALID_KEY)

    def test_handles_missing_key(self):
        assert redact_key(None) == "<no key>"
        assert redact_key("") == "<no key>"

    def test_handles_unrecognised_shape(self):
        assert redact_key("sk_live_whatever") == "<unrecognised key format>"


# ======================================================================
# resolve_communication_provider - fail-closed chain (mirrors payment_config)
# ======================================================================
class TestResolveCommunicationProviderFailClosed:
    def test_default_config_is_sandbox(self):
        provider = resolve_communication_provider(CommunicationConfig())
        assert isinstance(provider, SandboxEmailProvider)

    def test_live_sending_disabled_forces_sandbox_even_if_provider_is_resend(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", VALID_KEY)
        monkeypatch.setenv("RESEND_FROM_EMAIL", "bookings@detoura.app")
        cfg = CommunicationConfig(provider="resend", live_sending_enabled=False)
        provider = resolve_communication_provider(cfg)
        assert isinstance(provider, SandboxEmailProvider)

    def test_explicit_sandbox_choice_wins_even_with_live_sending_enabled(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", VALID_KEY)
        monkeypatch.setenv("RESEND_FROM_EMAIL", "bookings@detoura.app")
        cfg = CommunicationConfig(provider="sandbox", live_sending_enabled=True)
        provider = resolve_communication_provider(cfg)
        assert isinstance(provider, SandboxEmailProvider)

    def test_resend_live_missing_api_key_raises_not_falls_back(self, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        monkeypatch.setenv("RESEND_FROM_EMAIL", "bookings@detoura.app")
        cfg = CommunicationConfig(provider="resend", live_sending_enabled=True)
        with pytest.raises(ResendConfigurationError):
            resolve_communication_provider(cfg)

    def test_resend_live_missing_from_email_raises_not_falls_back(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", VALID_KEY)
        monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
        cfg = CommunicationConfig(provider="resend", live_sending_enabled=True)
        with pytest.raises(ResendConfigurationError):
            resolve_communication_provider(cfg)

    def test_resend_live_fully_configured_resolves_real_adapter(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", VALID_KEY)
        monkeypatch.setenv("RESEND_FROM_EMAIL", "bookings@detoura.app")
        cfg = CommunicationConfig(provider="resend", live_sending_enabled=True)
        provider = resolve_communication_provider(cfg)
        assert isinstance(provider, ResendEmailProvider)
        assert provider.name == "resend"

    def test_unknown_provider_name_raises(self, monkeypatch):
        cfg = CommunicationConfig(provider="postmark", live_sending_enabled=True)
        with pytest.raises(CommunicationConfigurationError):
            resolve_communication_provider(cfg)

    def test_config_object_never_carries_the_api_key(self, monkeypatch):
        """The frozen config's own fields must never include the secret -
        it is read directly from the environment only at provider-
        construction time (§7: 'API key not persisted', extended here to
        'not even held on the in-memory config object callers can inspect')."""
        import dataclasses

        monkeypatch.setenv("RESEND_API_KEY", VALID_KEY)
        cfg = CommunicationConfig.from_env()
        assert VALID_KEY not in repr(cfg)
        assert VALID_KEY not in str(dataclasses.astuple(cfg))


# ======================================================================
# send() - definitive outcomes
# ======================================================================
class TestResendSendOutcomes:
    def _provider(self, http):
        return ResendEmailProvider(api_key=VALID_KEY, from_email="bookings@detoura.app", http=http)

    def test_success_returns_sent(self):
        http = FakeHttpClient([_ok_response("msg_success_1")])
        provider = self._provider(http)
        result = provider.send(
            idempotency_key="idem-1", recipient="traveler@example.com",
            subject="Your trip", body_text="Confirmed.", reference="comm_1",
        )
        assert result.ok is True
        assert result.unknown is False
        assert result.provider_message_id == "msg_success_1"

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 422, 429])
    def test_definitive_rejection_statuses_are_failed_not_unknown(self, status):
        http = FakeHttpClient([_error_response(status)])
        provider = self._provider(http)
        result = provider.send(
            idempotency_key="idem-2", recipient="traveler@example.com",
            subject="Your trip", body_text="Confirmed.", reference="comm_2",
        )
        assert result.ok is False
        assert result.unknown is False, f"status {status} must be a definitive failure, not UNKNOWN"

    @pytest.mark.parametrize("status", [409, 500, 502, 503])
    def test_ambiguous_statuses_are_unknown_never_assumed_failed(self, status):
        """409 (idempotency conflict) and 5xx: Resend responded, but a send
        may have been queued before the error - never collapsed to FAILED
        (§9, module docstring)."""
        http = FakeHttpClient([_error_response(status)])
        provider = self._provider(http)
        result = provider.send(
            idempotency_key="idem-3", recipient="traveler@example.com",
            subject="Your trip", body_text="Confirmed.", reference="comm_3",
        )
        assert result.unknown is True
        assert result.ok is False

    def test_connection_timeout_is_unknown(self):
        http = FakeHttpClient([TimeoutError("connection timed out")])
        provider = self._provider(http)
        result = provider.send(
            idempotency_key="idem-4", recipient="traveler@example.com",
            subject="Your trip", body_text="Confirmed.", reference="comm_4",
        )
        assert result.unknown is True
        assert result.provider_message_id is None

    def test_provider_http_error_is_unknown(self):
        http = FakeHttpClient([ProviderHttpError("could not reach api.resend.com")])
        provider = self._provider(http)
        result = provider.send(
            idempotency_key="idem-5", recipient="traveler@example.com",
            subject="Your trip", body_text="Confirmed.", reference="comm_5",
        )
        assert result.unknown is True

    def test_malformed_json_2xx_body_is_unknown_not_success(self):
        http = FakeHttpClient([HttpResponse(status=200, body="not json at all", headers={})])
        provider = self._provider(http)
        result = provider.send(
            idempotency_key="idem-6", recipient="traveler@example.com",
            subject="Your trip", body_text="Confirmed.", reference="comm_6",
        )
        assert result.unknown is True
        assert result.ok is False

    def test_2xx_missing_id_is_unknown_not_success(self):
        http = FakeHttpClient([HttpResponse(status=200, body=json.dumps({"no_id_here": True}), headers={})])
        provider = self._provider(http)
        result = provider.send(
            idempotency_key="idem-7", recipient="traveler@example.com",
            subject="Your trip", body_text="Confirmed.", reference="comm_7",
        )
        assert result.unknown is True


# ======================================================================
# Idempotency-Key plumbing
# ======================================================================
class TestResendIdempotencyHeader:
    def test_idempotency_key_forwarded_verbatim(self):
        http = FakeHttpClient([_ok_response()])
        provider = ResendEmailProvider(api_key=VALID_KEY, from_email="bookings@detoura.app", http=http)
        provider.send(
            idempotency_key="stable-key-42", recipient="traveler@example.com",
            subject="Your trip", body_text="Confirmed.", reference="comm_x",
        )
        assert http.calls[0]["headers"]["Idempotency-Key"] == "stable-key-42"

    def test_idempotency_key_not_derived_from_recipient_alone(self):
        """Two different communications to the SAME recipient must not
        collide on idempotency - the key comes from the caller
        (communication_id:attempt_number in communication_service.py), not
        from the recipient address (§10)."""
        http = FakeHttpClient([_ok_response("a"), _ok_response("b")])
        provider = ResendEmailProvider(api_key=VALID_KEY, from_email="bookings@detoura.app", http=http)
        provider.send(
            idempotency_key="comm_1:1", recipient="same@example.com",
            subject="A", body_text="A", reference="comm_1",
        )
        provider.send(
            idempotency_key="comm_2:1", recipient="same@example.com",
            subject="B", body_text="B", reference="comm_2",
        )
        keys = [c["headers"]["Idempotency-Key"] for c in http.calls]
        assert keys == ["comm_1:1", "comm_2:1"]
        assert len(set(keys)) == 2

    def test_authorization_header_uses_bearer_scheme_and_never_appears_in_result(self):
        http = FakeHttpClient([_ok_response()])
        provider = ResendEmailProvider(api_key=VALID_KEY, from_email="bookings@detoura.app", http=http)
        result = provider.send(
            idempotency_key="idem-8", recipient="traveler@example.com",
            subject="Your trip", body_text="Confirmed.", reference="comm_8",
        )
        assert http.calls[0]["headers"]["Authorization"] == f"Bearer {VALID_KEY}"
        assert VALID_KEY not in result.detail
        assert VALID_KEY not in (result.status or "")


# ======================================================================
# retrieve() - reconciliation
# ======================================================================
class TestResendRetrieve:
    def _provider(self, http):
        return ResendEmailProvider(api_key=VALID_KEY, from_email="bookings@detoura.app", http=http)

    @pytest.mark.parametrize("last_event", ["sent", "delivered", "bounced", "complained", "delivery_delayed"])
    def test_sent_family_events_resolve_to_ok(self, last_event):
        http = FakeHttpClient([HttpResponse(status=200, body=json.dumps({"last_event": last_event}), headers={})])
        result = self._provider(http).retrieve(provider_message_id="msg_1")
        assert result.ok is True
        assert result.unknown is False

    def test_failed_event_resolves_to_definitive_failure(self):
        http = FakeHttpClient([HttpResponse(status=200, body=json.dumps({"last_event": "failed"}), headers={})])
        result = self._provider(http).retrieve(provider_message_id="msg_2")
        assert result.ok is False
        assert result.unknown is False

    def test_not_found_is_definitive_not_unknown(self):
        http = FakeHttpClient([HttpResponse(status=404, body="{}", headers={})])
        result = self._provider(http).retrieve(provider_message_id="msg_missing")
        assert result.status == "not_found"
        assert result.unknown is False

    def test_unrecognised_last_event_stays_unknown_never_guessed(self):
        http = FakeHttpClient([HttpResponse(status=200, body=json.dumps({"last_event": "some_new_future_event"}), headers={})])
        result = self._provider(http).retrieve(provider_message_id="msg_3")
        assert result.unknown is True

    def test_retrieve_timeout_stays_unknown(self):
        http = FakeHttpClient([TimeoutError("timed out")])
        result = self._provider(http).retrieve(provider_message_id="msg_4")
        assert result.unknown is True


# ======================================================================
# Content / secret safety
# ======================================================================
class TestResendContentAndSecretSafety:
    def test_no_html_field_is_ever_sent(self):
        """The communication interface only carries body_text (plain text,
        per models/communication.py's own render function docstring) - this
        adapter must never introduce an HTML rendering path in this slice
        (§18: no new template/HTML surface, no injection risk from one)."""
        http = FakeHttpClient([_ok_response()])
        provider = ResendEmailProvider(api_key=VALID_KEY, from_email="bookings@detoura.app", http=http)
        provider.send(
            idempotency_key="idem-9", recipient="traveler@example.com",
            subject="<script>alert(1)</script>", body_text="Plain text body, not HTML.",
            reference="comm_9",
        )
        sent_body = json.loads(http.calls[0]["body"])
        assert "html" not in sent_body
        assert sent_body["text"] == "Plain text body, not HTML."

    def test_recipient_is_a_fixed_list_not_customer_expandable(self):
        http = FakeHttpClient([_ok_response()])
        provider = ResendEmailProvider(api_key=VALID_KEY, from_email="bookings@detoura.app", http=http)
        provider.send(
            idempotency_key="idem-10", recipient="one@example.com",
            subject="S", body_text="B", reference="comm_10",
        )
        sent_body = json.loads(http.calls[0]["body"])
        assert sent_body["to"] == ["one@example.com"]

    def test_fixed_endpoint_host_never_customer_controlled(self):
        http = FakeHttpClient([_ok_response()])
        provider = ResendEmailProvider(api_key=VALID_KEY, from_email="bookings@detoura.app", http=http)
        provider.send(
            idempotency_key="idem-11", recipient="one@example.com",
            subject="S", body_text="B", reference="comm_11",
        )
        assert http.calls[0]["url"] == "https://api.resend.com/emails"

    def test_exception_message_never_contains_the_api_key(self):
        """Even a raised ResendConfigurationError must never leak a key
        fragment - construct with a key that DOES start with the expected
        prefix but fails a later check, to prove the from_email failure
        path (which fires after the key check) can't accidentally echo it."""
        try:
            ResendEmailProvider(api_key=VALID_KEY, from_email="")
        except ResendConfigurationError as exc:
            assert VALID_KEY not in str(exc)
        else:
            pytest.fail("expected ResendConfigurationError")


# ======================================================================
# Concurrency: two overlapping sends never duplicate a customer email
# ======================================================================
class TestResendConcurrency:
    def test_concurrent_create_and_send_never_double_sends_via_resend(self, in_memory_communication_db):
        """Mirrors test_v9_phase5_integration.py's concurrent-finalization
        discipline, but with the Resend adapter's own HTTP shape: many
        threads racing create_and_send_communication for the SAME booking
        must result in exactly one real Resend POST /emails call, because
        the existing (booking_id, communication_type) UNIQUE constraint and
        claim_send_slot() are what prevent the duplicate - the adapter
        itself does nothing special here, which is exactly the point: it
        does not need to, because it never bypasses that architecture."""
        from detoura.services import communication_service as cs

        http = FakeHttpClient([_ok_response(f"msg_{i}") for i in range(20)])
        provider = ResendEmailProvider(api_key=VALID_KEY, from_email="bookings@detoura.app", http=http)

        db = in_memory_communication_db
        results = []
        errors = []

        def _attempt():
            try:
                comm = cs.create_and_send_communication(
                    db, booking_id="booking_concurrent_1", journey_reference="JRN-X",
                    user_id="user_1", recipient_address="traveler@example.com",
                    subject="Your trip", body_text="Confirmed.",
                    idempotency_key="comm:booking_concurrent_1:confirmation",
                    provider=provider,
                )
                results.append(comm)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"unexpected errors: {errors}"
        # Exactly one real HTTP call reached the fake Resend transport,
        # regardless of how many threads raced to create/send.
        assert len(http.calls) == 1
        communication_ids = {c.communication_id for c in results}
        assert len(communication_ids) == 1


@pytest.fixture
def in_memory_communication_db():
    import sqlite3
    from detoura.persistence.db import Database

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE customer_communications (
            communication_id TEXT PRIMARY KEY,
            booking_id TEXT NOT NULL,
            journey_reference TEXT NOT NULL,
            user_id TEXT,
            channel TEXT NOT NULL,
            communication_type TEXT NOT NULL,
            status TEXT NOT NULL,
            recipient_address TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(booking_id, communication_type)
        );
        CREATE TABLE communication_attempts (
            attempt_id TEXT PRIMARY KEY,
            communication_id TEXT NOT NULL REFERENCES customer_communications(communication_id),
            attempt_number INTEGER NOT NULL,
            status TEXT NOT NULL,
            provider_name TEXT NOT NULL,
            provider_message_id TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(communication_id, attempt_number)
        );
        CREATE TABLE communication_events (
            event_id TEXT PRIMARY KEY,
            communication_id TEXT NOT NULL REFERENCES customer_communications(communication_id),
            attempt_id TEXT REFERENCES communication_attempts(attempt_id),
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            detail TEXT DEFAULT '',
            data_json TEXT DEFAULT '{}'
        );
    """)
    conn.commit()
    db = Database.__new__(Database)
    db._conn = conn
    db._lock = threading.Lock()
    return db


# ======================================================================
# UNKNOWN is never blindly resent
# ======================================================================
class TestUnknownNeverBlindlyResent:
    def test_reconcile_communication_does_not_resend_it_only_retrieves(self, in_memory_communication_db):
        """reconcile_communication must call provider.retrieve(), never
        provider.send() again - proven here by a fake whose send() would
        raise if called a second time."""
        from datetime import datetime, timezone

        from detoura.persistence import communications as store
        from detoura.services import communication_service as cs

        class OneSendOnly:
            name = "resend"

            def __init__(self):
                self._sent = False

            def capabilities(self):
                from detoura.providers.communication_provider import CommunicationProviderCapabilities
                return CommunicationProviderCapabilities(
                    supports_delivery_events=True, supports_idempotency_keys=True, max_retries_recommended=2,
                )

            def send(self, **kwargs):
                if self._sent:
                    raise AssertionError("send() must not be called again - reconciliation must use retrieve() only")
                self._sent = True
                return CommunicationSendResult(
                    ok=False, unknown=True, provider_message_id=None, status="unknown",
                    detail="resend: send request timed out",
                )

            def retrieve(self, *, provider_message_id):
                return CommunicationSendResult(
                    ok=True, unknown=False, provider_message_id=provider_message_id, status="success",
                    detail="resend: last_event=sent",
                )

        db = in_memory_communication_db
        provider = OneSendOnly()
        comm = cs.create_and_send_communication(
            db, booking_id="booking_unknown_1", journey_reference="JRN-Y", user_id="user_1",
            recipient_address="traveler@example.com", subject="S", body_text="B",
            idempotency_key="comm:booking_unknown_1:confirmation", provider=provider,
        )
        assert comm.status == CommunicationStatus.UNKNOWN

        # No provider_message_id was ever produced (a true network-level
        # ambiguity - see resend_email.py's module docstring) - reconcile
        # has nothing to retrieve against and must leave it UNKNOWN rather
        # than guess or resend.
        reconciled = cs.reconcile_communication(db, communication=comm, provider=provider)
        assert reconciled.status == CommunicationStatus.UNKNOWN
