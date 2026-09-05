"""Real-time personalization from explicit feedback actions (V6).

:mod:`detoura.learning` already turns a click stream into weights: it fits
nine numbers offline, in a batch, from the pairwise "this beat those" signal
every planning run produces. That module is deliberately not touched here.

This module answers a different question, on a different timescale. A
traveler saves a trip, likes one, dismisses another - one action at a time,
during a single session, before there is ever enough data for a Bradley-Terry
fit to mean anything. The two are complementary layers, not competitors:

* ``learning.py`` is the *slow* layer - batch, offline, statistically
  grounded, meant to retune the shipped profiles themselves over many
  travelers' choices.
* this module is the *fast* layer - a small, deterministic, session-scoped
  heuristic that nudges one traveler's own recommendations in real time,
  reacting to a single action as soon as it happens.

**Declared vs. observed.** A session carries two profiles, and they are never
allowed to collapse into one:

* ``declared`` is what the traveler *said* - the profile their search request
  named, or the default. It changes only when they explicitly change their
  search, never as a side effect of a click.
* ``observed`` is what their actions *imply* - a :class:`TravelValueWeights`
  nudged, action by action, towards (or away from) the components of the
  trips they reacted to.

Something downstream that wants a single number back would need to blend the
two explicitly (see :func:`blend`); nothing here does that blending silently,
because a silent blend is indistinguishable from overwriting the traveler's
own stated preference with a guess.

**The math.** Every nudge reuses the multiplicative, simplex-preserving update
:mod:`detoura.learning` already uses to keep a weight vector valid: it is the
same reason it is right there - it keeps every weight non-negative and the
vector normalized *by construction*, with no projection or clipping step. The
per-action step is small (about five percent of the remaining distance to the
trip's own profile, not an absolute five points on every weight), and total
drift from the starting point is capped per component so a burst of
one-sided, repeated, or contradictory clicks cannot run the profile away to a
corner of the simplex.

**Storage: intentionally in-memory, intentionally single-process.** Session
state lives in a module-level dict keyed by ``session_id``. This is a
deliberate simplification for V6, not an oversight:

* there is no account system in this repo yet, so a "session" is only ever a
  client-generated identifier with no identity behind it;
* production-grade storage would mean a persistence layer keyed on that
  future account, with eviction, multi-process sharing and probably a proper
  cache - none of which V6 needs to prove the heuristic works;
* until that lands, personalization does not survive a server restart or a
  second worker process, and callers should treat it as best-effort, not as
  a record of anything that must be kept.

The migration path when accounts exist: replace :data:`_SESSIONS` with a
lookup against persistent per-account storage, keep the update math exactly
as it is (it does not know or care where the profile it is nudging came
from), and let a session's in-memory state seed the very first write.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..profiles import COMPONENTS, ProfileName, TravelValueWeights, get_profile

#: Fraction of the remaining distance to a trip's profile that one signal
#: closes. Small on purpose: :mod:`learning.py`'s DEFAULT_LEARNING_RATE (0.35)
#: is tuned for dozens of pairwise observations pulled toward a fit; this is
#: tuned for a single click, which should move the needle, not decide it.
STEP_FRACTION = 0.05

#: How strongly each action type counts, as a multiple of :data:`STEP_FRACTION`.
#: ``booked`` is the strongest, unambiguous signal a traveler can send; a
#: ``rejected`` is a comparably strong negative one. ``saved``/``liked`` and
#: ``disliked`` are weaker because they are cheaper to click and easier to
#: reverse.
ACTION_STRENGTH: dict[str, float] = {
    "saved": 1.0,
    "liked": 1.0,
    "booked": 1.5,
    "disliked": 1.0,
    "rejected": 1.5,
}

#: Actions that pull ``observed`` towards the trip's own profile.
POSITIVE_ACTIONS = frozenset({"saved", "liked", "booked"})

#: Actions that push ``observed`` away from the trip's profile.
NEGATIVE_ACTIONS = frozenset({"disliked", "rejected"})

#: Cap on cumulative drift per component, as an absolute difference from the
#: starting (prior) weight after renormalization. This is what stops a burst
#: of repeated or contradictory signals from running one component to a
#: corner of the simplex - see the module docstring's "the math" section.
MAX_DRIFT_PER_COMPONENT = 0.15

#: Same floor :mod:`learning.py` uses: a component at exactly zero could never
#: recover under a multiplicative update.
MIN_WEIGHT = 1e-4


class FeedbackAction(str, Enum):
    """The explicit signals a traveler can send about one trip."""

    SAVED = "saved"
    LIKED = "liked"
    DISLIKED = "disliked"
    BOOKED = "booked"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class SessionProfile:
    """The state of one session's personalization.

    ``declared`` and ``observed`` are kept as separate, independently
    addressable fields on purpose - see the module docstring.
    """

    session_id: str
    declared_profile: ProfileName
    declared: TravelValueWeights
    observed: TravelValueWeights
    baseline: TravelValueWeights
    """What ``observed`` started from - the anchor cumulative drift is
    measured against. Either the profile the first trip was found under, or
    BEST_VALUE if that could not be determined."""
    signal_count: int = 0
    last_action: FeedbackAction | None = None

    @property
    def confidence(self) -> float:
        """0 (no signals yet) approaching 1 as evidence accumulates.

        Deliberately a simple saturating count rather than anything derived
        from the weights themselves: what a caller wants to know is "how many
        times has this traveler told us something", and diminishing returns
        past a handful of signals is the honest shape of that answer - the
        fifth click should not move confidence as much as the first.
        """
        # Half of a full count of signals gets to ~0.5, and it keeps climbing
        # slowly after that rather than ever quite reaching 1 - there is
        # always a little more a longer session could tell us.
        return round(self.signal_count / (self.signal_count + 4), 4)

    def explanation(self) -> str:
        if self.signal_count == 0:
            return (
                "No feedback yet for this session; recommendations are ranked "
                f"purely on the declared {self.declared_profile.value} profile."
            )
        direction = "confirmed" if self.last_action in POSITIVE_ACTIONS else "moved away from"
        return (
            f"Based on {self.signal_count} signal"
            f"{'s' if self.signal_count != 1 else ''} this session "
            f"(confidence {self.confidence:.0%}), the most recent action "
            f"{direction} the kind of trip it was given. The declared "
            f"{self.declared_profile.value} profile is unchanged; this is a "
            "separate, additive read on top of it."
        )


#: Session state, keyed by ``session_id``. See the module docstring's
#: "Storage" section: in-memory and single-process is a deliberate V6
#: simplification, not an oversight.
_SESSIONS: dict[str, SessionProfile] = {}


def reset_sessions() -> None:
    """Clear all in-memory session state. Test-only escape hatch."""
    _SESSIONS.clear()


def _cap_cumulative_drift(
    nudged: dict[str, float], baseline: dict[str, float]
) -> dict[str, float]:
    """Cap ``nudged``'s *total* distance from ``baseline``, per component.

    Both inputs already sum to 1.0, so their difference - the cumulative
    drift so far - sums to ~0.0. Shrinking that whole drift vector by one
    scalar factor when its worst component would exceed
    :data:`MAX_DRIFT_PER_COMPONENT` keeps every component within the cap
    *and* keeps the sum at 1.0 without a separate renormalization step: a
    uniform shrink of a zero-sum vector is still zero-sum. This is
    deliberately not a per-component clamp-then-rescale - rescaling a clamped
    vector back up to the original total can push a component that was
    exactly at its cap past it again, which defeats the cap it was supposed
    to enforce.
    """
    drift = {name: nudged[name] - baseline[name] for name in COMPONENTS}
    worst = max(abs(value) for value in drift.values())
    if worst > MAX_DRIFT_PER_COMPONENT:
        scale = MAX_DRIFT_PER_COMPONENT / worst
        drift = {name: value * scale for name, value in drift.items()}
    bounded = {name: max(baseline[name] + drift[name], MIN_WEIGHT) for name in COMPONENTS}
    total = sum(bounded.values())
    return {name: value / total for name, value in bounded.items()}


def _new_session(
    session_id: str,
    *,
    declared_profile: ProfileName,
    baseline: TravelValueWeights,
) -> SessionProfile:
    declared = get_profile(declared_profile).weights
    return SessionProfile(
        session_id=session_id,
        declared_profile=declared_profile,
        declared=declared,
        observed=baseline,
        baseline=baseline,
    )


def get_session(session_id: str | None) -> SessionProfile:
    """Look up a session's state, creating a fresh one when it is unknown.

    An empty or missing ``session_id`` still gets a session - one keyed on a
    generated id - rather than an error: the spec is explicit that the
    absence of a client-generated identifier must never fail the request.
    """
    key = session_id or "__anonymous__"
    existing = _SESSIONS.get(key)
    if existing is not None:
        return existing
    session = _new_session(
        key, declared_profile=ProfileName.BEST_VALUE, baseline=get_profile(ProfileName.BEST_VALUE).weights
    )
    _SESSIONS[key] = session
    return session


def record_feedback(
    session_id: str | None,
    action: FeedbackAction,
    trip_weights: dict[str, float],
    *,
    declared_profile: ProfileName | None = None,
    found_under_profile: ProfileName | None = None,
) -> SessionProfile:
    """Nudge (or create) a session's ``observed`` profile from one action.

    ``trip_weights`` is the trip's own ``value_breakdown``, in the same nine
    components :mod:`learning.py`'s ``Observation`` uses - what kind of trip
    this was, not a rating of it. ``found_under_profile``, when the caller
    knows it, seeds a brand-new session's baseline/anchor with the profile the
    trip actually came from rather than always assuming BEST_VALUE.
    """
    key = session_id or "__anonymous__"
    session = _SESSIONS.get(key)
    if session is None:
        baseline_name = found_under_profile or declared_profile or ProfileName.BEST_VALUE
        session = _new_session(
            key,
            declared_profile=declared_profile or ProfileName.BEST_VALUE,
            baseline=get_profile(baseline_name).weights,
        )
    elif declared_profile is not None and declared_profile != session.declared_profile:
        # The traveler explicitly changed their search profile: `declared`
        # is allowed to move here, and only here - never from a click.
        session = SessionProfile(
            session_id=session.session_id,
            declared_profile=declared_profile,
            declared=get_profile(declared_profile).weights,
            observed=session.observed,
            baseline=session.baseline,
            signal_count=session.signal_count,
            last_action=session.last_action,
        )

    current = session.observed.normalized()
    target = TravelValueWeights(**trip_weights).normalized()
    sign = 1.0 if action in POSITIVE_ACTIONS else -1.0
    strength = STEP_FRACTION * ACTION_STRENGTH[action.value]

    nudged = {}
    for name in COMPONENTS:
        # Move `strength` of the remaining distance towards the trip's
        # profile (positive actions) or away from it (negative actions, which
        # is the same update with the sign flipped - reinforcement in the
        # opposite direction, not a different mechanism).
        delta = sign * strength * (target[name] - current[name])
        nudged[name] = max(current[name] + delta, MIN_WEIGHT)

    total = sum(nudged.values())
    nudged = {name: value / total for name, value in nudged.items()}
    bounded = _cap_cumulative_drift(nudged, session.baseline.normalized())

    updated = SessionProfile(
        session_id=session.session_id,
        declared_profile=session.declared_profile,
        declared=session.declared,
        observed=TravelValueWeights(**{name: round(bounded[name], 6) for name in COMPONENTS}),
        baseline=session.baseline,
        signal_count=session.signal_count + 1,
        last_action=action,
    )
    _SESSIONS[key] = updated
    return updated


def blend(session: SessionProfile, *, observed_weight: float = 0.3) -> TravelValueWeights:
    """An explicit blend of ``declared`` and ``observed``, for a future caller.

    Not wired into ``/search`` - that remains a stretch goal, not a V6
    requirement. This exists so that if something downstream ever needs one
    number, it goes through a named function that says exactly how the two
    were combined, rather than one silently standing in for the other.
    """
    if not 0.0 <= observed_weight <= 1.0:
        raise ValueError("observed_weight must be within [0.0, 1.0]")
    declared = session.declared.normalized()
    observed = session.observed.normalized()
    blended = {
        name: (1 - observed_weight) * declared[name] + observed_weight * observed[name]
        for name in COMPONENTS
    }
    total = sum(blended.values())
    return TravelValueWeights(**{name: blended[name] / total for name in COMPONENTS})
