"""The deterministic reference/sandbox payment provider (V9 Phase 4 §V).

Why this exists rather than only a real Stripe adapter: this environment has
no live Stripe test credentials, and inventing a placeholder secret to
"complete the integration" is exactly the kind of thing §V forbids. This
adapter implements the real :class:`PaymentProvider` contract - the same one
:mod:`detoura.providers.stripe_payment` implements against Stripe's actual
API - entirely in-process and deterministically, so the payment
architecture (state machine, idempotency, orchestration, recovery,
reconciliation) can be built and adversarially tested end-to-end without
depending on network access or a real provider account. It is not a live
integration and must never be mistaken for one - ``name`` is literally
``"sandbox"``, never ``"stripe"``.

**Deterministic failure injection**: the caller's ``reference`` (Detoura's
own payment_id, always passed through) may carry a magic suffix that
selects a scripted outcome - the same "test card number" idea real
providers use (Stripe: ``4000000000000002`` always declines), applied to a
reference string instead. See ``_SCRIPTED_SUFFIXES``. Anything without a
recognised suffix behaves as an unconditional success. This makes every
failure-injection scenario in the test suite deterministic and readable
from the test itself, with no hidden shared mutable configuration.

**Idempotency**: every mutating call is keyed by ``idempotency_key`` in an
in-process dict - a repeated call with the same key returns the exact same
:class:`ProviderResult` without re-running the (possibly fault-injecting)
logic, mirroring how a real provider's idempotency-key support behaves.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .payment_provider import (
    ProviderCapabilities,
    ProviderEvent,
    ProviderEventVerificationError,
    ProviderResult,
)

#: reference suffix -> what each operation should do the FIRST time it is
#: called for that reference. Subsequent calls (retries, duplicates) always
#: use the idempotency-key cache, never re-trigger the fault.
_SCRIPTED_SUFFIXES: dict[str, dict[str, str]] = {
    "_FAIL_AUTH": {"authorize": "fail"},
    "_UNKNOWN_AUTH": {"authorize": "unknown"},
    "_REQUIRES_ACTION": {"authorize": "requires_action"},
    "_FAIL_CAPTURE": {"capture": "fail"},
    "_UNKNOWN_CAPTURE": {"capture": "unknown"},
    "_FAIL_REFUND": {"refund": "fail"},
    "_UNKNOWN_REFUND": {"refund": "unknown"},
    "_FAIL_CANCEL": {"cancel_authorization": "fail"},
}


def _scripted_outcome(reference: str, operation: str) -> str:
    for suffix, ops in _SCRIPTED_SUFFIXES.items():
        if reference.endswith(suffix) and operation in ops:
            return ops[operation]
    return "success"


@dataclass(slots=True)
class _Account:
    provider_reference: str
    currency: str
    detoura_reference: str = ""
    """The original ``reference`` (Detoura payment_id) this authorization was
    opened for - kept so a later capture/cancel/refund's fault injection can
    still key off the *Detoura* reference's magic suffix, since only
    ``authorize`` receives it directly."""
    authorized_amount: float = 0.0
    captured_amount: float = 0.0
    refunded_amount: float = 0.0
    status: str = "created"
    cancelled: bool = False


class SandboxPaymentProvider:
    """In-process, deterministic. Thread-safe for a single process."""

    name = "sandbox"

    def __init__(self) -> None:
        # RLock, not Lock: `_cached_or` holds the lock across the whole
        # `compute()` call, and `compute()` itself re-acquires the lock to
        # mutate an `_Account` - a plain Lock deadlocks on the very first
        # call. Reentrant so the same thread's nested acquisition succeeds;
        # still mutually exclusive against other threads.
        self._lock = threading.RLock()
        self._accounts: dict[str, _Account] = {}
        self._by_call_key: dict[str, ProviderResult] = {}
        self._ref_seq = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_manual_capture=True,
            # V9 Payment <-> Booking Coupling: this was False while nothing
            # actually exercised it - a latent inconsistency, since
            # capture() below always honors an explicit partial `amount`
            # regardless of this flag (flagged in the Beta Contract audit,
            # Part 21). The price-decrease capture fix now genuinely
            # depends on partial capture working against this provider, so
            # the declared capability must be honest.
            supports_partial_capture=True,
            supports_partial_refund=True,
            supports_customer_action=True,
            supports_webhooks=True,
            supports_idempotency_keys=True,
            unsupported_payment_methods=("bnpl", "crypto"),
        )

    # ------------------------------------------------------------------
    def _cached_or(self, idempotency_key: str, compute) -> ProviderResult:
        with self._lock:
            cached = self._by_call_key.get(idempotency_key)
            if cached is not None:
                return cached
            result = compute()
            self._by_call_key[idempotency_key] = result
            return result

    def _new_reference(self) -> str:
        self._ref_seq += 1
        return f"sbx_ref_{self._ref_seq:08d}"

    # ------------------------------------------------------------------
    def authorize(
        self, *, idempotency_key: str, amount: float, currency: str, reference: str,
        payment_method: str | None = None,
    ) -> ProviderResult:
        def compute() -> ProviderResult:
            outcome = _scripted_outcome(reference, "authorize")
            provider_ref = self._new_reference()
            if outcome == "fail":
                return ProviderResult(
                    ok=False, unknown=False, provider_reference=None,
                    status="failed", detail="sandbox: authorization declined (scripted)",
                )
            if outcome == "unknown":
                # A real timeout: no provider_reference is returned to the
                # caller, but the sandbox itself still privately opens the
                # account, exactly like a real provider might have received
                # the request even though the response never arrived. This
                # is what reconciliation (`retrieve`) is for.
                with self._lock:
                    self._accounts[provider_ref] = _Account(
                        provider_reference=provider_ref, currency=currency,
                        detoura_reference=reference,
                        authorized_amount=amount, status="authorized",
                    )
                return ProviderResult(
                    ok=False, unknown=True, provider_reference=provider_ref,
                    status="unknown", detail="sandbox: authorize timed out (scripted)",
                )
            if outcome == "requires_action":
                with self._lock:
                    self._accounts[provider_ref] = _Account(
                        provider_reference=provider_ref, currency=currency,
                        detoura_reference=reference, status="requires_action",
                    )
                return ProviderResult(
                    ok=False, unknown=False, provider_reference=provider_ref,
                    status="requires_action", detail="sandbox: customer action required (scripted)",
                )
            with self._lock:
                self._accounts[provider_ref] = _Account(
                    provider_reference=provider_ref, currency=currency,
                    detoura_reference=reference,
                    authorized_amount=amount, status="authorized",
                )
            return ProviderResult(
                ok=True, unknown=False, provider_reference=provider_ref, status="authorized",
                authorized_amount=amount,
            )

        return self._cached_or(idempotency_key, compute)

    def capture(
        self, *, idempotency_key: str, provider_reference: str, amount: float | None = None,
    ) -> ProviderResult:
        def compute() -> ProviderResult:
            with self._lock:
                acct = self._accounts.get(provider_reference)
            if acct is None:
                return ProviderResult(
                    ok=False, unknown=False, provider_reference=provider_reference,
                    status="not_found", detail="sandbox: no such authorization",
                )
            # Fault injection keys off the *Detoura* reference (the magic
            # suffix on the payment_id passed to `authorize`), remembered on
            # the account - `provider_reference` is a sandbox-generated id
            # the caller never puts a suffix on.
            outcome = _scripted_outcome(acct.detoura_reference, "capture")
            if outcome == "fail":
                return ProviderResult(
                    ok=False, unknown=False, provider_reference=provider_reference,
                    status="failed", detail="sandbox: capture declined (scripted)",
                )
            if outcome == "unknown":
                return ProviderResult(
                    ok=False, unknown=True, provider_reference=provider_reference,
                    status="unknown", detail="sandbox: capture timed out (scripted)",
                )
            amt = amount if amount is not None else acct.authorized_amount
            with self._lock:
                acct.captured_amount = amt
                acct.status = "captured"
            return ProviderResult(
                ok=True, unknown=False, provider_reference=provider_reference,
                status="captured", captured_amount=amt,
            )

        return self._cached_or(idempotency_key, compute)

    def cancel_authorization(
        self, *, idempotency_key: str, provider_reference: str,
    ) -> ProviderResult:
        def compute() -> ProviderResult:
            with self._lock:
                acct = self._accounts.get(provider_reference)
            if acct is None:
                return ProviderResult(
                    ok=False, unknown=False, provider_reference=provider_reference,
                    status="not_found", detail="sandbox: no such authorization",
                )
            if acct.status == "captured":
                # Can't cancel an authorization that is already captured -
                # that needs a refund instead. Fail closed rather than
                # silently no-op.
                return ProviderResult(
                    ok=False, unknown=False, provider_reference=provider_reference,
                    status="already_captured",
                    detail="sandbox: cannot cancel - already captured, use refund",
                )
            if _scripted_outcome(acct.detoura_reference, "cancel_authorization") == "fail":
                return ProviderResult(
                    ok=False, unknown=False, provider_reference=provider_reference,
                    status="failed", detail="sandbox: cancel declined (scripted)",
                )
            with self._lock:
                acct.cancelled = True
                acct.status = "cancelled"
            return ProviderResult(
                ok=True, unknown=False, provider_reference=provider_reference, status="cancelled",
            )

        return self._cached_or(idempotency_key, compute)

    def refund(
        self, *, idempotency_key: str, provider_reference: str, amount: float, reason: str = "",
    ) -> ProviderResult:
        def compute() -> ProviderResult:
            with self._lock:
                acct = self._accounts.get(provider_reference)
            if acct is None:
                return ProviderResult(
                    ok=False, unknown=False, provider_reference=provider_reference,
                    status="not_found", detail="sandbox: no such payment",
                )
            outcome = _scripted_outcome(acct.detoura_reference, "refund")
            if outcome == "fail":
                return ProviderResult(
                    ok=False, unknown=False, provider_reference=provider_reference,
                    status="failed", detail="sandbox: refund declined (scripted)",
                )
            if outcome == "unknown":
                return ProviderResult(
                    ok=False, unknown=True, provider_reference=provider_reference,
                    status="unknown", detail="sandbox: refund timed out (scripted)",
                )
            if acct.refunded_amount + amount - acct.captured_amount > 0.005:
                return ProviderResult(
                    ok=False, unknown=False, provider_reference=provider_reference,
                    status="failed", detail="sandbox: refund exceeds captured amount",
                )
            with self._lock:
                acct.refunded_amount = round(acct.refunded_amount + amount, 2)
                acct.status = (
                    "refunded" if abs(acct.refunded_amount - acct.captured_amount) < 0.005
                    else "partially_refunded"
                )
            return ProviderResult(
                ok=True, unknown=False, provider_reference=provider_reference,
                status=acct.status, refunded_amount=acct.refunded_amount,
            )

        return self._cached_or(idempotency_key, compute)

    def retrieve(self, *, provider_reference: str) -> ProviderResult:
        """The only way UNKNOWN is ever resolved - reads current sandbox
        truth, no fault injection (a real provider's retrieve is truthful
        even when an earlier call to it timed out)."""
        with self._lock:
            acct = self._accounts.get(provider_reference)
        if acct is None:
            return ProviderResult(
                ok=False, unknown=False, provider_reference=provider_reference,
                status="not_found", detail="sandbox: no such payment",
            )
        return ProviderResult(
            ok=True, unknown=False, provider_reference=provider_reference, status=acct.status,
            authorized_amount=acct.authorized_amount, captured_amount=acct.captured_amount,
            refunded_amount=acct.refunded_amount,
        )

    def verify_event(self, *, payload: bytes, signature: str) -> ProviderEvent:
        """A trivial deterministic "signature": ``sha256(payload) == signature``.
        Enough to exercise reject-unsigned/reject-invalid/dedup logic without
        depending on a real provider's signing scheme."""
        import hashlib
        import json

        expected = hashlib.sha256(payload).hexdigest()
        if signature != expected:
            raise ProviderEventVerificationError("sandbox: signature does not match payload")
        try:
            body = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ProviderEventVerificationError(f"sandbox: unparseable payload ({error})") from error
        event_id = body.get("id")
        event_type = body.get("type")
        if not event_id or not event_type:
            raise ProviderEventVerificationError("sandbox: event missing id/type")
        return ProviderEvent(
            provider_event_id=str(event_id), event_type=str(event_type),
            provider_reference=body.get("provider_reference"),
            status=str(body.get("status", "")), payload=body,
        )

    @staticmethod
    def sign(payload: bytes) -> str:
        """Test helper: produce a signature this adapter's ``verify_event``
        accepts, mirroring how a test would obtain a real provider's
        signature from its own test tooling."""
        import hashlib

        return hashlib.sha256(payload).hexdigest()
