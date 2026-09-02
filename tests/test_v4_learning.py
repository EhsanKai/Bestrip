"""Learning the Travel Value weights from observed choices (V4).

V3's limitations section said the profile weights "are tuned against the
synthetic dataset. They are a starting point, not a claim about real
travelers." These tests are what turns that starting point into something a
click stream can correct.
"""

from __future__ import annotations

import random

import pytest

from detoura.config import PlannerConfig
from detoura.learning import (
    DEFAULT_ITERATIONS,
    MAX_SCALE,
    FitReport,
    LearningError,
    Observation,
    fit_weights,
    learned_profile,
    observations_from_result,
)
from detoura.profiles import COMPONENTS, PROFILES, ProfileName, TravelValueWeights
from detoura.services.planner import TravelPlanner

from .conftest import trip_request


def normalize(raw: dict[str, float]) -> dict[str, float]:
    total = sum(raw.values())
    return {name: value / total for name, value in raw.items()}


def planted(**emphasis: float) -> dict[str, float]:
    """A synthetic traveler who cares about the named components."""
    weights = {name: 0.02 for name in COMPONENTS}
    weights.update(emphasis)
    return normalize(weights)


def choices(truth: dict[str, float], *, count: int = 60, shown: int = 5, seed: int = 7):
    """Choices a traveler with these weights would make, from random menus."""
    rng = random.Random(seed)
    observations = []
    for _ in range(count):
        menu = [
            {name: round(rng.random(), 4) for name in COMPONENTS} for _ in range(shown)
        ]
        best = max(
            range(shown),
            key=lambda i: sum(truth[n] * menu[i][n] for n in COMPONENTS),
        )
        observations.append(Observation(shown=tuple(menu), chosen=best))
    return observations


TRUTH = planted(experience=0.45, city_count=0.30, cost=0.05, time=0.10)


@pytest.fixture(scope="module")
def report() -> FitReport:
    return fit_weights(choices(TRUTH))


# ---------------------------------------------------------------------------
# Does it learn anything?
# ---------------------------------------------------------------------------
def test_it_explains_the_choices_better_than_the_prior(report):
    assert report.accuracy > report.prior_accuracy
    assert report.improvement > 0.05
    assert report.accuracy > 0.9


def test_it_finds_the_components_that_actually_mattered(report):
    """The two the traveler cares about must come out on top, in order."""
    fitted = report.weights.normalized()
    ranked = sorted(COMPONENTS, key=lambda name: -fitted[name])
    assert ranked[:2] == ["experience", "city_count"]


def test_it_gets_the_ratios_roughly_right(report):
    """Not exact - a fixed-budget vector fitted from choices never is - but the
    dominant pair must be recognisably in proportion."""
    fitted = report.weights.normalized()
    assert fitted["experience"] / fitted["city_count"] == pytest.approx(
        TRUTH["experience"] / TRUTH["city_count"], rel=0.6
    )


def test_it_learns_that_something_did_not_matter(report):
    """Components the traveler ignored must fall well below the prior."""
    fitted = report.weights.normalized()
    prior = PROFILES[ProfileName.BEST_VALUE].weights.normalized()
    for ignored in ("accommodation", "convenience", "preferences"):
        assert fitted[ignored] < prior[ignored] / 4


def test_a_different_traveler_gets_different_weights():
    """The fit must track the person, not converge on one answer."""
    thrifty = fit_weights(choices(planted(cost=0.70), seed=11)).weights.normalized()
    explorer = fit_weights(
        choices(planted(diversity=0.50, city_count=0.30), seed=11)
    ).weights.normalized()
    assert thrifty["cost"] > 0.5
    assert explorer["diversity"] > thrifty["diversity"] * 5
    assert explorer["cost"] < thrifty["cost"] / 5


def held_out_accuracy(weights: TravelValueWeights, observations) -> float:
    """Share of unseen choices the fitted weights would have got right."""
    vector = weights.normalized()
    correct = pairs = 0
    for observation in observations:
        chosen = sum(
            vector[n] * observation.shown[observation.chosen][n] for n in COMPONENTS
        )
        for alternative in observation.alternatives:
            pairs += 1
            correct += chosen > sum(vector[n] * alternative[n] for n in COMPONENTS)
    return correct / pairs


def test_more_evidence_generalizes_better():
    """Measured on choices the fit never saw.

    Training accuracy is not the test: ten observations reach 100% on their own
    pairs simply because there are fewer of them to get wrong. What has to
    improve with evidence is agreement with the *next* choice the traveler
    makes.
    """
    unseen = choices(TRUTH, count=40, seed=99)
    small = fit_weights(choices(TRUTH, count=6, seed=3))
    large = fit_weights(choices(TRUTH, count=80, seed=3))
    assert held_out_accuracy(large.weights, unseen) > held_out_accuracy(
        small.weights, unseen
    )


def test_a_fit_beats_the_prior_on_unseen_choices(report):
    """The claim that matters: this predicts better, not just fits better."""
    unseen = choices(TRUTH, count=40, seed=1234)
    fitted = held_out_accuracy(report.weights, unseen)
    prior = held_out_accuracy(PROFILES[ProfileName.BEST_VALUE].weights, unseen)
    assert fitted > prior


# ---------------------------------------------------------------------------
# The prior does its job
# ---------------------------------------------------------------------------
def test_thin_evidence_stays_close_to_the_prior():
    """Three clicks must not produce a confident, wild profile."""
    prior = PROFILES[ProfileName.BEST_VALUE].weights.normalized()
    thin = fit_weights(choices(TRUTH, count=2)).weights.normalized()
    thick = fit_weights(choices(TRUTH, count=60)).weights.normalized()
    drift_thin = sum(abs(thin[n] - prior[n]) for n in COMPONENTS)
    drift_thick = sum(abs(thick[n] - prior[n]) for n in COMPONENTS)
    assert drift_thin < drift_thick


def test_the_prior_can_be_chosen():
    cheapest = fit_weights(choices(TRUTH, count=3), prior=ProfileName.CHEAPEST)
    adventure = fit_weights(choices(TRUTH, count=3), prior=ProfileName.ADVENTURE)
    assert cheapest.weights != adventure.weights
    assert cheapest.prior_accuracy != adventure.prior_accuracy


def test_a_custom_prior_is_accepted():
    custom = TravelValueWeights(**{**{n: 0.0 for n in COMPONENTS}, "diversity": 1.0})
    report = fit_weights(choices(TRUTH, count=5), prior=custom)
    assert report.weights.normalized()["diversity"] > 0.0


# ---------------------------------------------------------------------------
# The output is always a usable profile
# ---------------------------------------------------------------------------
def test_the_fitted_weights_are_a_valid_weight_vector(report):
    """No projection step, no clipping - the update cannot produce an invalid
    profile, and this is the assertion that keeps it that way."""
    normalized = report.weights.normalized()
    assert sum(normalized.values()) == pytest.approx(1.0)
    assert all(value >= 0.0 for value in normalized.values())
    assert set(normalized) == set(COMPONENTS)


def test_no_component_is_ever_driven_to_exactly_zero():
    """A zero weight can never recover under a multiplicative update."""
    extreme = fit_weights(choices(planted(cost=0.98), count=80))
    assert all(value > 0.0 for value in extreme.weights.as_dict().values())


def test_the_sharpness_is_bounded():
    """Perfectly separable choices must not buy unearned confidence."""
    separable = fit_weights(choices(planted(cost=0.99), count=80))
    assert 1.0 <= separable.scale <= MAX_SCALE


def test_it_produces_a_profile_the_planner_accepts(report):
    profile = learned_profile(report)
    planner = TravelPlanner()
    result = planner.plan(trip_request(budget=450, travelers=2), profile=profile)
    assert result.recommendations
    assert result.profile is profile.name


def test_the_learned_profile_inherits_behavioural_policy(report):
    """Weights are taste; thresholds and biases are policy and are not learned."""
    template = PROFILES[ProfileName.ADVENTURE]
    profile = learned_profile(report, template=template)
    assert profile.weights == report.weights
    assert profile.diversity_similarity_threshold == (
        template.diversity_similarity_threshold
    )
    assert profile.city_count_bias == template.city_count_bias
    assert profile.budget_utilization_weight == template.budget_utilization_weight


def test_the_learned_profile_says_where_it_came_from(report):
    assert "Learned from" in learned_profile(report).description


def test_a_learned_profile_still_respects_the_hard_constraints(report):
    """Learning changes what is preferred, never what is allowed."""
    profile = learned_profile(report)
    request = trip_request(budget=450, travelers=2)
    for itinerary in TravelPlanner().plan(request, profile=profile).recommendations:
        assert itinerary.total_cost <= request.budget
        assert "Paris" not in itinerary.cities


# ---------------------------------------------------------------------------
# Building observations from real results
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def planned():
    return TravelPlanner().plan(trip_request(budget=450, travelers=2))


def test_a_real_result_becomes_an_observation(planned):
    observation = observations_from_result(planned, chosen_rank=2)
    assert observation.chosen == 1
    assert len(observation.shown) == len(planned.recommendations)
    assert set(observation.shown[0]) == set(COMPONENTS)


def test_the_observation_carries_the_planners_own_numbers(planned):
    observation = observations_from_result(planned, chosen_rank=1)
    value = planned.recommendations[0].value_breakdown
    assert observation.shown[0]["experience"] == value.experience
    assert observation.shown[0]["cost"] == value.cost


def test_choices_from_real_results_can_be_fitted(planned):
    """The whole loop: plan, someone picks one, refit."""
    report = fit_weights([observations_from_result(planned, chosen_rank=r) for r in (3, 3, 3)])
    assert report.pairs == 3 * (len(planned.recommendations) - 1)
    assert report.accuracy >= report.prior_accuracy


def test_learning_that_travelers_prefer_the_cheap_option():
    """A cohort that always books the cheapest must shift weight to cost."""
    planner = TravelPlanner()
    observations = []
    for budget in (400, 450, 500, 550, 600, 700):
        result = planner.plan(trip_request(budget=budget, travelers=2))
        if len(result.recommendations) < 2:
            continue
        cheapest = min(result.recommendations, key=lambda i: i.total_cost)
        observations.append(observations_from_result(result, cheapest.rank))
    assert len(observations) >= 4
    report = fit_weights(observations)
    prior = PROFILES[ProfileName.BEST_VALUE].weights.normalized()
    assert report.weights.normalized()["cost"] > prior["cost"]
    assert report.accuracy > report.prior_accuracy


# ---------------------------------------------------------------------------
# Determinism and validation
# ---------------------------------------------------------------------------
def test_the_fit_is_deterministic():
    observations = choices(TRUTH, count=20)
    assert fit_weights(observations) == fit_weights(observations)


def test_it_rejects_an_empty_history():
    with pytest.raises(LearningError, match="no observations"):
        fit_weights([])


def test_it_rejects_a_choice_with_no_alternative():
    """One itinerary shown and picked says nothing about preferences."""
    single = Observation(shown=({name: 0.5 for name in COMPONENTS},), chosen=0)
    with pytest.raises(LearningError, match="single itinerary"):
        fit_weights([single])


def test_it_rejects_an_out_of_range_choice():
    menu = tuple({name: 0.5 for name in COMPONENTS} for _ in range(3))
    with pytest.raises(LearningError, match="outside"):
        Observation(shown=menu, chosen=5)


def test_it_rejects_a_candidate_missing_components():
    with pytest.raises(LearningError, match="missing components"):
        Observation(shown=({"cost": 0.5}, {"cost": 0.2}), chosen=0)


def test_it_rejects_a_rank_that_was_never_shown(planned):
    with pytest.raises(LearningError, match="outside"):
        observations_from_result(planned, chosen_rank=99)


def test_it_rejects_a_result_with_nothing_to_learn_from():
    empty = TravelPlanner().plan(trip_request(budget=30))
    with pytest.raises(LearningError, match="no recommendations"):
        observations_from_result(empty, chosen_rank=1)


def test_it_rejects_nonsense_hyperparameters():
    observations = choices(TRUTH, count=5)
    with pytest.raises(LearningError, match="iterations"):
        fit_weights(observations, iterations=0)
    with pytest.raises(LearningError, match="learning_rate"):
        fit_weights(observations, learning_rate=0.0)


def test_it_renders(report):
    text = report.render()
    assert "ranking accuracy" in text
    for name in COMPONENTS:
        assert name in text


# ---------------------------------------------------------------------------
# It must stay out of the optimizer
# ---------------------------------------------------------------------------
def test_the_optimizer_does_not_depend_on_the_learning_module():
    """Same rule as the LLM seams: the deterministic core stays independent."""
    import ast
    import pathlib

    import detoura

    root = pathlib.Path(detoura.__file__).parent
    offenders = []
    for package in ("algorithms", "constraints", "providers", "data", "models"):
        for path in (root / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and "learning" in (node.module or ""):
                    offenders.append(str(path))
                elif isinstance(node, ast.Import) and any(
                    "learning" in alias.name for alias in node.names
                ):
                    offenders.append(str(path))
    assert offenders == []


def test_learning_never_changes_the_shipped_profiles(report):
    """A fit must not mutate the module-level profile table."""
    before = PROFILES[ProfileName.BEST_VALUE].weights.model_copy()
    learned_profile(report)
    assert PROFILES[ProfileName.BEST_VALUE].weights == before


def test_the_default_planner_is_unaffected():
    """Nothing learns unless a caller asks it to."""
    assert PlannerConfig().profile is ProfileName.BEST_VALUE
    result = TravelPlanner().plan(trip_request(budget=450, travelers=2))
    assert result.recommendations[0].value_breakdown.weights == (
        PROFILES[ProfileName.BEST_VALUE].weights.normalized()
    )
