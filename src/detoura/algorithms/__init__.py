"""Deterministic optimization algorithms.

No LLM is involved in anything here: route search, pricing, constraint checks
and scoring are all plain, reproducible computation.
"""

from .beam_search import BeamSearchOptimizer
from .diversity import DiversityResult, diversify, jaccard_similarity
from .pareto import ParetoResult, dominates, pareto_filter
from .scoring import Objectives, ScoringEngine

__all__ = [
    "BeamSearchOptimizer",
    "DiversityResult",
    "Objectives",
    "ParetoResult",
    "ScoringEngine",
    "diversify",
    "dominates",
    "jaccard_similarity",
    "pareto_filter",
]
