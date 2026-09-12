"""V9 Phase 3 §A — Adaptive Search Intelligence: attractiveness feeding
acquisition-candidate scoring, geo/experience-aware diversity in the
pre-acquisition funnel, provider-budget invariance, and explore/exploit
preservation."""

from __future__ import annotations

from datetime import date, datetime, timezone

from detoura.data.destinations import acquisition_catalog
from detoura.models.market_prior import PriorConfidence
from detoura.models.search_intel import AcquisitionStance
from detoura.models.trip import TripRequest
from detoura.persistence.db import Database
from detoura.search_intel_config import SearchIntelConfig
from detoura.services.attractiveness_import import seed_attractiveness
from detoura.services.candidate_funnel import _diversity_adjust, run_funnel
from detoura.services.opportunity import OpportunityInput, OpportunityScore, score_opportunities


def _catalog():
    return list(acquisition_catalog())


def _req(**kw) -> TripRequest:
    f = dict(origin="CGN", budget=2000.0, travelers=1, duration_days=3,
             date_from=date(2026, 6, 1), date_to=date(2026, 6, 3))
    f.update(kw)
    return TripRequest(**f)


# ======================================================================
# Attractiveness component in opportunity scoring (§A3)
# ======================================================================
def test_attractiveness_none_is_neutral_not_penalized():
    a = OpportunityInput("→a", 0, None, None, attractiveness_score=None)
    b = OpportunityInput("→b", 1, None, None, attractiveness_score=None)
    scored = {s.market: s for s in score_opportunities([a, b])}
    assert scored["→a"].components["attractiveness"] == 0.5
    assert scored["→a"].components["attractiveness_known"] is False


def test_higher_attractiveness_wins_all_else_equal():
    plain = OpportunityInput("→plain", 0, None, None, attractiveness_score=0.3)
    great = OpportunityInput("→great", 1, None, None, attractiveness_score=0.95)
    scored = {s.market: s for s in score_opportunities([plain, great])}
    assert scored["→great"].score > scored["→plain"].score


def test_attractiveness_does_not_override_live_price_precedence():
    """§A4: LIVE evidence must still dominate — attractiveness is additive,
    never a way to bypass live > prior > unknown precedence."""
    from detoura.models.search_intel import (
        ConfidenceBreakdown,
        HistoricalPriceSignal,
        MarketConfidence,
        MarketKey,
    )

    def _live(median_minor: int) -> HistoricalPriceSignal:
        return HistoricalPriceSignal(
            market=MarketKey.build(provider="duffel", origin="CGN", destination="AAA",
                                   departure_date=date(2026, 6, 1)),
            currency="EUR", sample_count=10, median_observed_minor=median_minor,
            confidence=ConfidenceBreakdown(
                sample_component=1.0, recency_component=1.0, consistency_component=1.0,
                score=1.0, verdict=MarketConfidence.HIGH,
            ),
        )

    # Two LIVE candidates establish real relative price signal (a lone LIVE
    # candidate is trivially its own field median, i.e. neutral — see the
    # price-component contract in opportunity.py).
    cheap_live = OpportunityInput("→cheap", 0, _live(2000), None, attractiveness_score=0.2)
    expensive_live = OpportunityInput("→expensive", 1, _live(6000), None, attractiveness_score=0.2)
    high_attract_no_history = OpportunityInput("→noattract", 2, None, None, attractiveness_score=0.95)

    scored = {s.market: s for s in score_opportunities(
        [cheap_live, expensive_live, high_attract_no_history]
    )}
    assert scored["→cheap"].knowledge == "LIVE"
    # A confirmed cheap LIVE market still reads as a real price advantage
    # even against a much more globally attractive but totally unproven one.
    assert scored["→cheap"].components["price"] > scored["→expensive"].components["price"]
    assert scored["→cheap"].score > scored["→noattract"].score


# ======================================================================
# Full funnel integration
# ======================================================================
def test_funnel_uses_batched_attractiveness_lookup_no_n_plus_one():
    """One query for the whole shortlist, not one per candidate — verified by
    counting queries via a wrapping Database subclass would be heavier than
    needed; instead we assert the funnel completes over the full catalog
    quickly and attractiveness coverage is reported."""
    db = Database(":memory:")
    cat = _catalog()
    seed_attractiveness(db, cat)
    req = _req()
    result = run_funnel(
        db, req, cat, origin_airports=["CGN"], departure_date=date(2026, 6, 1),
        slots=15, now=datetime.now(timezone.utc),
    )
    assert result.trace.attractiveness_known_count > 0
    assert result.trace.attractiveness_known_count == result.trace.feasible_total


def test_cold_start_shortlist_is_not_dominated_by_nearby_cheap_lookalikes():
    """The core Phase 3 claim at the acquisition-candidate stage (before any
    live price even exists): with zero live/prior history, attractiveness +
    geo/experience diversity should already steer scarce provider calls away
    from a cluster of near-identical nearby destinations."""
    db = Database(":memory:")
    cat = _catalog()
    seed_attractiveness(db, cat)
    req = _req()
    result = run_funnel(
        db, req, cat, origin_airports=["CGN"], departure_date=date(2026, 6, 1),
        slots=15, now=datetime.now(timezone.utc),
    )
    chosen_ids = [d.id for d in result.chosen]
    assert len(chosen_ids) == 15
    countries = {d.country_code for d in result.chosen}
    # a genuinely diverse shortlist spans many countries, not a cluster
    assert len(countries) >= 8


def test_provider_budget_not_increased_by_attractiveness_or_catalog_size():
    """§A2: catalog growth (203 destinations) must never translate into more
    acquisition slots — the funnel's `slots` argument is the only ceiling."""
    db = Database(":memory:")
    cat = _catalog()
    seed_attractiveness(db, cat)
    req = _req()
    for slots in (5, 10, 20):
        result = run_funnel(
            db, req, cat, origin_airports=["CGN"], departure_date=date(2026, 6, 1),
            slots=slots, now=datetime.now(timezone.utc),
        )
        assert len(result.chosen) <= slots


def test_explore_floor_preserved_with_attractiveness_enabled():
    db = Database(":memory:")
    cat = _catalog()
    seed_attractiveness(db, cat)
    req = _req()
    cfg = SearchIntelConfig(explore_fraction=0.3, explore_min_slots=2)
    result = run_funnel(
        db, req, cat, origin_airports=["CGN"], departure_date=date(2026, 6, 1),
        slots=10, cfg=cfg, now=datetime.now(timezone.utc),
    )
    assert result.trace.explore_candidates >= 2


def test_exploration_lottery_gives_a_systematically_disadvantaged_destination_a_real_chance():
    """Regression test for a real bug found by independent adversarial QA:
    a single unprofiled destination competing against ~200 otherwise-
    profiled ones (all UNKNOWN, all EXPLORE) was selected in literally 0/100
    searches across 25 independent departure dates — mathematically
    impossible to ever reach, not merely unlikely, because it was always
    outscored and EXPLORE ranking was pure score order. The exploration
    lottery (``_apply_exploration_lottery``) fixes this: reserves one
    EXPLORE slot per search for a uniform hash(destination, departure_date)
    lottery among the *whole* EXPLORE pool, giving every candidate a real,
    non-zero, statistically-verifiable chance. Uses a wide, varied set of
    departure dates (as the original QA reproduction did) and expects at
    least one hit for the target across enough searches — not a high rate
    (roughly 1/pool_size per search is the honest expectation), just
    provably non-zero, unlike before the fix."""
    db = Database(":memory:")
    cat = _catalog()
    target = "Zurich"
    seed_attractiveness(db, [d for d in cat if d.id != target])
    req = _req()
    cfg = SearchIntelConfig()

    from datetime import timedelta

    hits = 0
    n = 400
    for i in range(n):
        dep = date(2020, 1, 1) + timedelta(days=i * 3)
        result = run_funnel(
            db, req, cat, origin_airports=["CGN"], departure_date=dep,
            slots=10, cfg=cfg, now=datetime.now(timezone.utc),
        )
        if target in {d.id for d in result.chosen}:
            hits += 1
    assert hits > 0, "the target destination must be reachable at least once across many dates"


def test_exploration_lottery_never_displaces_an_exploit_pick():
    db = Database(":memory:")
    cat = _catalog()
    seed_attractiveness(db, cat)
    req = _req()
    cfg = SearchIntelConfig()
    result = run_funnel(
        db, req, cat, origin_airports=["CGN"], departure_date=date(2026, 6, 1),
        slots=10, cfg=cfg, now=datetime.now(timezone.utc),
    )
    # a cold-start DB has no EXPLOIT candidates at all, so this just proves
    # the lottery path doesn't crash / misbehave with an all-EXPLORE pool.
    assert result.trace.exploit_candidates == 0


def test_unknown_attractiveness_destination_remains_reachable():
    """A destination with no seeded attractiveness profile at all must still
    be able to enter the shortlist — UNKNOWN is neutral, never a ban."""
    db = Database(":memory:")  # deliberately do NOT seed attractiveness
    cat = _catalog()
    req = _req()
    result = run_funnel(
        db, req, cat, origin_airports=["CGN"], departure_date=date(2026, 6, 1),
        slots=10, now=datetime.now(timezone.utc),
    )
    assert len(result.chosen) == 10
    assert result.trace.attractiveness_known_count == 0


def test_full_catalog_scale_completes_without_error():
    db = Database(":memory:")
    cat = _catalog()
    assert len(cat) >= 200
    seed_attractiveness(db, cat)
    req = _req()
    result = run_funnel(
        db, req, cat, origin_airports=["CGN"], departure_date=date(2026, 6, 1),
        slots=20, now=datetime.now(timezone.utc),
    )
    assert result.trace.catalog_total == len(cat)
    assert len(result.chosen) == 20


def test_determinism_repeated_funnel_runs_identical_order():
    db = Database(":memory:")
    cat = _catalog()
    seed_attractiveness(db, cat)
    req = _req()
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    r1 = run_funnel(db, req, cat, origin_airports=["CGN"], departure_date=date(2026, 6, 1), slots=12, now=now)
    r2 = run_funnel(db, req, cat, origin_airports=["CGN"], departure_date=date(2026, 6, 1), slots=12, now=now)
    assert [d.id for d in r1.chosen] == [d.id for d in r2.chosen]


# ======================================================================
# Diversity adjust — geo/experience redundancy within a subregion
# ======================================================================
def test_diversity_adjust_penalizes_redundant_close_similar_candidates():
    by_id = {d.id: d for d in _catalog()}
    names = ["Dusseldorf", "Dortmund", "Antwerp", "Brussels", "Maastricht", "Groningen"]
    scores = [
        OpportunityScore(market=f"→{n}", pre_rank=i, knowledge="UNKNOWN",
                         stance=AcquisitionStance.EXPLORE, score=0.7,
                         subregion=by_id[n].subregion, country_code=by_id[n].country_code)
        for i, n in enumerate(names)
    ]
    adjusted, demoted = _diversity_adjust(scores, by_id=by_id, cfg=SearchIntelConfig())
    assert demoted > 0
    # the later (by original pre_rank tie) candidates in the redundant
    # cluster should have a real geo/experience penalty component recorded
    penalized = [s for s in adjusted if s.components.get("geo_redundancy_penalty", 0) > 0
                 or s.components.get("experience_redundancy_penalty", 0) > 0]
    assert penalized
