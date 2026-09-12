"""V9 Phase 5 §A - journey confirmation: state machine, eligibility, persistence.

The invariant the whole file exists to defend: a traveller is told they hold
a journey ONLY when the booking really completed and the money really
settled. Every other outcome - partial booking, unresolved provider timeout,
unknown payment state - has its own visible status and must be structurally
unable to masquerade as success.
"""

from __future__ import annotations

import ast
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from detoura.models.confirmation import (
    ALLOWED_TRANSITIONS,
    FINALIZED_STATUSES,
    INDETERMINATE_PAYMENT_STATUSES,
    MONEY_COMMITTED_PAYMENT_STATUSES,
    PAID_PAYMENT_STATUSES,
    PHASE_AWAITING_CONFIRMATION,
    PHASE_AWAITING_TRAVELERS,
    PHASE_COMPLETE,
    PHASE_FAILED,
    PHASE_GUIDED_BOOKING,
    PHASE_ISSUING,
    PHASE_PARTIAL_FAILURE,
    PHASE_PRICE_INCONSISTENT,
    PHASE_RECONFIRM_REQUIRED,
    PHASE_REVALIDATING,
    PRE_COMMITMENT_PHASES,
    TERMINAL_STATUSES,
    UNPROVEN_PHASES,
    ConfirmationEvent,
    ConfirmationStatus,
    InvalidConfirmationTransition,
    JourneyConfirmation,
    NotConfirmable,
    can_transition_confirmation,
    evaluate_confirmation_eligibility,
    require_confirmation_status,
)
from detoura.persistence import confirmations as store
from detoura.persistence.confirmations import (
    StaleConfirmationVersion,
    compare_and_swap_confirmation,
    create_confirmation,
    get_confirmation,
    get_confirmation_for_booking,
    get_confirmation_for_user,
    list_confirmations_for_user,
    list_events,
    new_confirmation_event_id,
    new_confirmation_id,
    record_event,
)
from detoura.persistence.db import Database

#: The entry state of the machine - the only status with no incoming edge,
#: exactly as PaymentStatus.CREATED is for the payment machine.
ENTRY_STATUS = ConfirmationStatus.PENDING_VERIFICATION


@pytest.fixture()
def db() -> Database:
    """An in-memory database carrying ONLY the Phase 5 tables.

    Applied through the module's own ``apply_schema`` rather than by editing
    the shared ``persistence/db.py`` migration, which Agent 6 owns and other
    agents are editing concurrently. Once the central migration carries this
    DDL the call is a no-op (every statement is ``IF NOT EXISTS``).
    """
    database = Database(":memory:")
    store.apply_schema(database)
    yield database
    database.close()


def _confirmation(**kw) -> JourneyConfirmation:
    defaults = dict(
        confirmation_id="conf_x",
        booking_id="bk_x",
        journey_reference="DTR-TEST-1",
        status=ConfirmationStatus.CONFIRMED,
        service_tier="BASIC",
        booking_phase=PHASE_COMPLETE,
        payment_id="pay_x",
        payment_status="CAPTURED",
        party_size=2,
        lead_name="A Traveller",
    )
    defaults.update(kw)
    return JourneyConfirmation(**defaults)


# ======================================================================
# §1 State machine - exhaustive
# ======================================================================
def test_every_state_is_reachable_or_is_the_entry_state():
    reachable: set[ConfirmationStatus] = set()
    for targets in ALLOWED_TRANSITIONS.values():
        reachable |= targets
    unreachable = set(ConfirmationStatus) - reachable - {ENTRY_STATUS}
    assert not unreachable, f"states with no incoming transition: {unreachable}"


def test_every_state_has_a_transition_entry():
    """No state may be missing from the table - a missing key would silently
    read as "no transitions allowed" instead of being a declared decision."""
    assert set(ALLOWED_TRANSITIONS) == set(ConfirmationStatus)


def test_terminal_states_have_no_outgoing_transitions():
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_only_declared_terminal_states_are_dead_ends():
    for status, targets in ALLOWED_TRANSITIONS.items():
        if not targets:
            assert status in TERMINAL_STATUSES, (
                f"{status} is a dead end but is not declared terminal"
            )


def test_same_state_transition_is_always_allowed_idempotent():
    for status in ConfirmationStatus:
        assert can_transition_confirmation(status, status)


def test_no_state_declares_a_self_transition_in_the_table():
    """Self-transitions are handled by ``can_transition_confirmation``'s
    idempotency rule, not by table entries - two mechanisms for one thing is
    how they drift apart."""
    for status, targets in ALLOWED_TRANSITIONS.items():
        assert status not in targets


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES, key=lambda s: s.value))
@pytest.mark.parametrize("target", list(ConfirmationStatus))
def test_terminal_states_reject_every_other_target(terminal, target):
    if terminal == target:
        assert can_transition_confirmation(terminal, target)  # idempotent no-op
    else:
        assert not can_transition_confirmation(terminal, target)


def test_pending_verification_can_never_be_reached_again():
    """Once a confirmation has an outcome it never returns to "still
    checking" - that would let a CONFIRMED journey quietly become unproven."""
    for targets in ALLOWED_TRANSITIONS.values():
        assert ConfirmationStatus.PENDING_VERIFICATION not in targets


def test_confirmed_is_not_terminal_but_partial_recovery_is_reachable_from_it():
    assert ConfirmationStatus.CONFIRMED not in TERMINAL_STATUSES
    assert can_transition_confirmation(
        ConfirmationStatus.CONFIRMED, ConfirmationStatus.PARTIAL_RECOVERY,
    )


def test_partial_recovery_can_be_resolved_to_confirmed():
    assert can_transition_confirmation(
        ConfirmationStatus.PARTIAL_RECOVERY, ConfirmationStatus.CONFIRMED,
    )


def test_invalid_transition_raises_not_silently_accepted():
    cancelled = _confirmation(status=ConfirmationStatus.CANCELLED)
    with pytest.raises(InvalidConfirmationTransition):
        cancelled.with_status(ConfirmationStatus.CONFIRMED)


def test_superseded_is_terminal_and_rejects_revival():
    superseded = _confirmation(status=ConfirmationStatus.SUPERSEDED)
    for target in ConfirmationStatus:
        if target is ConfirmationStatus.SUPERSEDED:
            continue
        with pytest.raises(InvalidConfirmationTransition):
            superseded.with_status(target)


# ======================================================================
# §2 Cancellation and its terminal-ness
# ======================================================================
def test_cancellation_from_every_non_terminal_state_is_allowed():
    for status in ConfirmationStatus:
        if status in TERMINAL_STATUSES:
            continue
        assert can_transition_confirmation(status, ConfirmationStatus.CANCELLED)


def test_cancellation_stamps_finalized_at_and_bumps_version():
    now = datetime.now(timezone.utc)
    live = _confirmation(status=ConfirmationStatus.CONFIRMED, finalized_at=None)
    cancelled = live.with_status(ConfirmationStatus.CANCELLED, now=now)
    assert cancelled.status is ConfirmationStatus.CANCELLED
    assert cancelled.finalized_at == now
    assert cancelled.version == live.version + 1
    assert cancelled.is_terminal


def test_cancelled_is_terminal_from_every_direction():
    cancelled = _confirmation(status=ConfirmationStatus.CANCELLED)
    for target in ConfirmationStatus:
        if target is ConfirmationStatus.CANCELLED:
            continue
        with pytest.raises(InvalidConfirmationTransition):
            cancelled.with_status(target)
    # ...and a repeat cancellation is a legal no-op, not a crash.
    assert cancelled.with_status(ConfirmationStatus.CANCELLED).is_terminal


def test_finalized_at_is_stamped_once_and_never_overwritten():
    first = datetime.now(timezone.utc)
    later = first + timedelta(hours=3)
    confirmed = _confirmation(
        status=ConfirmationStatus.PENDING_VERIFICATION, finalized_at=None,
    ).with_status(ConfirmationStatus.CONFIRMED, now=first)
    assert confirmed.finalized_at == first
    cancelled = confirmed.with_status(ConfirmationStatus.CANCELLED, now=later)
    assert cancelled.finalized_at == first, "original settlement time was rewritten"


def test_partial_recovery_does_not_stamp_finalized_at():
    pending = _confirmation(
        status=ConfirmationStatus.PENDING_VERIFICATION, finalized_at=None,
    )
    recovery = pending.with_status(ConfirmationStatus.PARTIAL_RECOVERY)
    assert recovery.finalized_at is None
    assert ConfirmationStatus.PARTIAL_RECOVERY not in FINALIZED_STATUSES
    assert ConfirmationStatus.PENDING_VERIFICATION not in FINALIZED_STATUSES


# ======================================================================
# §3 Eligibility rules
# ======================================================================
def test_complete_plus_settled_payment_is_confirmed():
    for paid in sorted(PAID_PAYMENT_STATUSES):
        assert evaluate_confirmation_eligibility(
            booking_phase=PHASE_COMPLETE, payment_status=paid, has_payment=True,
        ) is ConfirmationStatus.CONFIRMED


def test_complete_with_no_payment_required_is_confirmed():
    assert evaluate_confirmation_eligibility(
        booking_phase=PHASE_COMPLETE, payment_status=None, has_payment=False,
    ) is ConfirmationStatus.CONFIRMED


def test_partial_failure_is_partial_recovery_never_confirmed():
    for paid in sorted(PAID_PAYMENT_STATUSES):
        assert evaluate_confirmation_eligibility(
            booking_phase=PHASE_PARTIAL_FAILURE, payment_status=paid,
        ) is ConfirmationStatus.PARTIAL_RECOVERY
    # even with no payment at all it is still not a success
    assert evaluate_confirmation_eligibility(
        booking_phase=PHASE_PARTIAL_FAILURE, has_payment=False,
    ) is ConfirmationStatus.PARTIAL_RECOVERY


def test_indeterminate_payment_never_yields_confirmed_for_any_phase():
    """The core §K guarantee, checked against EVERY phase this code knows
    about plus an unrecognised one - not just the happy-path phase."""
    phases = sorted(
        PRE_COMMITMENT_PHASES
        | UNPROVEN_PHASES
        | {PHASE_COMPLETE, PHASE_PARTIAL_FAILURE, PHASE_FAILED, "some_new_phase"}
    )
    for phase in phases:
        for status in sorted(INDETERMINATE_PAYMENT_STATUSES):
            result = evaluate_confirmation_eligibility(
                booking_phase=phase, payment_status=status, has_payment=True,
            )
            assert result is not ConfirmationStatus.CONFIRMED, (
                f"{phase} + {status} produced a success confirmation"
            )


def test_unknown_payment_on_a_complete_booking_is_pending_not_confirmed():
    assert evaluate_confirmation_eligibility(
        booking_phase=PHASE_COMPLETE, payment_status="UNKNOWN",
    ) is ConfirmationStatus.PENDING_VERIFICATION


def test_reconciliation_required_on_a_complete_booking_is_pending():
    assert evaluate_confirmation_eligibility(
        booking_phase=PHASE_COMPLETE, payment_status="RECONCILIATION_REQUIRED",
    ) is ConfirmationStatus.PENDING_VERIFICATION


def test_failed_before_any_commitment_is_not_eligible_at_all():
    """No confirmation record must EXIST for a booking that failed before
    anything was committed - not a CONFIRMED one, and not a pending one."""
    assert evaluate_confirmation_eligibility(
        booking_phase=PHASE_FAILED, payment_status=None, has_payment=False,
    ) is None
    assert evaluate_confirmation_eligibility(
        booking_phase=PHASE_FAILED, payment_status="FAILED", has_payment=True,
    ) is None
    assert evaluate_confirmation_eligibility(
        booking_phase=PHASE_FAILED, payment_status="CANCELLED", has_payment=True,
    ) is None


def test_failed_after_money_moved_is_partial_recovery_not_silence():
    """The distinction the "before any commitment" wording turns on: money
    that moved against a journey that did not must never vanish."""
    for status in sorted(MONEY_COMMITTED_PAYMENT_STATUSES):
        assert evaluate_confirmation_eligibility(
            booking_phase=PHASE_FAILED, payment_status=status, has_payment=True,
        ) is ConfirmationStatus.PARTIAL_RECOVERY


def test_require_confirmation_status_raises_the_documented_sentinel():
    with pytest.raises(NotConfirmable):
        require_confirmation_status(
            booking_phase=PHASE_FAILED, has_payment=False,
        )
    assert require_confirmation_status(
        booking_phase=PHASE_COMPLETE, payment_status="CAPTURED",
    ) is ConfirmationStatus.CONFIRMED


@pytest.mark.parametrize("phase", sorted(PRE_COMMITMENT_PHASES))
def test_pre_commitment_phases_produce_no_record(phase):
    assert evaluate_confirmation_eligibility(
        booking_phase=phase, has_payment=False,
    ) is None


@pytest.mark.parametrize("phase", sorted(PRE_COMMITMENT_PHASES))
def test_pre_commitment_phase_with_money_committed_is_recovery(phase):
    assert evaluate_confirmation_eligibility(
        booking_phase=phase, payment_status="CAPTURED", has_payment=True,
    ) is ConfirmationStatus.PARTIAL_RECOVERY


@pytest.mark.parametrize("phase", sorted(UNPROVEN_PHASES))
def test_unproven_phases_are_pending_even_with_a_settled_payment(phase):
    """Guided booking in particular: Detoura creates no order of its own, so
    a captured payment proves money moved, never that the tickets exist."""
    assert evaluate_confirmation_eligibility(
        booking_phase=phase, payment_status="CAPTURED", has_payment=True,
    ) is ConfirmationStatus.PENDING_VERIFICATION


def test_authorized_but_not_captured_is_not_confirmed():
    assert evaluate_confirmation_eligibility(
        booking_phase=PHASE_COMPLETE, payment_status="AUTHORIZED",
    ) is ConfirmationStatus.PENDING_VERIFICATION


def test_a_payment_claimed_with_no_status_cannot_confirm():
    """Contradictory input fails closed: ``has_payment=True`` with no status
    named cannot prove the money arrived."""
    assert evaluate_confirmation_eligibility(
        booking_phase=PHASE_COMPLETE, payment_status=None, has_payment=True,
    ) is ConfirmationStatus.PENDING_VERIFICATION


def test_a_named_status_overrides_has_payment_false():
    """The other contradiction: a real payment status supplied alongside
    ``has_payment=False`` must not be waved through as "nothing owed"."""
    assert evaluate_confirmation_eligibility(
        booking_phase=PHASE_COMPLETE, payment_status="AUTHORIZED",
        has_payment=False,
    ) is ConfirmationStatus.PENDING_VERIFICATION


def test_unrecognised_phase_fails_closed_to_pending():
    assert evaluate_confirmation_eligibility(
        booking_phase="a_phase_added_after_this_code_was_written",
        payment_status="CAPTURED",
    ) is ConfirmationStatus.PENDING_VERIFICATION


def test_eligibility_is_case_and_whitespace_insensitive():
    assert evaluate_confirmation_eligibility(
        booking_phase="  COMPLETE  ", payment_status=" captured ",
    ) is ConfirmationStatus.CONFIRMED


def test_only_complete_can_ever_produce_confirmed():
    """Sweep the whole cross-product: no phase other than ``complete``
    reaches CONFIRMED, under any payment status."""
    phases = sorted(
        PRE_COMMITMENT_PHASES
        | UNPROVEN_PHASES
        | {PHASE_PARTIAL_FAILURE, PHASE_FAILED, "mystery_phase"}
    )
    statuses = sorted(
        PAID_PAYMENT_STATUSES
        | INDETERMINATE_PAYMENT_STATUSES
        | MONEY_COMMITTED_PAYMENT_STATUSES
        | {"CREATED", "FAILED", "CANCELLED"}
    )
    for phase in phases:
        for has_payment in (True, False):
            for status in [*statuses, None]:
                result = evaluate_confirmation_eligibility(
                    booking_phase=phase, payment_status=status,
                    has_payment=has_payment,
                )
                assert result is not ConfirmationStatus.CONFIRMED, (
                    f"{phase} + {status} (has_payment={has_payment}) confirmed"
                )


# ======================================================================
# §4 Drift guards against the real booking/payment vocabularies
# ======================================================================
def test_phase_constants_match_the_real_booking_phase_enum():
    """models/confirmation.py mirrors BookingPhase as plain strings to avoid
    a model->service import. Mirroring is only safe if drift is loud."""
    from detoura.services.booking_orchestrator import BookingPhase

    expected = {
        BookingPhase.AWAITING_TRAVELERS: PHASE_AWAITING_TRAVELERS,
        BookingPhase.AWAITING_CONFIRMATION: PHASE_AWAITING_CONFIRMATION,
        BookingPhase.REVALIDATING: PHASE_REVALIDATING,
        BookingPhase.RECONFIRM_REQUIRED: PHASE_RECONFIRM_REQUIRED,
        BookingPhase.ISSUING: PHASE_ISSUING,
        BookingPhase.COMPLETE: PHASE_COMPLETE,
        BookingPhase.PARTIAL_FAILURE: PHASE_PARTIAL_FAILURE,
        BookingPhase.FAILED: PHASE_FAILED,
        BookingPhase.GUIDED_BOOKING: PHASE_GUIDED_BOOKING,
        BookingPhase.PRICE_INCONSISTENT: PHASE_PRICE_INCONSISTENT,
    }
    for member, mirrored in expected.items():
        assert member.value == mirrored, f"{member} drifted from {mirrored!r}"
    unclassified = set(BookingPhase) - set(expected)
    assert not unclassified, (
        f"new BookingPhase members not classified by the confirmation "
        f"eligibility rules: {unclassified}"
    )


def test_every_booking_phase_is_classified_by_the_eligibility_rules():
    from detoura.services.booking_orchestrator import BookingPhase

    classified = (
        PRE_COMMITMENT_PHASES
        | UNPROVEN_PHASES
        | {PHASE_COMPLETE, PHASE_PARTIAL_FAILURE, PHASE_FAILED}
    )
    for member in BookingPhase:
        assert member.value in classified, (
            f"{member} falls through to the catch-all instead of a rule"
        )


def test_payment_status_constants_are_real_payment_statuses():
    from detoura.models.payment import PaymentStatus

    known = {s.value for s in PaymentStatus}
    for name in (
        PAID_PAYMENT_STATUSES
        | INDETERMINATE_PAYMENT_STATUSES
        | MONEY_COMMITTED_PAYMENT_STATUSES
    ):
        assert name in known, f"{name!r} is not a PaymentStatus member"


def test_paid_set_agrees_with_the_payment_domains_settled_set():
    """``SETTLED_STATUSES`` is the payment domain's own "this went right"
    set. Confirmation's paid set must be a superset of it, and may only add
    PARTIALLY_REFUNDED (money was collected; a later partial refund does not
    unmake the booking)."""
    from detoura.models.payment import SETTLED_STATUSES

    settled = {s.value for s in SETTLED_STATUSES}
    assert settled <= PAID_PAYMENT_STATUSES
    assert PAID_PAYMENT_STATUSES - settled == {"PARTIALLY_REFUNDED"}


def test_indeterminate_statuses_are_never_in_the_paid_set():
    assert not (INDETERMINATE_PAYMENT_STATUSES & PAID_PAYMENT_STATUSES)


def test_money_committed_set_matches_the_payment_domains_risk_set():
    from detoura.models.payment import MONEY_AT_RISK_STATUSES

    assert {s.value for s in MONEY_AT_RISK_STATUSES} == MONEY_COMMITTED_PAYMENT_STATUSES


# ======================================================================
# §5 Model shape - no boolean shortcuts, frozen identity, no PII
# ======================================================================
def test_no_is_success_style_boolean_field_exists():
    forbidden = (
        "is_success", "success", "successful", "is_confirmed", "confirmed",
        "ok", "is_ok", "is_paid", "paid", "is_valid", "complete", "is_complete",
    )
    fields = set(JourneyConfirmation.model_fields)
    for name in forbidden:
        assert name not in fields, f"{name!r} is a boolean shortcut past `status`"


def test_no_boolean_field_at_all_on_the_confirmation():
    """Stronger than a name blocklist: any bool field would be a candidate
    shortcut, whatever it is called."""
    for name, field in JourneyConfirmation.model_fields.items():
        assert field.annotation is not bool, (
            f"{name!r} is a boolean field; status is the only source of truth"
        )


def test_status_is_the_only_outcome_field_and_is_an_enum():
    assert JourneyConfirmation.model_fields["status"].annotation is ConfirmationStatus


def test_identity_fields_are_frozen_after_creation():
    confirmation = _confirmation()
    for field in ("confirmation_id", "booking_id", "journey_reference"):
        with pytest.raises(Exception):
            setattr(confirmation, field, "mutated")


def test_status_cannot_be_mutated_directly_only_via_with_status():
    confirmation = _confirmation(status=ConfirmationStatus.PENDING_VERIFICATION)
    with pytest.raises(Exception):
        confirmation.status = ConfirmationStatus.CONFIRMED  # type: ignore[misc]


def test_journey_id_is_the_booking_id_not_a_separate_concept():
    confirmation = _confirmation(booking_id="bk_journey_1")
    assert confirmation.journey_id == "bk_journey_1"
    assert "journey_id" not in JourneyConfirmation.model_fields, (
        "journey_id must be a derived alias, not a second storable column "
        "that can disagree with booking_id"
    )


def test_travel_pass_reference_is_the_booking_id_not_a_new_id():
    assert not any(
        "pass" in name for name in JourneyConfirmation.model_fields
    ), "the travel pass is regenerated from booking_id; it has no id of its own"


def test_no_sensitive_traveller_pii_fields():
    forbidden = (
        "date_of_birth", "dob", "passport", "document_number", "phone",
        "address", "nationality", "email",
    )
    for name in JourneyConfirmation.model_fields:
        assert not any(f in name for f in forbidden), f"PII field {name!r}"


def test_confirmation_holds_no_money_amounts():
    """Amounts live in payment_transactions / booking_economics. A second
    copy here is a second number that can disagree with the first."""
    for name, field in JourneyConfirmation.model_fields.items():
        assert field.annotation is not float, f"{name!r} is an amount"
        assert not any(
            token in name for token in ("amount", "total", "price", "minor")
        ), f"{name!r} looks like a stored money value"


def test_confirmation_event_is_frozen_and_append_only_shaped():
    event = ConfirmationEvent(
        event_id="cev_1", confirmation_id="conf_x", event_type="CREATED",
    )
    with pytest.raises(Exception):
        event.event_type = "CHANGED"  # type: ignore[misc]
    assert event.data == {}


# ======================================================================
# §6 Static isolation from the optimizer
# ======================================================================
FORBIDDEN_IMPORT_TOKENS = (
    "market_prior", "opportunity", "beam_search", "candidate_funnel",
)


def _imported_modules(module) -> set[str]:
    tree = ast.parse(open(module.__file__).read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return imported


def test_confirmation_domain_never_imports_optimizer_internals():
    """Static proof, not docstring prose: what a traveller is told they hold
    must derive from booking and payment truth only, never from an estimate
    built to rank candidates (§B/§X.3)."""
    import detoura.models.confirmation as module

    for name in _imported_modules(module):
        assert not any(token in name for token in FORBIDDEN_IMPORT_TOKENS), (
            f"confirmation domain must not import {name!r}"
        )


def test_confirmation_persistence_never_imports_optimizer_internals():
    for name in _imported_modules(store):
        assert not any(token in name for token in FORBIDDEN_IMPORT_TOKENS), (
            f"confirmation persistence must not import {name!r}"
        )


def test_confirmation_persistence_is_decoupled_from_phase4_persistence():
    """Phase 5 defines its own exceptions rather than importing Phase 4's -
    the two domains must be separable."""
    for name in _imported_modules(store):
        assert "persistence.payments" not in name
        assert not name.endswith("payments")


def test_confirmation_model_does_not_import_service_layer():
    """A model importing a service would invert the layering; the phase
    vocabulary is mirrored as strings and drift-guarded by tests instead."""
    import detoura.models.confirmation as module

    for name in _imported_modules(module):
        assert "services" not in name, f"model imports service module {name!r}"


# ======================================================================
# §7 Persistence - idempotency, concurrency, CAS, ownership
# ======================================================================
def test_create_then_read_back_roundtrips_every_field(db):
    now = datetime.now(timezone.utc)
    original = _confirmation(
        confirmation_id=new_confirmation_id(), booking_id="bk_round",
        user_id="user_1", finalized_at=now,
    )
    stored, created = create_confirmation(db, confirmation=original)
    assert created is True
    fetched = get_confirmation(db, original.confirmation_id)
    assert fetched is not None
    assert fetched.model_dump() == original.model_dump()
    assert stored.model_dump() == original.model_dump()


def test_duplicate_finalization_returns_the_existing_row(db):
    first = _confirmation(confirmation_id="conf_a", booking_id="bk_dupe")
    stored_first, created_first = create_confirmation(db, confirmation=first)
    assert created_first is True

    # A replayed finalizer run: same booking, a freshly minted confirmation id
    # and even a different (wrong) status - the stored row must win.
    second = _confirmation(
        confirmation_id="conf_b", booking_id="bk_dupe",
        status=ConfirmationStatus.PARTIAL_RECOVERY,
    )
    stored_second, created_second = create_confirmation(db, confirmation=second)
    assert created_second is False
    assert stored_second.confirmation_id == stored_first.confirmation_id
    assert stored_second.status is ConfirmationStatus.CONFIRMED
    rows = db.query("SELECT COUNT(*) AS n FROM journey_confirmations")
    assert rows[0]["n"] == 1


def test_unique_constraint_is_on_booking_id(db):
    create_confirmation(db, confirmation=_confirmation(
        confirmation_id="conf_1", booking_id="bk_unique",
    ))
    with pytest.raises(sqlite3.IntegrityError):
        with db.write() as conn:
            conn.execute(
                "INSERT INTO journey_confirmations ("
                " confirmation_id, booking_id, journey_reference, status,"
                " service_tier, booking_phase, party_size, lead_name,"
                " created_at, version"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("conf_2", "bk_unique", "ref", "CONFIRMED", "BASIC",
                 PHASE_COMPLETE, 1, "", datetime.now(timezone.utc).isoformat(), 1),
            )


def test_concurrent_creation_produces_exactly_one_row(db):
    """No lock, no pre-read: the UNIQUE constraint is the whole mechanism.
    Every racing thread must come back with the same winning row."""
    booking_id = "bk_race"
    barrier = threading.Barrier(8)
    results: list[tuple[str, bool]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        candidate = _confirmation(
            confirmation_id=f"conf_race_{index}", booking_id=booking_id,
        )
        try:
            barrier.wait(timeout=10)
            stored, created = create_confirmation(db, confirmation=candidate)
        except BaseException as exc:  # noqa: BLE001 - recorded and re-asserted
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append((stored.confirmation_id, created))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"racing creates crashed: {errors}"
    assert len(results) == 8
    assert sum(1 for _, created in results if created) == 1, "more than one winner"
    winner_ids = {cid for cid, _ in results}
    assert len(winner_ids) == 1, f"threads disagree on the winning row: {winner_ids}"
    rows = db.query(
        "SELECT COUNT(*) AS n FROM journey_confirmations WHERE booking_id=?",
        (booking_id,),
    )
    assert rows[0]["n"] == 1


def test_compare_and_swap_advances_the_stored_row(db):
    original = _confirmation(
        confirmation_id="conf_cas", booking_id="bk_cas",
        status=ConfirmationStatus.PENDING_VERIFICATION, finalized_at=None,
    )
    create_confirmation(db, confirmation=original)
    advanced = original.with_status(ConfirmationStatus.CONFIRMED)
    compare_and_swap_confirmation(
        db, confirmation=advanced, expected_version=original.version,
    )
    reloaded = get_confirmation(db, "conf_cas")
    assert reloaded.status is ConfirmationStatus.CONFIRMED
    assert reloaded.version == original.version + 1
    assert reloaded.finalized_at is not None


def test_compare_and_swap_rejects_a_stale_writer(db):
    original = _confirmation(
        confirmation_id="conf_stale", booking_id="bk_stale",
        status=ConfirmationStatus.PENDING_VERIFICATION, finalized_at=None,
    )
    create_confirmation(db, confirmation=original)
    winner = original.with_status(ConfirmationStatus.CONFIRMED)
    compare_and_swap_confirmation(
        db, confirmation=winner, expected_version=original.version,
    )
    # A second writer that read the same original row and decided otherwise.
    loser = original.with_status(ConfirmationStatus.CANCELLED)
    with pytest.raises(StaleConfirmationVersion):
        compare_and_swap_confirmation(
            db, confirmation=loser, expected_version=original.version,
        )
    assert get_confirmation(db, "conf_stale").status is ConfirmationStatus.CONFIRMED


def test_compare_and_swap_never_rewrites_identity_or_snapshot_columns(db):
    original = _confirmation(
        confirmation_id="conf_snap", booking_id="bk_snap",
        status=ConfirmationStatus.PENDING_VERIFICATION,
        booking_phase=PHASE_ISSUING, payment_status="AUTHORIZED",
        finalized_at=None,
    )
    create_confirmation(db, confirmation=original)
    # A caller that tampers with the evidence fields must not be able to
    # persist them - the snapshot records why the original decision was made.
    tampered = original.model_copy(update={
        "booking_phase": PHASE_COMPLETE, "payment_status": "CAPTURED",
        "journey_reference": "DTR-TAMPERED",
    }).with_status(ConfirmationStatus.CONFIRMED)
    compare_and_swap_confirmation(
        db, confirmation=tampered, expected_version=original.version,
    )
    reloaded = get_confirmation(db, "conf_snap")
    assert reloaded.booking_phase == PHASE_ISSUING
    assert reloaded.payment_status == "AUTHORIZED"
    assert reloaded.journey_reference == "DTR-TEST-1"
    assert reloaded.status is ConfirmationStatus.CONFIRMED


def test_get_confirmation_for_booking(db):
    create_confirmation(db, confirmation=_confirmation(
        confirmation_id="conf_bk", booking_id="bk_lookup",
    ))
    assert get_confirmation_for_booking(db, "bk_lookup").confirmation_id == "conf_bk"
    assert get_confirmation_for_booking(db, "bk_nope") is None


def test_ownership_checked_read_is_idor_safe(db):
    create_confirmation(db, confirmation=_confirmation(
        confirmation_id="conf_owned", booking_id="bk_owned", user_id="owner",
    ))
    assert get_confirmation_for_user(db, "conf_owned", user_id="owner") is not None
    # Someone else's id and a non-existent id are indistinguishable.
    assert get_confirmation_for_user(db, "conf_owned", user_id="intruder") is None
    assert get_confirmation_for_user(db, "conf_missing", user_id="intruder") is None


def test_anonymous_confirmations_are_not_owned_by_the_empty_user(db):
    create_confirmation(db, confirmation=_confirmation(
        confirmation_id="conf_anon", booking_id="bk_anon", user_id=None,
    ))
    assert get_confirmation_for_user(db, "conf_anon", user_id="") is None
    assert list_confirmations_for_user(db, user_id="") == []


def test_list_confirmations_for_user_only_returns_their_own(db):
    create_confirmation(db, confirmation=_confirmation(
        confirmation_id="c1", booking_id="b1", user_id="alice",
    ))
    create_confirmation(db, confirmation=_confirmation(
        confirmation_id="c2", booking_id="b2", user_id="bob",
    ))
    mine = list_confirmations_for_user(db, user_id="alice")
    assert [c.confirmation_id for c in mine] == ["c1"]


# ======================================================================
# §8 Event ledger
# ======================================================================
def test_events_are_ordered_by_insertion_not_by_random_event_id(db):
    """Same-timestamp events must come back in insertion order. Sorting the
    tie by a random url-safe token (the Phase 4 bug) makes the ledger's
    displayed order not chronological."""
    same_moment = datetime.now(timezone.utc)
    types = ["CREATED", "ELIGIBILITY_EVALUATED", "FINALIZED", "NOTIFIED"]
    for event_type in types:
        record_event(db, ConfirmationEvent(
            event_id=new_confirmation_event_id(), confirmation_id="conf_led",
            event_type=event_type, occurred_at=same_moment,
        ))
    assert [e.event_type for e in list_events(db, "conf_led")] == types


def test_events_respect_occurred_at_across_distinct_timestamps(db):
    base = datetime.now(timezone.utc)
    for offset, event_type in ((2, "LATER"), (0, "EARLIER"), (1, "MIDDLE")):
        record_event(db, ConfirmationEvent(
            event_id=new_confirmation_event_id(), confirmation_id="conf_ts",
            event_type=event_type, occurred_at=base + timedelta(seconds=offset),
        ))
    assert [e.event_type for e in list_events(db, "conf_ts")] == [
        "EARLIER", "MIDDLE", "LATER",
    ]


def test_events_are_scoped_to_one_confirmation(db):
    for confirmation_id in ("conf_p", "conf_q"):
        record_event(db, ConfirmationEvent(
            event_id=new_confirmation_event_id(),
            confirmation_id=confirmation_id, event_type="CREATED",
        ))
    assert len(list_events(db, "conf_p")) == 1
    assert list_events(db, "conf_absent") == []


def test_event_can_join_an_open_transaction_with_the_status_change(db):
    """A status change and the ledger row that explains it commit together or
    not at all - otherwise the ledger and the row can disagree."""
    original = _confirmation(
        confirmation_id="conf_tx", booking_id="bk_tx",
        status=ConfirmationStatus.PENDING_VERIFICATION, finalized_at=None,
    )
    create_confirmation(db, confirmation=original)
    advanced = original.with_status(ConfirmationStatus.CONFIRMED)
    with pytest.raises(RuntimeError):
        with db.write() as conn:
            conn.execute(
                "UPDATE journey_confirmations SET status=?, version=?"
                " WHERE confirmation_id=? AND version=?",
                (advanced.status.value, advanced.version, "conf_tx",
                 original.version),
            )
            record_event(db, ConfirmationEvent(
                event_id="cev_tx", confirmation_id="conf_tx",
                event_type="FINALIZED",
            ), conn=conn)
            raise RuntimeError("simulated crash before commit")
    # Both halves rolled back together.
    assert get_confirmation(db, "conf_tx").status is ConfirmationStatus.PENDING_VERIFICATION
    assert list_events(db, "conf_tx") == []


def test_event_payload_roundtrips_as_structured_data(db):
    record_event(db, ConfirmationEvent(
        event_id="cev_data", confirmation_id="conf_data",
        event_type="ELIGIBILITY_EVALUATED", detail="complete + captured",
        data={"booking_phase": PHASE_COMPLETE, "payment_status": "CAPTURED"},
    ))
    (event,) = list_events(db, "conf_data")
    assert event.data == {
        "booking_phase": PHASE_COMPLETE, "payment_status": "CAPTURED",
    }
    assert event.detail == "complete + captured"


# ======================================================================
# §9 Schema handoff to the central migration
# ======================================================================
def test_ddl_comment_block_matches_the_executable_ddl():
    """The commented block at the top of persistence/confirmations.py is what
    Agent 6 copies into db.py's central migration. If it drifts from the DDL
    this module actually executes, the integrated schema silently differs
    from the one every test above was written against."""
    source = open(store.__file__).read()
    start = source.index("# --- Phase 5 schema addition")
    end = source.index("# --- end Phase 5 schema addition ---")
    block_lines = source[start:end].splitlines()[1:]  # drop the header line
    uncommented = "\n".join(
        line[2:] if line.startswith("# ") else line.lstrip("#")
        for line in block_lines
    )

    def statements(text: str) -> list[str]:
        # Strip `--` comment lines BEFORE splitting on ";" - a semicolon
        # inside a prose comment would otherwise cut a statement in half and
        # leave the comment's tail masquerading as SQL.
        code = "\n".join(
            line for line in text.splitlines()
            if line.strip() and not line.strip().startswith("--")
        )
        return [
            " ".join(chunk.split()) for chunk in code.split(";") if chunk.strip()
        ]

    assert statements(uncommented) == statements(store.CONFIRMATION_DDL)


def test_apply_schema_is_idempotent(db):
    """Once Agent 6 folds the DDL into db.py, apply_schema must be a harmless
    no-op rather than a second, conflicting CREATE."""
    store.apply_schema(db)
    store.apply_schema(db)
    create_confirmation(db, confirmation=_confirmation(
        confirmation_id="conf_idem", booking_id="bk_idem",
    ))
    assert get_confirmation(db, "conf_idem") is not None


def test_schema_has_no_money_columns(db):
    names = {
        row["name"]
        for row in db.query("PRAGMA table_info(journey_confirmations)")
    }
    assert names, "journey_confirmations was not created"
    assert not any(
        token in name
        for name in names
        for token in ("minor", "amount", "total", "price")
    ), f"confirmation schema stores money: {names}"


def test_schema_columns_match_the_model_fields(db):
    """Every persisted field has a column and vice versa - a field added to
    the model without a column is data silently dropped on write."""
    columns = {
        row["name"]
        for row in db.query("PRAGMA table_info(journey_confirmations)")
    }
    assert columns == set(JourneyConfirmation.model_fields)


# ======================================================================
# §10 End-to-end: eligibility -> record -> finalization
# ======================================================================
def test_finalizer_shaped_flow_confirmed(db):
    status = evaluate_confirmation_eligibility(
        booking_phase=PHASE_COMPLETE, payment_status="CAPTURED", has_payment=True,
    )
    assert status is ConfirmationStatus.CONFIRMED
    confirmation, created = create_confirmation(db, confirmation=JourneyConfirmation(
        confirmation_id=new_confirmation_id(), booking_id="bk_e2e",
        journey_reference="DTR-E2E", user_id="alice", status=status,
        service_tier="SIGNATURE", booking_phase=PHASE_COMPLETE,
        payment_id="pay_e2e", payment_status="CAPTURED", party_size=2,
        lead_name="A Traveller",
    ))
    assert created is True
    assert confirmation.journey_id == "bk_e2e"
    reloaded = get_confirmation_for_user(
        db, confirmation.confirmation_id, user_id="alice",
    )
    assert reloaded.status is ConfirmationStatus.CONFIRMED


def test_finalizer_shaped_flow_failed_booking_creates_nothing(db):
    status = evaluate_confirmation_eligibility(
        booking_phase=PHASE_FAILED, payment_status=None, has_payment=False,
    )
    assert status is None
    # The finalizer must branch on None and write nothing at all.
    assert get_confirmation_for_booking(db, "bk_never") is None
    rows = db.query("SELECT COUNT(*) AS n FROM journey_confirmations")
    assert rows[0]["n"] == 0


# ======================================================================
# V9 Phase 5 QA finding #2 regression: RECOVERY_REQUIRED != CONFIRMED
# even when only `recovery_state` (not `phase`) reflects the problem.
# ======================================================================
def test_recovery_state_flag_overrides_an_otherwise_confirmable_booking():
    """A booking that looks perfectly healthy by phase/payment alone
    (complete + captured) must never read CONFIRMED once an explicit
    Ops/ticket-operations recovery flag is set - a cancellation attempt
    that itself failed leaves `phase` untouched but sets `recovery_state`."""
    status = evaluate_confirmation_eligibility(
        booking_phase=PHASE_COMPLETE, payment_status="CAPTURED", has_payment=True,
        recovery_state="CANCELLATION_FAILED",
    )
    assert status is ConfirmationStatus.PARTIAL_RECOVERY
    assert status is not ConfirmationStatus.CONFIRMED


def test_recovery_state_empty_string_is_healthy_not_a_flag():
    status = evaluate_confirmation_eligibility(
        booking_phase=PHASE_COMPLETE, payment_status="CAPTURED", has_payment=True,
        recovery_state="",
    )
    assert status is ConfirmationStatus.CONFIRMED


def test_recovery_state_flag_checked_before_confirmed_for_every_named_recovery_value():
    """Every value in persistence.bookings.RECOVERY_STATES (except the
    healthy "") must prevent CONFIRMED on an otherwise-eligible booking."""
    from detoura.persistence.bookings import RECOVERY_STATES

    for value in RECOVERY_STATES:
        status = evaluate_confirmation_eligibility(
            booking_phase=PHASE_COMPLETE, payment_status="CAPTURED", has_payment=True,
            recovery_state=value,
        )
        assert status is not ConfirmationStatus.CONFIRMED, f"recovery_state={value!r} leaked CONFIRMED"


def test_require_confirmation_status_also_honors_recovery_state():
    status = require_confirmation_status(
        booking_phase=PHASE_COMPLETE, payment_status="CAPTURED", has_payment=True,
        recovery_state="PRICE_CHANGED",
    )
    assert status is ConfirmationStatus.PARTIAL_RECOVERY
