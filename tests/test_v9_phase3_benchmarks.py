"""V9 Phase 3 — the mandatory benchmark scenarios, as pytest assertions
(cold start, warm search, misleading prior, unknown discovery, origin
generalization). Nearby-exception, diversity-tradeoff, catalog-scale,
provider-budget and determinism are covered in
``test_v9_phase3_portfolio.py`` / ``test_v9_phase3_adaptive.py``; the
Cologne problem itself has a dedicated benchmark script
(``scripts/bench_phase3_cologne.py``) this file also sanity-checks."""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

# The Cologne benchmark script lives in scripts/, not a package on the
# default pythonpath (only `src` is configured for tests) — add the repo
# root so `from scripts.bench_phase3_cologne import run` resolves, exactly
# the way the script itself resolves `src` for its own imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from detoura.data.destinations import acquisition_catalog
from detoura.models.market_prior import (
    HistoricalMarketPriorSignal,
    PriorConfidence,
)
from detoura.models.search_intel import (
    ConfidenceBreakdown,
    HistoricalPriceSignal,
    MarketConfidence,
    MarketKey,
)
from detoura.persistence.db import Database
from detoura.services.attractiveness_import import seed_attractiveness
from detoura.services.candidate_funnel import run_funnel
from detoura.models.trip import TripRequest
from detoura.services.opportunity import OpportunityInput, score_opportunities


def _catalog():
    return list(acquisition_catalog())


def _req(**kw) -> TripRequest:
    f = dict(origin="CGN", budget=2000.0, travelers=1, duration_days=3,
             date_from=date(2026, 6, 1), date_to=date(2026, 6, 3))
    f.update(kw)
    return TripRequest(**f)


def _live(median_minor: int, *, confidence=MarketConfidence.HIGH, recency=0.9) -> HistoricalPriceSignal:
    return HistoricalPriceSignal(
        market=MarketKey.build(provider="duffel", origin="CGN", destination="AAA",
                               departure_date=date(2026, 6, 1)),
        currency="EUR", sample_count=8, median_observed_minor=median_minor,
        confidence=ConfidenceBreakdown(
            sample_component=0.9, recency_component=recency, consistency_component=0.9,
            score=0.9, verdict=confidence,
        ),
    )


def _prior(typical_minor: int, rel_attr: float, confidence=PriorConfidence.HIGH) -> HistoricalMarketPriorSignal:
    return HistoricalMarketPriorSignal(
        origin_airport="CGN", destination_airport="AAA", currency="EUR",
        prior_available=True, row_count=6, typical_estimate_minor=typical_minor,
        relative_price_attractiveness=rel_attr, confidence=confidence,
        confidence_components={"freshness": 0.8},
    )


# ======================================================================
# 1. COLD START — no live memory, some prior, some unknown
# ======================================================================
def test_benchmark_cold_start_no_live_some_prior_some_unknown():
    unknown = OpportunityInput("→unknown", 0, None, None, attractiveness_score=0.6)
    prior_known = OpportunityInput("→prior", 1, None, _prior(4000, 0.7), attractiveness_score=0.6)
    scored = {s.market: s for s in score_opportunities([unknown, prior_known])}
    assert scored["→unknown"].knowledge == "UNKNOWN"
    assert scored["→unknown"].stance.value == "EXPLORE"
    assert scored["→prior"].knowledge == "PRIOR"
    assert scored["→prior"].stance.value == "EXPLOIT"


# ======================================================================
# 2. WARM SEARCH — useful live Price Memory exists
# ======================================================================
def test_benchmark_warm_search_prefers_confirmed_live_deal():
    warm_cheap = OpportunityInput("→warm", 0, _live(2500), None, attractiveness_score=0.6)
    warm_expensive = OpportunityInput("→pricier", 1, _live(5500), None, attractiveness_score=0.6)
    scored = {s.market: s for s in score_opportunities([warm_cheap, warm_expensive])}
    assert scored["→warm"].knowledge == "LIVE"
    assert scored["→warm"].score > scored["→pricier"].score


# ======================================================================
# 3. MISLEADING PRIOR — prior favors a market live data later disproves
# ======================================================================
def test_benchmark_misleading_prior_is_overridden_by_disconfirming_live_data():
    """§A4: a bootstrap prior that suggested a market was a great deal must
    lose to fresh live evidence proving it is not, once that live evidence
    exists — precedence is LIVE > PRIOR, not "whichever is more optimistic"."""
    misleading_prior = _prior(2000, 0.95, confidence=PriorConfidence.HIGH)  # prior: "cheap!"
    disconfirming_live = _live(7000)  # live: actually expensive
    misled_but_corrected = OpportunityInput(
        "→corrected", 0, disconfirming_live, misleading_prior, attractiveness_score=0.6,
    )
    genuinely_cheap_live = OpportunityInput(
        "→genuine", 1, _live(2200), None, attractiveness_score=0.6,
    )
    scored = {s.market: s for s in score_opportunities([misled_but_corrected, genuinely_cheap_live])}
    assert scored["→corrected"].knowledge == "LIVE"
    assert scored["→corrected"].components["price_basis"] in ("live", "live+prior")
    # the genuinely cheap live market must clearly outrank the one whose
    # cheap reputation came only from a now-disconfirmed prior
    assert scored["→genuine"].score > scored["→corrected"].score


# ======================================================================
# 4. UNKNOWN DISCOVERY — an excellent destination with little/no history
# ======================================================================
def test_benchmark_unknown_discovery_excellent_destination_still_surfaces():
    """A destination with zero live/prior history but high attractiveness
    must still be able to win an acquisition slot via the EXPLORE floor —
    unknown != unreachable."""
    db = Database(":memory:")
    catalog = _catalog()
    seed_attractiveness(db, catalog)
    req = _req()
    result = run_funnel(
        db, req, catalog, origin_airports=["CGN"], departure_date=date(2026, 6, 1),
        slots=12, now=datetime.now(timezone.utc),
    )
    # every chosen candidate at cold start is, by construction, UNKNOWN —
    # prove the shortlist is nonetheless meaningfully attractiveness-weighted
    # (not just an arbitrary/alphabetical/catalog-order subset).
    assert len(result.chosen) == 12
    mean_attract_chosen = sum(
        result.scores[d.id].components["attractiveness"] for d in result.chosen
    ) / len(result.chosen)
    import random as _random
    rnd = _random.Random(1)
    random_subset = rnd.sample([d.id for d in catalog if d.acquisition_eligible], 12)
    mean_attract_random = sum(
        result.scores[i].components["attractiveness"] for i in random_subset if i in result.scores
    ) / len(random_subset)
    assert mean_attract_chosen >= mean_attract_random


# ======================================================================
# 10. ORIGIN GENERALIZATION — Cologne-style concentration test for others
# ======================================================================
def test_benchmark_origin_generalization_no_worse_than_baseline():
    from scripts.bench_phase3_cologne import run as run_cologne_bench

    for origin in ("Paris", "Vienna", "Warsaw"):
        report = run_cologne_bench(origin_id=origin, seed=42)
        baseline = report["1_price_heavy_baseline"]
        full = report["3_phase3_full_portfolio"]
        # Geographic spread and experience-similarity concentration are what
        # the portfolio pass directly optimises against — these must improve
        # for every origin. Country diversity is an explicit DIAGNOSTIC only
        # (§C7: never a quota), so it is reported but not asserted monotone —
        # a portfolio can legitimately improve geo/experience spread while
        # country diversity stays flat or dips slightly.
        assert full["mean_pairwise_distance_km"] >= baseline["mean_pairwise_distance_km"]
        assert full["mean_pairwise_experience_similarity"] <= baseline["mean_pairwise_experience_similarity"]
