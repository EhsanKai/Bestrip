"""V7.5 booking foundation: the domain, before anything can be bought.

Nothing here books. The tests exist because the hard part of multi-ticket
booking is not the happy path - it is the third ticket failing after two have
already been paid for, and a system that calls that CONFIRMED has told somebody
they have a holiday they cannot take.

So most of what follows tries to reach CONFIRMED dishonestly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from detoura.models.booking import (
    ALLOWED_TRANSITIONS,
    BookingItem,
    BookingState,
    InvalidTransition,
    JourneyBookingIntent,
    PriceTolerance,
    RevalidationResult,
    can_transition,
)
from detoura.models.provider_reference import ProviderOfferReference

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def an_item(item_id: str, *, state=BookingState.DRAFT, required=True,
            price=100.0, expires_in_minutes=60) -> BookingItem:
    return BookingItem(
        item_id=item_id,
        provider_ref=ProviderOfferReference(
            provider="duffel", offer_id=f"off_{item_id}",
            expires_at=NOW + timedelta(minutes=expires_in_minutes),
        ),
        origin="CGN", destination="BCN",
        departure=NOW + timedelta(days=7), arrival=NOW + timedelta(days=7, hours=2),
        travelers=2, quoted_price=price, state=state, required=required,
    )


def a_journey(items, *, state=BookingState.DRAFT, total=400.0) -> JourneyBookingIntent:
    return JourneyBookingIntent(
        journey_id="jrn_1", items=tuple(items), quoted_total=total, travelers=2,
        state=state,
    )


def _settle(item: BookingItem) -> BookingItem:
    """Walk an item legitimately through to CONFIRMED."""
    for target in (BookingState.REVALIDATING, BookingState.READY,
                   BookingState.USER_CONFIRMED, BookingState.BOOKING,
                   BookingState.CONFIRMED):
        item = item.with_state(target)
    return item


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------
def test_a_journey_with_one_unbooked_flight_cannot_be_confirmed():
    """The single worst thing this system could tell somebody."""
    journey = a_journey(
        [_settle(an_item("a")), _settle(an_item("b")), an_item("c")],
        state=BookingState.BOOKING,
    )
    assert journey.can_confirm is False
    with pytest.raises(InvalidTransition, match="not all booked"):
        journey.with_state(BookingState.CONFIRMED)


def test_a_journey_with_every_flight_booked_can_be_confirmed():
    journey = a_journey(
        [_settle(an_item("a")), _settle(an_item("b"))], state=BookingState.BOOKING
    )
    assert journey.can_confirm is True
    assert journey.with_state(BookingState.CONFIRMED).state is BookingState.CONFIRMED


def test_there_is_no_way_to_force_a_confirmation():
    """An override would exist only to do the forbidden thing."""
    import inspect

    signature = inspect.signature(JourneyBookingIntent.with_state)
    assert set(signature.parameters) == {"self", "target"}


def test_an_empty_journey_cannot_be_confirmed():
    journey = a_journey([], state=BookingState.BOOKING)
    assert journey.can_confirm is False
    assert journey.outcome is BookingState.FAILED


def test_partial_failure_is_derived_from_the_tickets_not_asserted():
    """A journey-level flag could drift from its own items. This cannot."""
    journey = a_journey(
        [_settle(an_item("a")), an_item("b", state=BookingState.DRAFT)],
        state=BookingState.BOOKING,
    )
    assert journey.outcome is BookingState.PARTIAL_FAILURE


def test_nothing_booked_at_all_is_failed_not_partial():
    journey = a_journey([an_item("a"), an_item("b")], state=BookingState.BOOKING)
    assert journey.outcome is BookingState.FAILED


def test_partial_failure_can_only_go_to_recovery_never_to_confirmed():
    assert can_transition(BookingState.PARTIAL_FAILURE, BookingState.RECOVERY_REQUIRED)
    assert not can_transition(BookingState.PARTIAL_FAILURE, BookingState.CONFIRMED)
    assert not can_transition(BookingState.RECOVERY_REQUIRED, BookingState.CONFIRMED)


def test_no_state_at_all_can_jump_straight_to_confirmed_except_booking():
    """Enumerated, not spot-checked: CONFIRMED has exactly one predecessor."""
    predecessors = {
        state for state, targets in ALLOWED_TRANSITIONS.items()
        if BookingState.CONFIRMED in targets
    }
    assert predecessors == {BookingState.BOOKING}


def test_confirmed_and_failed_are_terminal():
    assert ALLOWED_TRANSITIONS[BookingState.CONFIRMED] == frozenset()
    assert ALLOWED_TRANSITIONS[BookingState.FAILED] == frozenset()


def test_every_state_has_a_transition_rule():
    """A state missing from the table would silently allow nothing - or, worse,
    be read as allowing anything by a future edit."""
    for state in BookingState:
        assert state in ALLOWED_TRANSITIONS


# ---------------------------------------------------------------------------
# Item-level transitions
# ---------------------------------------------------------------------------
def test_an_item_cannot_skip_straight_to_confirmed():
    with pytest.raises(InvalidTransition):
        an_item("a").with_state(BookingState.CONFIRMED)


def test_a_price_change_sends_the_journey_back_rather_than_forward():
    """The traveller agreed to a number that is no longer the number."""
    assert can_transition(BookingState.PRICE_CHANGED, BookingState.REVALIDATING)
    assert not can_transition(BookingState.PRICE_CHANGED, BookingState.CONFIRMED)


def test_unattempted_items_are_distinguishable_from_failed_ones():
    """Nothing was sent for these, so nothing needs undoing."""
    journey = a_journey([
        _settle(an_item("a")),
        an_item("b", state=BookingState.DRAFT).with_state(BookingState.REVALIDATING)
                                              .with_state(BookingState.PROVIDER_FAILURE),
        an_item("c", state=BookingState.NOT_ATTEMPTED),
    ])
    assert [i.item_id for i in journey.settled_items()] == ["a"]
    assert [i.item_id for i in journey.failed_items()] == ["b"]
    assert "c" in [i.item_id for i in journey.unattempted_items()]


def test_optional_items_do_not_block_a_journey():
    journey = a_journey(
        [_settle(an_item("flight")), an_item("bag", required=False)],
        state=BookingState.BOOKING,
    )
    assert journey.can_confirm is True


def test_duplicate_item_ids_are_rejected():
    with pytest.raises(ValueError, match="distinct ids"):
        a_journey([an_item("a"), an_item("a")])


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------
def test_a_journey_expires_with_its_shortest_lived_ticket():
    journey = a_journey([
        an_item("a", expires_in_minutes=120),
        an_item("b", expires_in_minutes=15),
        an_item("c", expires_in_minutes=90),
    ])
    assert journey.expires_at == NOW + timedelta(minutes=15)


def test_a_journey_of_undated_offers_has_no_expiry():
    item = BookingItem(
        item_id="a",
        provider_ref=ProviderOfferReference(provider="duffel", offer_id="off"),
        origin="CGN", destination="BCN",
        departure=NOW, arrival=NOW + timedelta(hours=2),
        travelers=1, quoted_price=50.0,
    )
    assert a_journey([item]).expires_at is None


# ---------------------------------------------------------------------------
# Price tolerance
# ---------------------------------------------------------------------------
def test_a_small_rise_within_tolerance_proceeds():
    assert PriceTolerance(absolute=15.0).accepts(500.0, 508.0) is True


def test_a_rise_beyond_tolerance_needs_a_new_confirmation():
    assert PriceTolerance(absolute=15.0).accepts(500.0, 529.0) is False


def test_a_price_drop_never_needs_re_consent():
    """Nobody needs asking again to be charged less."""
    assert PriceTolerance(absolute=0.0).accepts(500.0, 400.0) is True


def test_percentage_and_absolute_tolerance_take_whichever_is_larger():
    tolerance = PriceTolerance(absolute=10.0, percentage=5.0)
    assert tolerance.accepts(1000.0, 1045.0) is True    # 4.5% < 5%
    assert tolerance.accepts(1000.0, 1060.0) is False   # 6% and > 10 absolute
    assert tolerance.accepts(100.0, 109.0) is True      # 9 < 10 absolute


def test_zero_tolerance_accepts_only_an_unchanged_or_lower_price():
    tolerance = PriceTolerance()
    assert tolerance.accepts(500.0, 500.0) is True
    assert tolerance.accepts(500.0, 500.01) is False


# ---------------------------------------------------------------------------
# Revalidation
# ---------------------------------------------------------------------------
def test_revalidation_reports_both_absolute_and_relative_movement():
    result = RevalidationResult(original_total=500.0, revalidated_total=530.0)
    assert result.absolute_delta == 30.0
    assert result.percentage_delta == pytest.approx(6.0)


def test_an_unavailable_item_makes_a_journey_unbookable_whatever_the_price():
    result = RevalidationResult(
        original_total=500.0, revalidated_total=500.0, items_unavailable=("b",)
    )
    assert result.is_bookable is False


def test_an_expired_item_makes_a_journey_unbookable():
    result = RevalidationResult(
        original_total=500.0, revalidated_total=500.0, items_expired=("c",)
    )
    assert result.is_bookable is False


def test_an_unchanged_revalidation_is_bookable():
    result = RevalidationResult(original_total=500.0, revalidated_total=500.0)
    assert result.is_bookable is True
    assert result.absolute_delta == 0.0


# ---------------------------------------------------------------------------
# Nothing here can book
# ---------------------------------------------------------------------------
def test_the_booking_domain_cannot_reach_a_provider():
    """V7.5 builds the shape; V8 executes it. No network, no payment, no order."""
    import ast
    import inspect

    from detoura.models import booking

    source = inspect.getsource(booking)
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    joined = " ".join(imported).lower()
    for forbidden in ("http", "urllib", "requests", "duffel", "socket", "payment"):
        assert forbidden not in joined
