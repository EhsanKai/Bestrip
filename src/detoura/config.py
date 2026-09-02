"""Planner configuration.

Every tunable number in the optimizer lives here so the algorithms stay free of
magic constants.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .usable_time import DEFAULT_DAY_END, DEFAULT_DAY_START, usable_day_minutes
from .profiles import DEFAULT_PROFILE, ProfileName


class ScoreWeights(BaseModel):
    """Relative importance of the six scoring components."""

    model_config = ConfigDict(frozen=True)

    budget: float = Field(default=0.40, ge=0.0)
    preference: float = Field(default=0.20, ge=0.0)
    destination: float = Field(default=0.15, ge=0.0)
    convenience: float = Field(default=0.10, ge=0.0)
    time: float = Field(default=0.10, ge=0.0)
    diversity: float = Field(default=0.05, ge=0.0)

    @model_validator(mode="after")
    def _check_nonzero(self) -> "ScoreWeights":
        if self.total <= 0:
            raise ValueError("at least one score weight must be positive")
        return self

    @property
    def total(self) -> float:
        return (
            self.budget
            + self.preference
            + self.destination
            + self.convenience
            + self.time
            + self.diversity
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "budget": self.budget,
            "preference": self.preference,
            "destination": self.destination,
            "convenience": self.convenience,
            "time": self.time,
            "diversity": self.diversity,
        }

    def normalized(self) -> dict[str, float]:
        """Weights rescaled to sum to 1.0, so the total score stays in [0, 1]."""
        total = self.total
        return {name: value / total for name, value in self.as_dict().items()}


class PlannerConfig(BaseModel):
    """Search, scoring and filtering configuration."""

    model_config = ConfigDict(frozen=True)

    # --- beam search -------------------------------------------------
    beam_width: int = Field(default=20, ge=1)
    max_results: int = Field(default=5, ge=1)
    max_cities: int = Field(default=4, ge=1)
    scale_beam_with_start_dates: bool = True
    """Widen the beam in proportion to the number of candidate start dates.

    A flexible-date request searches a state space ``len(start_dates)`` times
    larger than a fixed-date one. Holding the beam at a constant width would
    make the flexible search explore proportionally *less* of it and sometimes
    return a worse answer than the narrower request - which is indefensible
    from the user's point of view. Capped by ``max_effective_beam_width``.
    """
    max_effective_beam_width: int = Field(default=80, ge=1)

    # --- adaptive beam (V4) ------------------------------------------
    adaptive_beam: bool = False
    """Widen the beam until widening stops paying, instead of guessing a width.

    V3 measured what a fixed beam costs and admitted the default misses better
    itineraries: ``beam_width=40`` found a 0.6802 trip that the default 20 never
    reached. The honest fix is not a bigger guess, it is to stop guessing - run
    the search, double the beam, and keep doubling while the best score is still
    improving by more than :attr:`adaptive_beam_tolerance`.

    Re-running is cheaper than it sounds: every provider lookup is already
    cached from the previous round, so a widened round pays for search work
    only, never for upstream calls. The ladder still costs real time - doubling
    means the whole climb is a little under twice the widest round on its own.
    Measured on the standard request: 0.98s at a fixed beam of 40, 13.9s for
    the full ladder 40 -> 80 -> 160 -> 320, and the answer improves from 0.6729
    to 0.6823. That is the trade, stated rather than hidden.

    **Off by default**, because turning it on changes which itinerary wins and
    every number published for V3 was measured with a fixed beam. Turn it on and
    the engine finds better trips; leave it off and V3 reproduces exactly.
    """
    adaptive_beam_tolerance: float = Field(default=0.002, ge=0.0)
    """Score gain below which a wider beam is judged not to have paid.

    0.002 is about a tenth of the gap between adjacent recommendations on a
    typical result set - small enough that a real improvement is never mistaken
    for noise, large enough that the ladder stops.
    """
    adaptive_beam_max_rounds: int = Field(default=4, ge=1)
    """Hard ceiling on widenings, so a pathological request cannot run away."""
    adaptive_beam_time_budget_seconds: float | None = Field(default=None, gt=0.0)
    """Wall-clock ceiling on the adaptive ladder (V5.2).

    ``None`` means untimed, which is V4's behaviour. The DEEP search mode sets
    it, because a mode that tells the user "10-15 seconds" must not start a
    doubling it has no time to finish. Checked between rounds, never inside
    one, so it cannot make a single round non-deterministic - it only decides
    whether the *next* round happens.
    """
    adaptive_beam_max_width: int = Field(default=320, ge=1)
    """Widest beam the ladder may climb to.

    Deliberately *not* ``max_effective_beam_width``: that one exists to stop
    flexible-date scaling from multiplying an already-wide beam, and reusing it
    here would stop the ladder at 80 before it had plateaued - which is a
    ceiling masquerading as a stopping rule.
    """

    beam_slots_per_route: int = Field(default=2, ge=1)
    """Cap on beam slots sharing one city sequence.

    Without it the beam fills with the same trip departing from four airports on
    two dates at three times of day, and the search degenerates into a very
    expensive single-branch walk.
    """

    # --- stay durations ----------------------------------------------
    min_city_stay_days: int = Field(default=1, ge=1)
    max_city_stay_days: int = Field(default=4, ge=1)
    min_duration_utilization: float = Field(default=0.6, ge=0.0, le=1.0)
    """Fraction of the requested duration a finished itinerary must actually use.

    ``duration_days`` is an upper bound on elapsed time, but a user asking for
    five days does not want a 40-hour trip back. Set to ``0.0`` to disable and
    accept any itinerary that fits the window.
    """

    # --- origin discovery --------------------------------------------
    max_origin_distance_km: float = Field(default=250.0, gt=0.0)
    max_origin_airports: int = Field(default=4, ge=1)

    # --- scoring ------------------------------------------------------
    score_weights: ScoreWeights = Field(default_factory=ScoreWeights)
    max_travel_time_fraction: float = Field(default=0.25, gt=0.0, le=1.0)
    """Above this fraction of the trip spent in transit, TimeScore hits zero."""
    preferred_destination_bonus: float = Field(default=0.5, ge=0.0, le=1.0)
    must_visit_bonus: float = Field(default=0.3, ge=0.0, le=1.0)

    # --- filtering ----------------------------------------------------
    enable_pareto: bool = True
    enable_diversity: bool = True
    diversity_similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    """Two itineraries are "the same trip" above this Jaccard city overlap."""

    # --- accommodation (V2) -------------------------------------------
    # --- inventory (V4) ----------------------------------------------
    require_availability: bool = True
    """Refuse to book fares and rooms the provider says are sold out.

    Harmless against a feed that reports no inventory - ``None`` means
    *unknown*, and unknown stays bookable - so this is on by default and the
    synthetic providers only report counts when asked to
    (``simulate_scarcity``). Turn it off to price a trip against availability
    you intend to re-check at booking time.
    """

    enable_accommodation: bool = True
    """Charge for city stays. ``False`` restores the V1 "stays are free" model."""
    accommodation_options_per_stay: int = Field(default=2, ge=1)
    """How many accommodation tiers each stay branches on.

    ``1`` reproduces V2: always the cheapest sufficient room, so "pay more for a
    better hotel" is not a decision the optimizer can make. The V3 default of
    ``2`` gives it the cheapest option and one step up, which is enough for a
    real trade-off; ``3`` offers the full budget/standard/premium ladder.

    Complexity is multiplicative in this number: an ``n``-city itinerary
    branches ``accommodation_options_per_stay ** (n + 1)`` ways on rooms alone,
    which is precisely why it is bounded and why the beam is what keeps the
    total state count flat.
    """

    # --- ground transfer (V2) -----------------------------------------
    enable_ground_transfer: bool = True
    """Charge for getting from the user's origin to the departure airport."""

    # --- usable destination time (V2) ---------------------------------
    usable_day_start: time = Field(default=DEFAULT_DAY_START)
    usable_day_end: time = Field(default=DEFAULT_DAY_END)
    """The window in which a traveler can actually do something with their day.

    Arriving after ``usable_day_end`` or departing before ``usable_day_start``
    therefore contributes nothing to the trip's usable time.
    """

    # --- candidate generation limits (V3) -----------------------------
    # Real APIs make an unbounded Cartesian product of origins x dates x
    # destinations x departures x rooms x stay lengths impossible. Every axis
    # of that product has an explicit cap here.
    max_candidate_destinations: int | None = Field(default=None, ge=1)
    """Destinations considered per expansion. ``None`` means the whole catalog."""
    max_transport_options_per_leg: int = Field(default=4, ge=1)
    """Departures kept per (origin, destination, date). Cheapest first."""
    max_stay_lengths: int | None = Field(default=None, ge=1)
    """Stay lengths branched on. ``None`` means every configured length."""

    # --- experience (V3) ----------------------------------------------
    stay_overrun_tolerance_days: float = Field(default=3.0, gt=0.0)
    """Days past a city's recommended maximum at which stay quality bottoms out."""
    city_change_overhead_days: float = Field(default=0.5, ge=0.0)
    """Days an extra city costs beyond its own recommended stay.

    Packing up, travelling and checking in is most of a day. Ignoring it makes
    the planner think a five-day trip comfortably holds three cities.
    """

    # --- travel value (V2) --------------------------------------------
    profile: ProfileName = DEFAULT_PROFILE
    """Default recommendation profile when the request does not name one."""
    budget_utilization_target: float = Field(default=0.6, gt=0.0, le=1.0)
    """Budget share a trip may use before CostScore starts falling meaningfully."""
    comfortable_days_per_city: float = Field(default=2.0, gt=0.0)
    """Days per city at or above which an itinerary's pace stops being rushed."""

    # --- observability (V3) -------------------------------------------
    collect_provider_metrics: bool = True
    """Count provider calls and cache hits. Cheap; leave on."""

    # --- misc ---------------------------------------------------------
    currency: str = "EUR"
    debug_example_limit: int = Field(default=5, ge=0)
    """How many example rejected/pruned states to retain per iteration."""

    @model_validator(mode="after")
    def _check_stays(self) -> "PlannerConfig":
        if self.max_city_stay_days < self.min_city_stay_days:
            raise ValueError("max_city_stay_days must be >= min_city_stay_days")
        return self

    @model_validator(mode="after")
    def _check_usable_day(self) -> "PlannerConfig":
        if self.usable_day_end <= self.usable_day_start:
            raise ValueError("usable_day_end must be later than usable_day_start")
        return self

    @property
    def stay_day_options(self) -> tuple[int, ...]:
        """Candidate stay lengths generated for every visited city."""
        return tuple(range(self.min_city_stay_days, self.max_city_stay_days + 1))

    @property
    def usable_day_minutes(self) -> int:
        """Minutes in one fully usable day."""
        return usable_day_minutes(self.usable_day_start, self.usable_day_end)
