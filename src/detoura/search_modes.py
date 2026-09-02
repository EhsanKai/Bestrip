"""Product-level search modes (V5.2).

V4 exposed `adaptive_beam=True` and a beam width. Those are the right knobs for
an engineer and the wrong ones for a product: nobody choosing a holiday has an
opinion about beam width, and "14 seconds" is not a default anyone would pick.

So the user chooses an *intent* and the engine derives the configuration:

    QUICK   ~1s     "Show me strong candidates now."
    SMART   ~1-3s   "Find good alternatives without making me wait." (default)
    DEEP    ~10-20s "Search harder for the ones I'd never find myself."

DEEP is opt-in, always. The spec is explicit - *do not make every search take
14 seconds* - and the UI turns that constraint into the "Search deeper" feature
rather than hiding it.

The modes are a **mapping to existing config**, not a new search path. Nothing
in `beam_search.py` knows a mode exists, which is what keeps this a product
layer rather than a second optimizer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .config import PlannerConfig


class SearchMode(str, Enum):
    """How hard the traveler has asked us to look."""

    QUICK = "QUICK"
    SMART = "SMART"
    DEEP = "DEEP"

    @property
    def label(self) -> str:
        return {"QUICK": "Quick", "SMART": "Smart", "DEEP": "Deep"}[self.value]

    @property
    def blurb(self) -> str:
        return {
            "QUICK": "Strong candidates, fast.",
            "SMART": "Good alternatives without the wait.",
            "DEEP": "Search deeper for less obvious alternatives.",
        }[self.value]

    @property
    def estimated_seconds(self) -> tuple[float, float]:
        """A range honest enough to print next to the button."""
        return {
            "QUICK": (0.3, 1.5),
            "SMART": (0.8, 3.0),
            "DEEP": (8.0, 20.0),
        }[self.value]


DEFAULT_MODE = SearchMode.SMART


@dataclass(frozen=True, slots=True)
class ModeSettings:
    """The engine configuration one mode implies."""

    beam_width: int
    adaptive: bool
    max_rounds: int
    max_width: int
    max_transport_options_per_leg: int
    accommodation_options_per_stay: int
    time_budget_seconds: float | None
    """Wall-clock ceiling for adaptive widening. ``None`` means untimed."""


#: Chosen so QUICK is genuinely quick rather than nominally different.
#:
#: QUICK narrows the two branching factors that dominate cost - transport
#: options per leg and room tiers per stay - rather than only the beam. Halving
#: the beam alone would not reach a second, because the branching factor, not
#: the beam, is what generates states.
#:
#: SMART is V4's default exactly, so every published benchmark still describes
#: the default experience.
#:
#: DEEP is V4's adaptive ladder with a wall-clock ceiling added, because an
#: unbounded ladder on a pathological request is how a "10-15 seconds" promise
#: becomes a minute.
MODE_SETTINGS: dict[SearchMode, ModeSettings] = {
    SearchMode.QUICK: ModeSettings(
        beam_width=10,
        adaptive=False,
        max_rounds=1,
        max_width=20,
        max_transport_options_per_leg=2,
        accommodation_options_per_stay=1,
        time_budget_seconds=None,
    ),
    SearchMode.SMART: ModeSettings(
        beam_width=20,
        adaptive=False,
        max_rounds=1,
        max_width=80,
        max_transport_options_per_leg=4,
        accommodation_options_per_stay=2,
        time_budget_seconds=None,
    ),
    SearchMode.DEEP: ModeSettings(
        beam_width=20,
        adaptive=True,
        max_rounds=4,
        max_width=320,
        max_transport_options_per_leg=4,
        accommodation_options_per_stay=2,
        time_budget_seconds=25.0,
    ),
}


def apply_mode(config: PlannerConfig, mode: SearchMode) -> PlannerConfig:
    """Derive the planner configuration for a mode.

    Returns a copy: the caller's config is never mutated, so one planner can
    serve QUICK and DEEP requests concurrently without them interfering.
    """
    settings = MODE_SETTINGS[mode]
    return config.model_copy(
        update={
            "beam_width": settings.beam_width,
            "adaptive_beam": settings.adaptive,
            "adaptive_beam_max_rounds": settings.max_rounds,
            "adaptive_beam_max_width": settings.max_width,
            "max_transport_options_per_leg": settings.max_transport_options_per_leg,
            "accommodation_options_per_stay": settings.accommodation_options_per_stay,
            "adaptive_beam_time_budget_seconds": settings.time_budget_seconds,
        }
    )


def deeper_than(mode: SearchMode) -> SearchMode | None:
    """The next mode up, or ``None`` at the top.

    Drives the "Search deeper?" offer: it is only shown when there is
    genuinely somewhere deeper to go.
    """
    order = [SearchMode.QUICK, SearchMode.SMART, SearchMode.DEEP]
    index = order.index(mode)
    return order[index + 1] if index + 1 < len(order) else None
