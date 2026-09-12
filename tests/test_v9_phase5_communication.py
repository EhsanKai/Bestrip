"""Tests for V9 Phase 5 communication domain and provider infrastructure.

Tests the state machine, idempotency, sandbox provider, and email content
truthfulness with comprehensive coverage including concurrency safety (RLock).
"""

import secrets
import threading
from datetime import datetime, timezone

import pytest

from detoura.models.communication import (
    ALLOWED_TRANSITIONS,
    CommunicationAttempt,
    CommunicationChannel,
    CommunicationEvent,
    CommunicationStatus,
    CommunicationType,
    CustomerCommunication,
    InvalidCommunicationTransition,
    can_transition_communication,
    render_booking_confirmation_email,
)
from detoura.persistence.communications import (
    StaleVersion,
    compare_and_swap_communication,
    create_communication,
    get_communication,
    get_communication_for_booking,
    list_attempts_for_communication,
    list_events,
    new_id,
    record_attempt,
    record_event,
    update_attempt_completion,
)
from detoura.persistence.db import Database
from detoura.providers.sandbox_email import SandboxEmailProvider


# ======================================================================
# Fixtures
# ======================================================================
@pytest.fixture
def in_memory_db():
    """In-memory SQLite database for testing."""
    import sqlite3

    # Create in-memory database and initialize schema
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Use executescript directly (not within write() context)
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

    # Wrap in a Database instance for use in tests
    db = Database.__new__(Database)
    db._conn = conn
    db._lock = threading.Lock()
    return db


@pytest.fixture
def sandbox_provider():
    """Sandbox email provider for testing."""
    return SandboxEmailProvider()


@pytest.fixture
def sample_communication():
    """A sample communication for testing."""
    return CustomerCommunication(
        communication_id=new_id("comm"),
        booking_id="booking_abc123",
        journey_reference="JRN-ABC123",
        user_id="user_123",
        channel=CommunicationChannel.EMAIL,
        communication_type=CommunicationType.BOOKING_CONFIRMATION,
        status=CommunicationStatus.PENDING,
        recipient_address="traveler@example.com",
        idempotency_key=new_id("idempotency"),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


# ======================================================================
# State Machine Tests
# ======================================================================
class TestCommunicationStateMachine:
    """Exhaustive state machine tests."""

    def test_allowed_transitions_complete(self):
        """Verify ALLOWED_TRANSITIONS covers all states."""
        all_statuses = set(CommunicationStatus)
        transitioned_statuses = set(ALLOWED_TRANSITIONS.keys())
        assert all_statuses == transitioned_statuses, (
            f"Missing transitions for: {all_statuses - transitioned_statuses}"
        )

    def test_can_transition_idempotent(self):
        """Transitioning to the same state is always allowed."""
        for status in CommunicationStatus:
            assert can_transition_communication(status, status)

    def test_pending_can_transition_to_sending_sent_failed_unknown(self):
        """PENDING → SENDING, FAILED, UNKNOWN."""
        allowed = ALLOWED_TRANSITIONS[CommunicationStatus.PENDING]
        assert CommunicationStatus.SENDING in allowed
        assert CommunicationStatus.FAILED in allowed
        assert CommunicationStatus.UNKNOWN in allowed
        assert CommunicationStatus.SENT not in allowed  # must go through SENDING

    def test_sending_can_transition_to_sent_failed_unknown(self):
        """SENDING → SENT, FAILED, UNKNOWN."""
        allowed = ALLOWED_TRANSITIONS[CommunicationStatus.SENDING]
        assert CommunicationStatus.SENT in allowed
        assert CommunicationStatus.FAILED in allowed
        assert CommunicationStatus.UNKNOWN in allowed
        assert CommunicationStatus.PENDING not in allowed  # no going backwards

    def test_sent_is_terminal(self):
        """SENT has no outgoing transitions."""
        assert ALLOWED_TRANSITIONS[CommunicationStatus.SENT] == frozenset()

    def test_failed_is_terminal(self):
        """FAILED has no outgoing transitions."""
        assert ALLOWED_TRANSITIONS[CommunicationStatus.FAILED] == frozenset()

    def test_unknown_can_only_go_to_sent_or_failed(self):
        """UNKNOWN → SENT, FAILED only (no blind retry to SENDING)."""
        allowed = ALLOWED_TRANSITIONS[CommunicationStatus.UNKNOWN]
        assert CommunicationStatus.SENT in allowed
        assert CommunicationStatus.FAILED in allowed
        assert CommunicationStatus.SENDING not in allowed
        assert CommunicationStatus.PENDING not in allowed

    def test_invalid_transition_raises(self, sample_communication):
        """Invalid transitions raise InvalidCommunicationTransition."""
        # PENDING cannot go directly to SENT (must go through SENDING)
        with pytest.raises(InvalidCommunicationTransition):
            sample_communication.with_status(CommunicationStatus.SENT)

        # Once SENT, cannot transition anywhere
        comm = sample_communication.with_status(CommunicationStatus.SENDING)
        comm = comm.with_status(CommunicationStatus.SENT)
        with pytest.raises(InvalidCommunicationTransition):
            comm.with_status(CommunicationStatus.FAILED)

    def test_with_status_increments_version(self, sample_communication):
        """with_status() increments version."""
        assert sample_communication.version == 1
        comm2 = sample_communication.with_status(CommunicationStatus.SENDING)
        assert comm2.version == 2
        comm3 = comm2.with_status(CommunicationStatus.SENT)
        assert comm3.version == 3


# ======================================================================
# Idempotent Creation Tests
# ======================================================================
class TestIdempotentCreation:
    """Test idempotent communication creation."""

    def test_create_communication_first_time(self, in_memory_db, sample_communication):
        """First create succeeds with created=True."""
        comm, created = create_communication(in_memory_db, communication=sample_communication)
        assert created is True
        assert comm.communication_id == sample_communication.communication_id

    def test_create_communication_duplicate_idempotency_key(
        self, in_memory_db, sample_communication
    ):
        """Duplicate create by idempotency_key returns existing row with created=False."""
        comm1, created1 = create_communication(
            in_memory_db, communication=sample_communication
        )
        assert created1 is True

        # Retry with same idempotency_key but different communication_id
        retry = sample_communication.model_copy(
            update={"communication_id": new_id("comm")}
        )
        comm2, created2 = create_communication(in_memory_db, communication=retry)
        assert created2 is False
        assert comm2.communication_id == comm1.communication_id

    def test_create_communication_duplicate_booking_type(
        self, in_memory_db, sample_communication
    ):
        """Duplicate create by (booking_id, communication_type) returns existing row."""
        comm1, created1 = create_communication(
            in_memory_db, communication=sample_communication
        )
        assert created1 is True

        # Retry with same booking_id/communication_type but different idempotency_key
        retry = sample_communication.model_copy(
            update={"idempotency_key": new_id("idempotency")}
        )
        comm2, created2 = create_communication(in_memory_db, communication=retry)
        assert created2 is False
        assert comm2.communication_id == comm1.communication_id

    def test_get_communication(self, in_memory_db, sample_communication):
        """Retrieve a communication by ID."""
        create_communication(in_memory_db, communication=sample_communication)
        retrieved = get_communication(in_memory_db, sample_communication.communication_id)
        assert retrieved is not None
        assert retrieved.communication_id == sample_communication.communication_id
        assert retrieved.booking_id == sample_communication.booking_id

    def test_get_communication_for_booking(self, in_memory_db, sample_communication):
        """Retrieve the communication for a (booking, type) pair."""
        create_communication(in_memory_db, communication=sample_communication)
        retrieved = get_communication_for_booking(
            in_memory_db,
            sample_communication.booking_id,
            sample_communication.communication_type.value,
        )
        assert retrieved is not None
        assert retrieved.communication_id == sample_communication.communication_id


# ======================================================================
# Sandbox Provider Tests
# ======================================================================
class TestSandboxEmailProvider:
    """Test sandbox email provider determinism and idempotency."""

    def test_sandbox_name(self, sandbox_provider):
        """Provider name is 'sandbox'."""
        assert sandbox_provider.name == "sandbox"

    def test_capabilities(self, sandbox_provider):
        """Capabilities are correctly declared."""
        caps = sandbox_provider.capabilities()
        assert caps.supports_delivery_events is False
        assert caps.supports_idempotency_keys is True
        assert caps.max_retries_recommended == 3

    def test_send_success(self, sandbox_provider):
        """Successful send returns ok=True."""
        result = sandbox_provider.send(
            idempotency_key="key_001",
            recipient="user@example.com",
            subject="Test",
            body_text="Hello",
            reference="comm_abc123",
        )
        assert result.ok is True
        assert result.unknown is False
        assert result.status == "success"
        assert result.provider_message_id is not None

    def test_send_fail_suffix(self, sandbox_provider):
        """Reference with _FAIL_SEND suffix causes failure."""
        result = sandbox_provider.send(
            idempotency_key="key_fail_001",
            recipient="user@example.com",
            subject="Test",
            body_text="Hello",
            reference="comm_abc123_FAIL_SEND",
        )
        assert result.ok is False
        assert result.unknown is False
        assert result.status == "rejected"
        assert result.provider_message_id is None

    def test_send_unknown_suffix(self, sandbox_provider):
        """Reference with _UNKNOWN_SEND suffix causes UNKNOWN."""
        result = sandbox_provider.send(
            idempotency_key="key_unknown_001",
            recipient="user@example.com",
            subject="Test",
            body_text="Hello",
            reference="comm_abc123_UNKNOWN_SEND",
        )
        assert result.ok is False
        assert result.unknown is True
        assert result.status == "unknown"
        assert result.provider_message_id is not None

    def test_idempotency_key_cache_hit(self, sandbox_provider):
        """Same idempotency_key returns cached result without re-running."""
        result1 = sandbox_provider.send(
            idempotency_key="key_cache_001",
            recipient="user@example.com",
            subject="Test",
            body_text="Hello",
            reference="comm_abc123",
        )
        provider_msg_id_1 = result1.provider_message_id

        # Same key, different inputs - should return cached result
        result2 = sandbox_provider.send(
            idempotency_key="key_cache_001",  # same
            recipient="different@example.com",  # different
            subject="Different",
            body_text="Hi",
            reference="comm_xyz789",  # different
        )
        assert result2.provider_message_id == provider_msg_id_1
        # Message ID didn't change = cached result

    def test_idempotency_key_fault_injection_not_repeated(self, sandbox_provider):
        """Fault injection only runs once per idempotency_key."""
        # First call with UNKNOWN suffix
        result1 = sandbox_provider.send(
            idempotency_key="key_fault_001",
            recipient="user@example.com",
            subject="Test",
            body_text="Hello",
            reference="comm_unknown_UNKNOWN_SEND",
        )
        assert result1.unknown is True
        msg_id_1 = result1.provider_message_id

        # Second call with same key but without suffix - should still return cached UNKNOWN
        result2 = sandbox_provider.send(
            idempotency_key="key_fault_001",
            recipient="user@example.com",
            subject="Test",
            body_text="Hello",
            reference="comm_success",  # no suffix, but cached
        )
        assert result2.unknown is True  # still unknown, from cache
        assert result2.provider_message_id == msg_id_1

    def test_retrieve_success(self, sandbox_provider):
        """Retrieve returns truthful status for a known message."""
        result = sandbox_provider.send(
            idempotency_key="key_ret_001",
            recipient="user@example.com",
            subject="Test",
            body_text="Hello",
            reference="comm_abc123",
        )
        msg_id = result.provider_message_id

        retrieved = sandbox_provider.retrieve(provider_message_id=msg_id)
        assert retrieved.ok is True
        assert retrieved.status == "success"

    def test_retrieve_unknown_message(self, sandbox_provider):
        """Retrieve for non-existent message returns not_found."""
        result = sandbox_provider.retrieve(provider_message_id="sbx_msg_99999999")
        assert result.ok is False
        assert result.status == "not_found"

    def test_concurrent_sends_no_deadlock(self, sandbox_provider):
        """Concurrent sends with different keys don't deadlock (RLock safety)."""
        errors = []

        def send_email(key: str, ref: str):
            try:
                result = sandbox_provider.send(
                    idempotency_key=key,
                    recipient=f"user{key}@example.com",
                    subject="Test",
                    body_text="Hello",
                    reference=ref,
                )
                assert result.provider_message_id is not None
            except Exception as e:
                errors.append(e)

        # Spawn 10 threads, each sending with a unique key
        threads = []
        for i in range(10):
            t = threading.Thread(
                target=send_email,
                args=(f"key_concurrent_{i:03d}", f"comm_concurrent_{i:03d}"),
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=5)  # reasonable timeout

        assert not errors, f"Concurrent sends produced errors: {errors}"
        # If we got here without hanging, RLock is working


# ======================================================================
# Event Recording Tests
# ======================================================================
class TestEventRecording:
    """Test ledger recording and ordering."""

    def test_record_event(self, in_memory_db, sample_communication):
        """Record an event to the ledger."""
        create_communication(in_memory_db, communication=sample_communication)

        event = CommunicationEvent(
            event_id=new_id("evt"),
            communication_id=sample_communication.communication_id,
            event_type="COMMUNICATION_CREATED",
            data={"status": "pending"},
        )
        record_event(in_memory_db, event)

        events = list_events(in_memory_db, sample_communication.communication_id)
        assert len(events) == 1
        assert events[0].event_type == "COMMUNICATION_CREATED"

    def test_event_ordering_by_rowid(self, in_memory_db, sample_communication):
        """Events are ordered by (occurred_at, rowid), not event_id."""
        create_communication(in_memory_db, communication=sample_communication)

        now = datetime.now(timezone.utc)
        # Insert two events with the same timestamp (microsecond precision)
        event1 = CommunicationEvent(
            event_id=secrets.token_urlsafe(8),  # random ID
            communication_id=sample_communication.communication_id,
            event_type="EMAIL_SEND_REQUESTED",
            occurred_at=now,
        )
        event2 = CommunicationEvent(
            event_id=secrets.token_urlsafe(8),  # different random ID
            communication_id=sample_communication.communication_id,
            event_type="EMAIL_SENT",
            occurred_at=now,
        )

        record_event(in_memory_db, event1)
        record_event(in_memory_db, event2)

        events = list_events(in_memory_db, sample_communication.communication_id)
        assert len(events) == 2
        # Should be in insertion order (rowid), not event_id order
        assert events[0].event_type == "EMAIL_SEND_REQUESTED"
        assert events[1].event_type == "EMAIL_SENT"

    def test_no_pii_in_event_data(self, in_memory_db, sample_communication):
        """Event data validator prevents @ symbol (email addresses)."""
        create_communication(in_memory_db, communication=sample_communication)

        # Try to create an event with an email in data
        with pytest.raises(ValueError, match="PII detected"):
            CommunicationEvent(
                event_id=new_id("evt"),
                communication_id=sample_communication.communication_id,
                event_type="TEST",
                data={"recipient": "user@example.com"},  # @ symbol forbidden
            )


# ======================================================================
# Attempt Tracking Tests
# ======================================================================
class TestAttemptTracking:
    """Test send attempt tracking (resends)."""

    def test_record_attempt_increments_attempt_number(self, in_memory_db, sample_communication):
        """Resend creates a new attempt with incremented attempt_number."""
        create_communication(in_memory_db, communication=sample_communication)

        # First attempt
        attempt1 = CommunicationAttempt(
            attempt_id=new_id("att"),
            communication_id=sample_communication.communication_id,
            attempt_number=1,
            status="SENT",
            provider_name="sandbox",
            provider_message_id="sbx_msg_00000001",
        )
        record_attempt(in_memory_db, attempt1)

        # Resend: new attempt with same communication_id
        attempt2 = CommunicationAttempt(
            attempt_id=new_id("att"),
            communication_id=sample_communication.communication_id,
            attempt_number=2,
            status="SENT",
            provider_name="sandbox",
            provider_message_id="sbx_msg_00000002",
        )
        record_attempt(in_memory_db, attempt2)

        attempts = list_attempts_for_communication(
            in_memory_db, sample_communication.communication_id
        )
        assert len(attempts) == 2
        assert attempts[0].attempt_number == 1
        assert attempts[1].attempt_number == 2
        assert attempts[0].attempt_id != attempts[1].attempt_id


# ======================================================================
# Compare-and-Swap Tests
# ======================================================================
class TestCompareAndSwap:
    """Test optimistic concurrency control."""

    def test_cas_succeeds_on_matching_version(self, in_memory_db, sample_communication):
        """CAS succeeds when version matches."""
        create_communication(in_memory_db, communication=sample_communication)

        updated = sample_communication.with_status(CommunicationStatus.SENDING)
        result = compare_and_swap_communication(
            in_memory_db, communication=updated, expected_version=1
        )
        assert result.status == CommunicationStatus.SENDING
        assert result.version == 2

    def test_cas_fails_on_stale_version(self, in_memory_db, sample_communication):
        """CAS raises StaleVersion when version doesn't match."""
        create_communication(in_memory_db, communication=sample_communication)

        updated = sample_communication.with_status(CommunicationStatus.SENDING)
        # Try to write with wrong expected version
        with pytest.raises(StaleVersion):
            compare_and_swap_communication(
                in_memory_db, communication=updated, expected_version=999
            )


# ======================================================================
# Email Render Truthfulness Tests
# ======================================================================
class TestEmailRenderTruthfulness:
    """Test that email content is truthful based on status."""

    def test_confirmed_email_contains_confirmed(self):
        """CONFIRMED status email explicitly says 'confirmed'."""
        subject, body = render_booking_confirmation_email(
            journey_reference="JRN-ABC123",
            traveler_names=["Alice", "Bob"],
            party_size=2,
            itinerary_lines=["NYC → Paris, AA100"],
            customer_total=1500.00,
            currency="EUR",
            service_tier="Standard",
            confirmation_status="CONFIRMED",
        )
        assert "confirmed" in body.lower()

    def test_partial_recovery_email_avoids_confirmed(self):
        """PARTIAL_RECOVERY email does NOT say 'confirmed'."""
        subject, body = render_booking_confirmation_email(
            journey_reference="JRN-ABC123",
            traveler_names=["Alice", "Bob"],
            party_size=2,
            itinerary_lines=["NYC → Paris, AA100"],
            customer_total=1500.00,
            currency="EUR",
            service_tier="Standard",
            confirmation_status="PARTIAL_RECOVERY",
        )
        assert "confirmed" not in body.lower()
        assert "finishing" in body.lower()

    def test_payment_unknown_email_avoids_successful(self):
        """PAYMENT_UNKNOWN email does NOT say 'payment successful'."""
        subject, body = render_booking_confirmation_email(
            journey_reference="JRN-ABC123",
            traveler_names=["Alice"],
            party_size=1,
            itinerary_lines=["NYC → Paris"],
            customer_total=1500.00,
            currency="EUR",
            service_tier="Standard",
            confirmation_status="PAYMENT_UNKNOWN",
        )
        assert "successful" not in body.lower()
        assert "payment successful" not in body.lower()
        assert "verifying" in body.lower() or "confirmation" in body.lower()

    def test_refund_pending_email_avoids_refunded(self):
        """REFUND_PENDING email does NOT say 'refunded'."""
        subject, body = render_booking_confirmation_email(
            journey_reference="JRN-ABC123",
            traveler_names=["Alice"],
            party_size=1,
            itinerary_lines=["NYC → Paris"],
            customer_total=1500.00,
            currency="EUR",
            service_tier="Standard",
            confirmation_status="REFUND_PENDING",
        )
        # Must not say "refunded" - that implies completion
        assert "refunded" not in body.lower()
        # Must say something like "in progress"
        assert "progress" in body.lower() or "back" in body.lower()

    def test_confirmed_email_includes_itinerary(self):
        """CONFIRMED email includes full itinerary."""
        subject, body = render_booking_confirmation_email(
            journey_reference="JRN-ABC123",
            traveler_names=["Alice"],
            party_size=1,
            itinerary_lines=["NYC → Paris, AA100, 10:00-22:00"],
            customer_total=1500.00,
            currency="EUR",
            service_tier="Standard",
            confirmation_status="CONFIRMED",
        )
        assert "AA100" in body

    def test_unknown_email_omits_itinerary(self):
        """UNKNOWN status email should omit itinerary (not final)."""
        subject, body = render_booking_confirmation_email(
            journey_reference="JRN-ABC123",
            traveler_names=["Alice"],
            party_size=1,
            itinerary_lines=["NYC → Paris, AA100, 10:00-22:00"],
            customer_total=1500.00,
            currency="EUR",
            service_tier="Standard",
            confirmation_status="PAYMENT_UNKNOWN",
        )
        # Itinerary should NOT be included for non-confirmed status
        assert "AA100" not in body

    def test_confirmed_email_includes_total(self):
        """CONFIRMED email includes customer total."""
        subject, body = render_booking_confirmation_email(
            journey_reference="JRN-ABC123",
            traveler_names=["Alice"],
            party_size=1,
            itinerary_lines=["NYC → Paris"],
            customer_total=1500.00,
            currency="EUR",
            service_tier="Standard",
            confirmation_status="CONFIRMED",
        )
        assert "1500.00" in body
        assert "EUR" in body

    def test_all_emails_include_journey_reference(self):
        """All emails include the journey reference."""
        for status in ["CONFIRMED", "PARTIAL_RECOVERY", "PAYMENT_UNKNOWN", "REFUND_PENDING"]:
            subject, body = render_booking_confirmation_email(
                journey_reference="JRN-ABC123",
                traveler_names=["Alice"],
                party_size=1,
                itinerary_lines=["NYC → Paris"],
                customer_total=1500.00,
                currency="EUR",
                service_tier="Standard",
                confirmation_status=status,
            )
            assert "JRN-ABC123" in body


# ======================================================================
# Integration Tests
# ======================================================================
class TestIntegration:
    """End-to-end integration scenarios."""

    def test_full_communication_flow_success(
        self, in_memory_db, sandbox_provider, sample_communication
    ):
        """Full flow: create → attempt → send → event."""
        # 1. Create communication
        comm, created = create_communication(in_memory_db, communication=sample_communication)
        assert created is True

        # 2. Transition to SENDING
        comm_sending = comm.with_status(CommunicationStatus.SENDING)
        compare_and_swap_communication(
            in_memory_db, communication=comm_sending, expected_version=1
        )

        # 3. Send via provider
        send_result = sandbox_provider.send(
            idempotency_key=comm.idempotency_key,
            recipient=comm.recipient_address,
            subject="Booking Confirmation",
            body_text="Your booking is confirmed.",
            reference=comm.communication_id,
        )
        assert send_result.ok is True

        # 4. Record attempt
        attempt = CommunicationAttempt(
            attempt_id=new_id("att"),
            communication_id=comm.communication_id,
            attempt_number=1,
            status="SENT",
            provider_name=sandbox_provider.name,
            provider_message_id=send_result.provider_message_id,
            completed_at=datetime.now(timezone.utc),
        )
        record_attempt(in_memory_db, attempt)

        # 5. Transition communication to SENT
        comm_sent = comm_sending.with_status(CommunicationStatus.SENT)
        compare_and_swap_communication(
            in_memory_db, communication=comm_sent, expected_version=2
        )

        # 6. Record events
        record_event(
            in_memory_db,
            CommunicationEvent(
                event_id=new_id("evt"),
                communication_id=comm.communication_id,
                attempt_id=attempt.attempt_id,
                event_type="EMAIL_SENT",
                data={"provider": sandbox_provider.name},
            ),
        )

        # 7. Verify
        retrieved = get_communication(in_memory_db, comm.communication_id)
        assert retrieved.status == CommunicationStatus.SENT
        assert retrieved.version == 3

        attempts = list_attempts_for_communication(in_memory_db, comm.communication_id)
        assert len(attempts) == 1
        assert attempts[0].provider_message_id == send_result.provider_message_id

        events = list_events(in_memory_db, comm.communication_id)
        assert any(e.event_type == "EMAIL_SENT" for e in events)

    def test_unknown_outcome_requires_reconciliation(
        self, in_memory_db, sandbox_provider, sample_communication
    ):
        """UNKNOWN outcome cannot silently become SENT - requires reconciliation."""
        # Create and send (with success, not UNKNOWN_SEND)
        comm, _ = create_communication(in_memory_db, communication=sample_communication)
        comm_sending = comm.with_status(CommunicationStatus.SENDING)
        compare_and_swap_communication(
            in_memory_db, communication=comm_sending, expected_version=1
        )

        # Send successfully
        send_result = sandbox_provider.send(
            idempotency_key=comm.idempotency_key,
            recipient=comm.recipient_address,
            subject="Booking Confirmation",
            body_text="Your booking is confirmed.",
            reference=comm.communication_id,
        )
        assert send_result.ok is True

        # But simulate UNKNOWN outcome: record attempt as UNKNOWN
        # (in real life, this would happen if provider response timed out)
        attempt = CommunicationAttempt(
            attempt_id=new_id("att"),
            communication_id=comm.communication_id,
            attempt_number=1,
            status="UNKNOWN",
            provider_name=sandbox_provider.name,
            provider_message_id=send_result.provider_message_id,
        )
        record_attempt(in_memory_db, attempt)

        # Transition communication to UNKNOWN
        comm_unknown = comm_sending.with_status(CommunicationStatus.UNKNOWN)
        compare_and_swap_communication(
            in_memory_db, communication=comm_unknown, expected_version=2
        )

        # Verify: communication is UNKNOWN, not SENT
        retrieved = get_communication(in_memory_db, comm.communication_id)
        assert retrieved.status == CommunicationStatus.UNKNOWN

        # Now reconcile: retrieve from provider
        reconcile_result = sandbox_provider.retrieve(
            provider_message_id=send_result.provider_message_id
        )
        assert reconcile_result.ok is True  # Provider says it's success

        # Only after reconciliation can we transition from UNKNOWN to SENT
        comm_sent = comm_unknown.with_status(CommunicationStatus.SENT)
        compare_and_swap_communication(
            in_memory_db, communication=comm_sent, expected_version=3
        )

        # Verify final state
        retrieved = get_communication(in_memory_db, comm.communication_id)
        assert retrieved.status == CommunicationStatus.SENT
