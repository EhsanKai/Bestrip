"""Communication send/resend orchestration (V9 Phase 5 - Agent 6 integration).

Agent 3 built the communication domain model, persistence layer and the
sandbox provider; this module is the missing piece that actually ties them
together and talks to a provider - the exact role
:mod:`detoura.services.payment_service` plays for the payment domain (a
provider call is never made directly from an API route or from
persistence - it goes through here, so every send is idempotent,
auditable, and UNKNOWN is never silently converted to SENT/FAILED).

**BOOKING SUCCESS != EMAIL SUCCESS** (global Phase 5 principle): nothing
here ever touches a booking or payment row. A failed or ambiguous send is
recorded fully and truthfully on the communication domain alone.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from ..models.communication import (
    CommunicationAttempt,
    CommunicationChannel,
    CommunicationEvent,
    CommunicationStatus,
    CommunicationType,
    CustomerCommunication,
)
from ..persistence import communications as store
from ..persistence.db import Database
from ..providers.communication_provider import CommunicationProvider, CommunicationSendResult
from ..communication_config import resolve_communication_provider


class NoSuchCommunication(Exception):
    """Resend/retry was requested for a booking with no existing
    communication of the given type - there is nothing to resend."""


class CommunicationAlreadyInFlight(Exception):
    """A send/resend was requested while a previous attempt on the same
    communication is still SENDING - refuse rather than risk a genuine
    duplicate provider call from two overlapping requests."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_id() -> str:
    return store.new_id("cevt")


def _attempt_provider_key(communication_id: str, attempt_number: int) -> str:
    """Deterministic per-(communication, attempt) provider idempotency key
    - the exact same reasoning as Phase 4's payment idempotency keys: a
    retried call for the SAME attempt always presents the SAME key, so a
    provider-level dedup never lets one attempt execute twice."""
    return hashlib.sha256(f"{communication_id}:{attempt_number}".encode()).hexdigest()[:40]


def create_and_send_communication(
    db: Database,
    *,
    booking_id: str,
    journey_reference: str,
    user_id: str | None,
    recipient_address: str,
    subject: str,
    body_text: str,
    idempotency_key: str,
    communication_type: CommunicationType = CommunicationType.BOOKING_CONFIRMATION,
    channel: CommunicationChannel = CommunicationChannel.EMAIL,
    provider: CommunicationProvider | None = None,
    now: datetime | None = None,
) -> CustomerCommunication:
    """Create the one logical communication for (booking_id, type) and send
    its first attempt. Idempotent: a duplicate call (same idempotency_key,
    or simply a retry for the same booking+type) never creates a second
    logical communication and never sends a second time for an
    already-SENT/already-SENDING communication - it returns the existing
    state."""
    now = now or _now()
    provider = provider or resolve_communication_provider()

    candidate = CustomerCommunication(
        communication_id=store.new_id("comm"),
        booking_id=booking_id,
        journey_reference=journey_reference,
        user_id=user_id,
        channel=channel,
        communication_type=communication_type,
        status=CommunicationStatus.PENDING,
        recipient_address=recipient_address,
        idempotency_key=idempotency_key,
        created_at=now,
        updated_at=now,
    )
    communication, created = store.create_communication(db, communication=candidate)
    if not created:
        # Already exists (a prior call, or a concurrent one that won the
        # race) - never send a second logical communication. If it never
        # got past PENDING (e.g. the process died between create and
        # send), let the caller explicitly retry via request_resend rather
        # than silently sending here on an unrelated read/create path.
        store.record_event(db, CommunicationEvent(
            event_id=_event_id(), communication_id=communication.communication_id,
            event_type="COMMUNICATION_CREATED", occurred_at=now,
            detail="idempotent replay - communication already existed",
        ))
        return communication

    store.record_event(db, CommunicationEvent(
        event_id=_event_id(), communication_id=communication.communication_id,
        event_type="COMMUNICATION_CREATED", occurred_at=now,
    ))
    return _send_attempt(
        db, communication=communication, provider=provider,
        subject=subject, body_text=body_text, now=now,
    )


def request_resend(
    db: Database,
    *,
    booking_id: str,
    communication_type: CommunicationType | str = CommunicationType.BOOKING_CONFIRMATION,
    subject: str | None = None,
    body_text: str | None = None,
    provider: CommunicationProvider | None = None,
    now: datetime | None = None,
) -> CustomerCommunication:
    """An explicit, auditable new attempt on the SAME logical communication
    - never a new :class:`CustomerCommunication` row (§ global idempotency).

    ``subject``/``body_text`` default to re-rendering nothing new here (the
    caller - the finalizer, which has the booking/traveler context - is
    expected to pass the same content again for a true resend; if omitted,
    the most recent attempt's absence of content is acceptable since the
    provider call only needs *a* body, and callers that only want to
    trigger operational recovery, e.g. Ops, may not have the itinerary
    context at hand). A resend is refused outright while a previous
    attempt is still SENDING - never blindly retried on top of an
    in-flight send (§ communication truth)."""
    now = now or _now()
    if isinstance(communication_type, str):
        communication_type = CommunicationType(communication_type)
    provider = provider or resolve_communication_provider()

    communication = store.get_communication_for_booking(db, booking_id, communication_type.value)
    if communication is None:
        raise NoSuchCommunication(
            f"no {communication_type.value} communication exists yet for booking {booking_id}"
        )
    if communication.status == CommunicationStatus.SENDING:
        raise CommunicationAlreadyInFlight(
            f"{communication.communication_id}: a send is already in flight"
        )

    store.record_event(db, CommunicationEvent(
        event_id=_event_id(), communication_id=communication.communication_id,
        event_type="EMAIL_RESEND_REQUESTED", occurred_at=now,
    ))
    return _send_attempt(
        db, communication=communication, provider=provider,
        subject=subject or "", body_text=body_text or "", now=now,
    )


def _send_attempt(
    db: Database, *, communication: CustomerCommunication, provider: CommunicationProvider,
    subject: str, body_text: str, now: datetime,
) -> CustomerCommunication:
    existing_attempts = store.list_attempts_for_communication(db, communication.communication_id)
    attempt_number = len(existing_attempts) + 1

    pending = communication
    if pending.status in (CommunicationStatus.PENDING,):
        pending = pending.with_status(CommunicationStatus.SENDING, now=now)
        pending = store.compare_and_swap_communication(
            db, communication=pending, expected_version=communication.version,
        )
    elif pending.status in (CommunicationStatus.SENT, CommunicationStatus.FAILED):
        # A resend from a terminal state - move through SENDING again
        # (same-state is never re-entered; a resend is a fresh attempt).
        pending = pending.with_status(CommunicationStatus.SENDING, now=now) \
            if CommunicationStatus.SENDING in _reachable(pending.status) else pending
        if pending.status != CommunicationStatus.SENDING:
            # SENT/FAILED have no outgoing transitions in the domain model
            # (deliberately terminal) - a resend after a terminal outcome
            # is still a legitimate new attempt at the ATTEMPT level, it
            # just cannot move the already-terminal COMMUNICATION status
            # backwards. Record the attempt without forcing an illegal
            # communication-level transition.
            pending = communication
    attempt = CommunicationAttempt(
        attempt_id=store.new_id("attempt"), communication_id=communication.communication_id,
        attempt_number=attempt_number, status=CommunicationStatus.SENDING.value,
        provider_name=provider.name, created_at=now,
    )
    store.record_attempt(db, attempt)
    store.record_event(db, CommunicationEvent(
        event_id=_event_id(), communication_id=communication.communication_id,
        attempt_id=attempt.attempt_id, event_type="EMAIL_SEND_REQUESTED", occurred_at=now,
    ))

    idem = _attempt_provider_key(communication.communication_id, attempt_number)
    result = provider.send(
        idempotency_key=idem, recipient=pending.recipient_address, subject=subject,
        body_text=body_text, reference=communication.communication_id,
    )
    return _apply_send_result(db, communication=pending, attempt=attempt, result=result, now=now)


def _reachable(status: CommunicationStatus) -> frozenset:
    from ..models.communication import ALLOWED_TRANSITIONS

    return ALLOWED_TRANSITIONS.get(status, frozenset())


def _apply_send_result(
    db: Database, *, communication: CustomerCommunication, attempt: CommunicationAttempt,
    result: CommunicationSendResult, now: datetime,
) -> CustomerCommunication:
    if result.unknown:
        target, event_type, detail = (
            CommunicationStatus.UNKNOWN, "EMAIL_OUTCOME_UNKNOWN",
            "send outcome unknown (timeout) - never silently retried",
        )
    elif result.ok:
        target, event_type, detail = CommunicationStatus.SENT, "EMAIL_SENT", result.detail
    else:
        target, event_type, detail = CommunicationStatus.FAILED, "EMAIL_FAILED", result.detail

    store.update_attempt_completion(
        db, attempt_id=attempt.attempt_id, completed_at=now, status=target.value,
        provider_message_id=result.provider_message_id,
    )
    stored = communication
    if communication.status != target and target in _reachable(communication.status) | {communication.status}:
        try:
            updated = communication.with_status(target, now=now)
            stored = store.compare_and_swap_communication(
                db, communication=updated, expected_version=communication.version,
            )
        except Exception:
            # The communication-level status transition failed to apply
            # (e.g. a race, or an already-terminal communication receiving
            # a resend's outcome) - the ATTEMPT's own truthful outcome is
            # still recorded above regardless; a communication-level
            # status mismatch here is a reconciliation/Ops-visibility
            # concern, never a reason to lose the attempt's audit trail.
            stored = communication
    store.record_event(db, CommunicationEvent(
        event_id=_event_id(), communication_id=communication.communication_id,
        attempt_id=attempt.attempt_id, event_type=event_type, occurred_at=now, detail=detail,
    ))
    return stored


def reconcile_communication(
    db: Database, *, communication: CustomerCommunication, provider: CommunicationProvider,
    now: datetime | None = None,
) -> CustomerCommunication:
    """The only path that resolves an UNKNOWN outcome - always by asking
    the provider for real truth via its own ``retrieve()``, mirroring
    Phase 4's payment reconciliation discipline exactly."""
    now = now or _now()
    if communication.status != CommunicationStatus.UNKNOWN:
        return communication
    attempts = store.list_attempts_for_communication(db, communication.communication_id)
    latest = next((a for a in reversed(attempts) if a.provider_message_id), None)
    if latest is None or latest.provider_message_id is None:
        return communication  # nothing to reconcile against yet

    result = provider.retrieve(provider_message_id=latest.provider_message_id)
    if result.unknown:
        return communication  # still genuinely unresolved
    if result.status in ("failed", "rejected"):
        target = CommunicationStatus.FAILED
    elif result.ok or result.status in ("success", "accepted"):
        # "accepted" is the sandbox's own truthful terminology for "the
        # provider actually has this message" (see sandbox_email.py's
        # UNKNOWN branch) - distinct from `ok=True`/"success" but still a
        # real, resolvable outcome from Detoura's perspective: the send
        # DID happen, so it is truthfully SENT, never left dangling.
        target = CommunicationStatus.SENT
    else:
        return communication  # some other ambiguous provider status - stays UNKNOWN, human/Ops decides
    updated = communication.with_status(target, now=now)
    stored = store.compare_and_swap_communication(
        db, communication=updated, expected_version=communication.version,
    )
    store.update_attempt_completion(
        db, attempt_id=latest.attempt_id, completed_at=now, status=target.value,
    )
    store.record_event(db, CommunicationEvent(
        event_id=_event_id(), communication_id=communication.communication_id,
        event_type="EMAIL_SENT" if target is CommunicationStatus.SENT else "EMAIL_FAILED", occurred_at=now,
        detail=f"reconciled: {result.detail}",
    ))
    return stored
