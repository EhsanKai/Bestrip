"""Synthetic baggage policies, including the ones that admit ignorance (V7).

The default is deliberately **unknown on every fare**. Two reasons, both
important.

Truth: no provider currently wired into this repository reports baggage, so a
fixture that confidently declares "personal item included" would be inventing
exactly the fact this phase exists to stop inventing.

Coverage: if the fixtures always knew the answer, the unknown path - the one
that carries all the risk - would ship untested. A synthetic dataset that
cannot express ignorance guarantees the honest branch is the least exercised
code in the system.

So this module is opt-in. `SyntheticTransportDataProvider(baggage=...)` selects
a scenario; the default is `None`, and every existing golden signature is
therefore untouched.
"""

from __future__ import annotations

from enum import Enum

from ..models.baggage import (
    BaggageAllowance,
    BaggageKind,
    BaggagePolicy,
    BaggageStatus,
)


class BaggageScenario(str, Enum):
    """Named fixtures covering the cases the brief calls out."""

    UNKNOWN = "unknown"
    """Case C: the provider said nothing at all. The honest default."""
    PERSONAL_ONLY = "personal_only"
    """Case A: personal item included, cabin EUR 20, checked EUR 45."""
    CABIN_INCLUDED = "cabin_included"
    """Case B/D: cabin bag free, checked bag unquoted."""
    CABIN_UNAVAILABLE = "cabin_unavailable"
    """Case E: a fare that will not carry a cabin bag at any price."""
    PER_LEG_VARYING = "per_leg_varying"
    """Cases F/G/H: the policy differs by leg, carrier and direction."""


def _personal_only() -> BaggagePolicy:
    return BaggagePolicy(
        personal_item=BaggageAllowance.included(BaggageKind.PERSONAL_ITEM),
        cabin_bag=BaggageAllowance.extra(BaggageKind.CABIN_BAG, 20.0),
        checked_bag=BaggageAllowance.extra(BaggageKind.CHECKED_BAG, 45.0),
    )


def _cabin_included() -> BaggagePolicy:
    return BaggagePolicy(
        personal_item=BaggageAllowance.included(BaggageKind.PERSONAL_ITEM),
        cabin_bag=BaggageAllowance.included(BaggageKind.CABIN_BAG),
        # Known to cost something, but the fee was never quoted. A real and
        # common shape, and the one most likely to be mistaken for free.
        checked_bag=BaggageAllowance.extra_unpriced(BaggageKind.CHECKED_BAG),
    )


def _cabin_unavailable() -> BaggagePolicy:
    return BaggagePolicy(
        personal_item=BaggageAllowance.included(BaggageKind.PERSONAL_ITEM),
        cabin_bag=BaggageAllowance(
            kind=BaggageKind.CABIN_BAG, status=BaggageStatus.NOT_AVAILABLE
        ),
        checked_bag=BaggageAllowance.extra(BaggageKind.CHECKED_BAG, 45.0),
    )


#: Deterministic per-leg variation, keyed on a stable hash of the leg id.
#:
#: Rotating through four genuinely different shapes - priced, free, unquoted,
#: differently priced - is what makes a multi-leg trip exercise mixed carriers,
#: differing directions and partial knowledge in one search, which is where
#: per-leg arithmetic actually breaks.
_ROTATION = (
    lambda: _personal_only(),
    lambda: _cabin_included(),
    lambda: BaggagePolicy.all_unknown(),
    lambda: BaggagePolicy(
        personal_item=BaggageAllowance.included(BaggageKind.PERSONAL_ITEM),
        cabin_bag=BaggageAllowance.extra(BaggageKind.CABIN_BAG, 35.0),
        checked_bag=BaggageAllowance.unknown(BaggageKind.CHECKED_BAG),
    ),
)


def policy_for(scenario: BaggageScenario | None, leg_id: str) -> BaggagePolicy | None:
    """The policy a synthetic fare should carry.

    ``None`` scenario returns ``None``, meaning the provider said nothing -
    which is what every pre-Phase-3 fixture implicitly was, and what keeps the
    golden signatures exact.
    """
    if scenario is None:
        return None
    if scenario is BaggageScenario.UNKNOWN:
        return BaggagePolicy.all_unknown()
    if scenario is BaggageScenario.PERSONAL_ONLY:
        return _personal_only()
    if scenario is BaggageScenario.CABIN_INCLUDED:
        return _cabin_included()
    if scenario is BaggageScenario.CABIN_UNAVAILABLE:
        return _cabin_unavailable()
    # Deterministic in the leg id, so repeated searches agree and a trip's
    # baggage does not change when results are re-sorted.
    return _ROTATION[sum(ord(c) for c in leg_id) % len(_ROTATION)]()
