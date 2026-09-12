"""The deterministic reference/sandbox email provider (V9 Phase 5).

Why this exists rather than only a real provider integration: this environment
has no live transactional email credentials, and inventing a placeholder
credential to "complete the integration" is exactly the kind of thing we forbid.
This adapter implements the real :class:`CommunicationProvider` contract
entirely in-process and deterministically, so the communication architecture
(state machine, idempotency, orchestration, recovery, reconciliation) can be
built and adversarially tested end-to-end without depending on network access
or a real provider account. It is not a live integration and must never be
mistaken for one - ``name`` is literally ``"sandbox"``, never ``"postmark"``.

**Deterministic failure injection**: the caller's ``reference`` (Detoura's
own communication_id, always passed through) may carry a magic suffix that
selects a scripted outcome - the same "test email address" idea real providers
use (Postmark: ``+bounce@example.com``), applied to a reference string instead.
See ``_SCRIPTED_SUFFIXES``. Anything without a recognised suffix behaves as an
unconditional success. This makes every failure-injection scenario in the test
suite deterministic and readable from the test itself, with no hidden shared
mutable configuration.

**Idempotency**: every mutating call is keyed by ``idempotency_key`` in an
in-process dict - a repeated call with the same key returns the exact same
:class:`CommunicationSendResult` without re-running the (possibly fault-injecting)
logic, mirroring how a real provider's idempotency-key support behaves.

**Thread safety**: uses ``threading.RLock``, not ``Lock`` - the idempotency
cache holds the lock across the whole ``compute()`` call, and ``compute()``
itself may re-acquire the lock to mutate internal state. A plain ``Lock``
would deadlock on the very first call (found via direct testing in Phase 4).
Reentrant so the same thread's nested acquisition succeeds; still mutually
exclusive against other threads.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .communication_provider import (
    CommunicationProviderCapabilities,
    CommunicationSendResult,
)

#: reference suffix -> what each operation should do the FIRST time it is
#: called for that reference. Subsequent calls (retries, duplicates) always
#: use the idempotency-key cache, never re-trigger the fault.
_SCRIPTED_SUFFIXES: dict[str, str] = {
    "_FAIL_SEND": "fail",
    "_UNKNOWN_SEND": "unknown",
}


def _scripted_outcome(reference: str) -> str:
    for suffix, outcome in _SCRIPTED_SUFFIXES.items():
        if reference.endswith(suffix):
            return outcome
    return "success"


@dataclass(slots=True)
class _Message:
    """Sandbox representation of a sent message."""

    provider_message_id: str
    reference: str
    recipient: str
    subject: str
    body_text: str
    status: str
    """success, failed, unknown"""


class SandboxEmailProvider:
    """In-process, deterministic transactional email provider. Thread-safe
    for a single process."""

    name = "sandbox"

    def __init__(self) -> None:
        # RLock, not Lock: idempotency cache holds the lock across the whole
        # `compute()` call, and `compute()` itself re-acquires the lock to
        # mutate a `_Message` - a plain Lock deadlocks on the very first call.
        # Reentrant so the same thread's nested acquisition succeeds; still
        # mutually exclusive against other threads.
        self._lock = threading.RLock()
        self._messages: dict[str, _Message] = {}
        self._by_idempotency_key: dict[str, CommunicationSendResult] = {}
        self._ref_seq = 0

    def capabilities(self) -> CommunicationProviderCapabilities:
        return CommunicationProviderCapabilities(
            supports_delivery_events=False,  # Sandbox doesn't simulate webhooks
            supports_idempotency_keys=True,
            max_retries_recommended=3,
        )

    # ------------------------------------------------------------------
    def _cached_or(self, idempotency_key: str, compute) -> CommunicationSendResult:
        """Idempotency-key cache: a repeated call with the same key returns
        the exact same result without re-running fault injection."""
        with self._lock:
            cached = self._by_idempotency_key.get(idempotency_key)
            if cached is not None:
                return cached
            result = compute()
            self._by_idempotency_key[idempotency_key] = result
            return result

    def _new_message_id(self) -> str:
        with self._lock:
            self._ref_seq += 1
            return f"sbx_msg_{self._ref_seq:08d}"

    # ------------------------------------------------------------------
    def send(
        self,
        *,
        idempotency_key: str,
        recipient: str,
        subject: str,
        body_text: str,
        reference: str,
    ) -> CommunicationSendResult:
        def compute() -> CommunicationSendResult:
            outcome = _scripted_outcome(reference)
            msg_id = self._new_message_id()

            if outcome == "fail":
                return CommunicationSendResult(
                    ok=False, unknown=False, provider_message_id=None,
                    status="rejected", detail="sandbox: send rejected (scripted)",
                )

            if outcome == "unknown":
                # A real timeout: no provider_message_id is returned to the
                # caller, but the sandbox itself still privately creates the
                # message, exactly like a real provider might have received
                # the request even though the response never arrived. This
                # is what reconciliation (`retrieve`) is for.
                with self._lock:
                    self._messages[msg_id] = _Message(
                        provider_message_id=msg_id, reference=reference,
                        recipient=recipient, subject=subject, body_text=body_text,
                        status="accepted",
                    )
                return CommunicationSendResult(
                    ok=False, unknown=True, provider_message_id=msg_id,
                    status="unknown", detail="sandbox: send timed out (scripted)",
                )

            # success
            with self._lock:
                self._messages[msg_id] = _Message(
                    provider_message_id=msg_id, reference=reference,
                    recipient=recipient, subject=subject, body_text=body_text,
                    status="success",
                )
            return CommunicationSendResult(
                ok=True, unknown=False, provider_message_id=msg_id,
                status="success", detail="sandbox: message accepted",
            )

        return self._cached_or(idempotency_key, compute)

    def retrieve(self, *, provider_message_id: str) -> CommunicationSendResult:
        """The only way UNKNOWN is ever resolved - reads current sandbox
        truth, no fault injection (a real provider's retrieve is truthful
        even when an earlier call to it timed out)."""
        with self._lock:
            msg = self._messages.get(provider_message_id)

        if msg is None:
            return CommunicationSendResult(
                ok=False, unknown=False, provider_message_id=provider_message_id,
                status="not_found", detail="sandbox: no such message",
            )

        return CommunicationSendResult(
            ok=(msg.status == "success"), unknown=False,
            provider_message_id=provider_message_id, status=msg.status,
            detail=f"sandbox: message {msg.status}",
        )

    @staticmethod
    def sign_webhook_payload(payload: bytes) -> str:
        """Test helper: produce a signature - not used in this phase,
        but provided for symmetry with payment provider."""
        import hashlib
        return hashlib.sha256(payload).hexdigest()
