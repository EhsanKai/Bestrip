"""Learning the Travel Value weights from what travelers actually pick (V4).

V3 shipped three profiles whose weights were hand-tuned against a synthetic
dataset, and said so in its own limitations: *"they are a starting point, not a
claim about real travelers."* This is how they stop being a guess.

**The signal.** Every planning run already produces the material: a handful of
itineraries were *shown*, and one of them was *chosen*. That single fact -
"this one beat those" - is a pairwise preference, and a few dozen of them pin
down nine weights perfectly well. Nothing else needs collecting: no ratings, no
surveys, no tracking.

**The method.** Exponentiated gradient ascent on the Bradley-Terry likelihood
of the observed choices, fitting a direction on the simplex and a sharpness
separately. Three properties earn it its place over anything fancier:

* the multiplicative update keeps every weight non-negative and the vector
  normalized *by construction*, which is exactly the constraint
  :class:`~travel_planner.profiles.TravelValueWeights` imposes - no projection
  step, no clipping, no way to produce an invalid profile;
* separating sharpness from direction is what makes the fit recover real
  weights rather than a corner of the simplex (see ``fit_weights``); and
* it is a few dozen lines of arithmetic with a fixed iteration count and no
  randomness, so it is deterministic, has no training loop to babysit, and adds
  no dependency. The spec's "no unnecessary frameworks" rule is not a reason to
  do this badly - it is a reason not to import a tensor library to fit nine
  numbers.

**What it recovers.** On planted preferences it identifies the components that
matter and gets their ratios roughly right - experience 0.45 / city count 0.30
comes back as 0.62 / 0.28 - with pairwise agreement rising from 80% to 94%. It
is biased towards the dominant component, which is inherent to learning a
fixed-budget weight vector from choices alone, and is why the fit is anchored
to a prior rather than trusted outright.

**What it is not.** This does not touch the optimizer. It produces a
:class:`~travel_planner.profiles.RecommendationProfile`, which the planner
already accepts from any caller. The deterministic core neither knows nor cares
that a weight vector was fitted rather than written down.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models.itinerary import PlanResult
from .profiles import (
    COMPONENTS,
    ProfileName,
    RecommendationProfile,
    TravelValueWeights,
    get_profile,
)

#: Steps of exponentiated gradient ascent. Fixed rather than "until converged":
#: a deterministic, bounded amount of work is worth more here than the last
#: fraction of a percent of likelihood.
DEFAULT_ITERATIONS = 200

#: Step size. Large enough to move a long way from the prior in a few dozen
#: observations, small enough that one unusual choice cannot dominate.
DEFAULT_LEARNING_RATE = 0.35

#: How hard the fitted weights are pulled back towards the prior, per step, at
#: :data:`REFERENCE_PAIRS` worth of evidence. This is what stops three
#: observations from producing a wild profile.
DEFAULT_PRIOR_STRENGTH = 0.02

#: Pairs of evidence at which the prior pulls with exactly
#: :data:`DEFAULT_PRIOR_STRENGTH`.
#:
#: The likelihood gradient is averaged over pairs so the step size does not
#: depend on how much data there is - which is good for stability and wrong for
#: the prior: averaged, two observations shove exactly as hard as two hundred,
#: and thin evidence produces a *more* extreme profile than thick. Scaling the
#: prior by ``REFERENCE_PAIRS / pairs`` restores what a fixed-strength prior
#: against a summed likelihood would have done: the prior dominates when
#: evidence is thin and is out-voted as it accumulates.
REFERENCE_PAIRS = 100.0

#: Weights below this are floored rather than allowed to reach zero. A
#: component at exactly zero can never recover - the multiplicative update
#: has nothing to multiply - so the model would be unable to change its mind.
MIN_WEIGHT = 1e-4

#: Cap on the fitted sharpness. Choices that happen to be perfectly separable
#: would otherwise drive it to infinity, which is confidence the evidence has
#: not earned.
MAX_SCALE = 60.0


class LearningError(ValueError):
    """Raised when observations cannot support a fit."""


@dataclass(frozen=True, slots=True)
class Observation:
    """One choice: ``chosen`` was picked over everything else in ``shown``.

    ``shown`` holds each candidate's nine component scores - exactly the
    ``value_breakdown`` the planner already returns - so this is recorded from
    a result the caller has in hand, not from extra instrumentation.
    """

    shown: tuple[dict[str, float], ...]
    chosen: int

    def __post_init__(self) -> None:
        if not 0 <= self.chosen < len(self.shown):
            raise LearningError(
                f"chosen index {self.chosen} is outside the {len(self.shown)} "
                "itineraries that were shown"
            )
        for candidate in self.shown:
            missing = set(COMPONENTS) - set(candidate)
            if missing:
                raise LearningError(f"candidate is missing components: {sorted(missing)}")

    @property
    def alternatives(self) -> list[dict[str, float]]:
        """Everything that lost."""
        return [c for i, c in enumerate(self.shown) if i != self.chosen]


@dataclass(frozen=True, slots=True)
class FitReport:
    """What a fit produced, and how well it explains the evidence."""

    weights: TravelValueWeights
    observations: int
    pairs: int
    """Pairwise comparisons the fit was trained on."""
    prior_accuracy: float
    """Share of pairs the starting weights already ranked correctly."""
    accuracy: float
    """Share the fitted weights rank correctly."""
    log_likelihood: float
    scale: float = 1.0
    """Fitted sharpness. High means the choices were highly consistent; it does
    not affect the ranking, only how confident the model is about it."""
    iterations: int = DEFAULT_ITERATIONS
    changed: dict[str, float] = field(default_factory=dict)
    """Fitted weight minus prior weight, per component, normalized."""

    @property
    def improvement(self) -> float:
        return round(self.accuracy - self.prior_accuracy, 6)

    def render(self) -> str:
        lines = [
            f"Fitted on {self.observations} choices ({self.pairs} pairs)",
            "-" * 42,
            f"  ranking accuracy  {self.prior_accuracy:.1%} -> {self.accuracy:.1%}"
            f"  ({self.improvement:+.1%})",
            f"  log likelihood    {self.log_likelihood:.4f}",
            "  weights:",
        ]
        normalized = self.weights.normalized()
        for name in COMPONENTS:
            delta = self.changed.get(name, 0.0)
            lines.append(f"    {name:<14} {normalized[name]:.4f}  ({delta:+.4f})")
        return "\n".join(lines)


def observations_from_result(
    result: PlanResult, chosen_rank: int
) -> Observation:
    """Turn "the traveler booked recommendation #n" into an observation.

    ``chosen_rank`` is the 1-based rank the caller sees, because that is what a
    UI has to hand.
    """
    if not result.recommendations:
        raise LearningError("a result with no recommendations carries no signal")
    breakdowns = []
    for itinerary in result.recommendations:
        value = itinerary.value_breakdown
        if value is None:
            raise LearningError(
                f"recommendation #{itinerary.rank} has no value breakdown to learn from"
            )
        breakdowns.append({name: getattr(value, name) for name in COMPONENTS})
    index = chosen_rank - 1
    if not 0 <= index < len(breakdowns):
        raise LearningError(
            f"rank {chosen_rank} is outside the {len(breakdowns)} recommendations shown"
        )
    return Observation(shown=tuple(breakdowns), chosen=index)


def _score(candidate: dict[str, float], weights: dict[str, float]) -> float:
    return sum(weights[name] * candidate[name] for name in COMPONENTS)


def _accuracy(observations: list[Observation], weights: dict[str, float]) -> tuple[float, int]:
    """Share of pairs where the chosen itinerary outscores the alternative."""
    correct = pairs = 0
    for observation in observations:
        chosen = _score(observation.shown[observation.chosen], weights)
        for alternative in observation.alternatives:
            pairs += 1
            if chosen > _score(alternative, weights):
                correct += 1
    return (correct / pairs if pairs else 0.0), pairs


def _log_likelihood(
    observations: list[Observation], weights: dict[str, float], *, scale: float = 1.0
) -> float:
    """Mean Bradley-Terry log likelihood of the observed choices."""
    total = 0.0
    pairs = 0
    for observation in observations:
        chosen = _score(observation.shown[observation.chosen], weights)
        for alternative in observation.alternatives:
            margin = scale * (chosen - _score(alternative, weights))
            # log sigma(margin), computed stably for large negative margins.
            total += -math.log1p(math.exp(-margin)) if margin > -30 else margin
            pairs += 1
    return total / pairs if pairs else 0.0


def fit_weights(
    observations: list[Observation],
    *,
    prior: TravelValueWeights | ProfileName | None = None,
    iterations: int = DEFAULT_ITERATIONS,
    learning_rate: float = DEFAULT_LEARNING_RATE,
    prior_strength: float = DEFAULT_PRIOR_STRENGTH,
) -> FitReport:
    """Fit the nine Travel Value weights to a set of observed choices.

    The fit starts from ``prior`` - by default the BEST_VALUE profile - and is
    pulled back towards it every step, so a handful of observations produces a
    profile that is recognisably a nudged BEST_VALUE rather than an
    overconfident invention.
    """
    if not observations:
        raise LearningError("no observations to learn from")
    if iterations < 1:
        raise LearningError("iterations must be >= 1")
    if learning_rate <= 0:
        raise LearningError("learning_rate must be > 0")

    if prior is None:
        prior_weights = get_profile(ProfileName.BEST_VALUE).weights
    elif isinstance(prior, TravelValueWeights):
        prior_weights = prior
    else:
        prior_weights = get_profile(prior).weights

    prior_vector = prior_weights.normalized()
    weights = dict(prior_vector)
    usable = [o for o in observations if o.alternatives]
    if not usable:
        raise LearningError(
            "every observation shows a single itinerary; a choice with no "
            "alternative carries no preference information"
        )

    prior_accuracy, pairs = _accuracy(usable, prior_vector)
    # See REFERENCE_PAIRS: the prior has to be scaled against the averaged
    # likelihood gradient, or thin evidence produces the wilder profile.
    anchor = prior_strength * REFERENCE_PAIRS / pairs

    # Direction and sharpness are fitted separately, and this is the one design
    # decision that makes the fit work at all. The weights live on the simplex
    # (they must: that is what a profile is), which fixes their total and so
    # also fixes how large a score gap the model can express. Left coupled, the
    # only way for the likelihood to sharpen its margins is to pile the whole
    # budget onto the single most discriminative component - so a traveler who
    # weighs experience 0.45 and city count 0.30 comes back as experience 0.97.
    # ``scale`` gives sharpness somewhere else to go, and the weights are then
    # free to keep their true ratios.
    scale = 1.0

    for _ in range(iterations):
        # Gradient of the mean Bradley-Terry log likelihood. Each losing pair
        # pushes weight towards the components where the chosen trip was
        # better, in proportion to how surprised the model was to lose.
        gradient = {name: 0.0 for name in COMPONENTS}
        scale_gradient = 0.0
        for observation in usable:
            winner = observation.shown[observation.chosen]
            for alternative in observation.alternatives:
                deltas = {
                    name: winner[name] - alternative[name] for name in COMPONENTS
                }
                separation = sum(weights[name] * deltas[name] for name in COMPONENTS)
                margin = scale * separation
                # sigma(-margin): 1 when the model has it badly wrong, ~0 when
                # it already agrees, so confident-correct pairs stop pulling.
                surprise = 1.0 / (1.0 + math.exp(margin)) if margin > -30 else 1.0
                for name in COMPONENTS:
                    gradient[name] += surprise * scale * deltas[name]
                scale_gradient += surprise * separation

        # Exponentiated gradient: multiplicative, so weights stay positive and
        # renormalize to a simplex without a projection step.
        updated = {}
        for name in COMPONENTS:
            step = learning_rate * gradient[name] / pairs
            step += anchor * (prior_vector[name] - weights[name])
            updated[name] = max(weights[name] * math.exp(step), MIN_WEIGHT)
        total = sum(updated.values())
        weights = {name: value / total for name, value in updated.items()}
        # Scale is positive, so it gets a multiplicative step too, and a cap so
        # a perfectly separable set cannot run it away to infinity.
        scale = min(
            max(scale * math.exp(learning_rate * scale_gradient / pairs), MIN_WEIGHT),
            MAX_SCALE,
        )

    # Accuracy is scale-invariant - it only compares scores - so it is
    # reported on the weights themselves. The likelihood is not, so it is
    # reported at the fitted sharpness.
    accuracy, _ = _accuracy(usable, weights)
    fitted = TravelValueWeights(**{name: round(weights[name], 6) for name in COMPONENTS})
    return FitReport(
        weights=fitted,
        observations=len(usable),
        pairs=pairs,
        prior_accuracy=round(prior_accuracy, 6),
        accuracy=round(accuracy, 6),
        log_likelihood=round(_log_likelihood(usable, weights, scale=scale), 6),
        scale=round(scale, 4),
        iterations=iterations,
        changed={
            name: round(weights[name] - prior_vector[name], 6) for name in COMPONENTS
        },
    )


def learned_profile(
    report: FitReport,
    *,
    name: ProfileName = ProfileName.BEST_VALUE,
    template: RecommendationProfile | None = None,
) -> RecommendationProfile:
    """Wrap fitted weights in a profile the planner will accept.

    Everything other than the weights - the budget-utilization split, the
    diversity threshold, the city-count bias - is inherited from ``template``.
    Those are behavioural policy, not taste, and nothing in a click stream
    identifies them.
    """
    base = template or get_profile(name)
    return base.model_copy(
        update={
            "weights": report.weights,
            "description": (
                f"Learned from {report.observations} choices "
                f"({report.accuracy:.0%} pairwise agreement); "
                f"based on {base.name.value}."
            ),
        }
    )
