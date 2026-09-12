"""V9 Phase 3 §C/§D — the final Recommendation Portfolio: cheapness
diminishing returns, base value, diversified greedy selection, and the
Itinerary adapter. This is the module the Cologne benchmark exercises."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from detoura.data.destinations import acquisition_catalog
from detoura.models.itinerary import Itinerary
from detoura.models.trip import TripRequest
from detoura.search_intel_config import SearchIntelConfig
from detoura.services.portfolio import (
    PortfolioCandidate,
    apply_portfolio_to_itineraries,
    base_value,
    candidates_from_itineraries,
    cheapness_value,
    select_portfolio,
)


def _catalog():
    return {d.id: d for d in acquisition_catalog()}


def _cfg(**kw) -> SearchIntelConfig:
    return SearchIntelConfig(**kw)


# ======================================================================
# Cheapness diminishing returns (§D1)
# ======================================================================
def test_cheapness_value_monotonically_decreasing_in_price():
    v_cheap = cheapness_value(5.0, floor=0.0, ceiling=50.0, gamma=1.6)
    v_mid = cheapness_value(25.0, floor=0.0, ceiling=50.0, gamma=1.6)
    v_expensive = cheapness_value(45.0, floor=0.0, ceiling=50.0, gamma=1.6)
    assert v_cheap > v_mid > v_expensive


def test_cheapness_value_bounded_0_1():
    for price in (-10, 0, 25, 50, 1000):
        v = cheapness_value(price, floor=0.0, ceiling=50.0, gamma=1.6)
        assert 0.0 <= v <= 1.0


def test_cheapness_gap_is_larger_at_higher_absolute_price_than_lower_for_same_ratio():
    """The core §D1 property: a EUR30-vs-EUR15 gap (2x ratio) must swing value
    more than a EUR10-vs-EUR5 gap (same 2x ratio) — a EUR5 saving among
    already-cheap options should not by itself equal a EUR15 saving among
    pricier ones."""
    floor, ceiling, gamma = 0.0, 50.0, 1.6
    gap_high = cheapness_value(15, floor=floor, ceiling=ceiling, gamma=gamma) - \
        cheapness_value(30, floor=floor, ceiling=ceiling, gamma=gamma)
    gap_low = cheapness_value(5, floor=floor, ceiling=ceiling, gamma=gamma) - \
        cheapness_value(10, floor=floor, ceiling=ceiling, gamma=gamma)
    assert gap_high > gap_low


def test_cheapness_curve_gamma_one_is_plain_linear():
    v = cheapness_value(25.0, floor=0.0, ceiling=50.0, gamma=1.0)
    assert v == pytest.approx(0.5, abs=1e-9)


def test_cheapness_value_no_price_variation_is_neutral():
    """When every candidate is the same price, price carries no information —
    must not arbitrarily favour or penalise anyone."""
    assert cheapness_value(20.0, floor=20.0, ceiling=20.0, gamma=1.6) == 1.0


def test_cheapness_gamma_must_never_invert_the_guard():
    cfg = SearchIntelConfig.from_env.__wrapped__ if False else None  # not used
    # gamma < 1 is not exposed via config (clamped to >= 1.0); verify the
    # function itself defends the floor too.
    v = cheapness_value(10.0, floor=0.0, ceiling=50.0, gamma=0.5)
    # Even given an out-of-policy gamma, the function should not crash and
    # should still be monotone: expensive costs less value than cheap.
    v2 = cheapness_value(40.0, floor=0.0, ceiling=50.0, gamma=0.5)
    assert v > v2


# ======================================================================
# Base value combiner
# ======================================================================
def test_base_value_unknown_components_are_neutral_not_penalised():
    cfg = _cfg()
    c_known = PortfolioCandidate(
        destination_id="A", price_per_person=20.0, market_opportunity=0.9,
        attractiveness_score=90.0, user_fit=0.9, trip_quality=0.9, novelty=0.9,
    )
    c_unknown = PortfolioCandidate(destination_id="B", price_per_person=20.0)
    bv_known, _ = base_value(c_known, price_floor=10.0, price_ceiling=30.0, cfg=cfg)
    bv_unknown, _ = base_value(c_unknown, price_floor=10.0, price_ceiling=30.0, cfg=cfg)
    assert bv_known > bv_unknown  # every unknown defaults to neutral 0.5, not a boost
    assert 0.0 <= bv_unknown <= 1.0


def test_extreme_overpriced_candidate_loses_when_opportunity_is_realistic():
    """Regression test for a real finding by independent adversarial QA: a
    candidate priced 5-10x the field ceiling, with maxed attractiveness and
    user_fit, previously won against a realistic mid-priced/mid-quality
    rival whenever the other components (market_opportunity, trip_quality,
    novelty) were left at their neutral "unknown" default — which is exactly
    what the shipped `candidates_from_itineraries` adapter always did.
    Fixed two ways together: (1) value weights rebalanced so price +
    market_opportunity together outweigh attractiveness + user_fit, and (2)
    the adapter now threads a real per-search market_opportunity through
    instead of always leaving it unknown. This test proves the fix holds
    once a realistic (low) opportunity score is supplied for the genuinely
    bad deal — the honest, intended production shape."""
    cfg = _cfg()
    floor, ceiling = 10.0, 40.0
    for ratio in (1.0, 5.0, 10.0):
        outlier = PortfolioCandidate(
            destination_id="outlier", price_per_person=ceiling * ratio,
            market_opportunity=0.15, attractiveness_score=100.0, user_fit=0.95, novelty=0.3,
        )
        mid = PortfolioCandidate(
            destination_id="mid", price_per_person=25.0, market_opportunity=0.6,
            attractiveness_score=60.0, user_fit=0.6, novelty=0.5,
        )
        ov, _ = base_value(outlier, price_floor=floor, price_ceiling=ceiling, cfg=cfg)
        mv, _ = base_value(mid, price_floor=floor, price_ceiling=ceiling, cfg=cfg)
        assert mv > ov, f"ratio={ratio}: mid={mv} should beat outlier={ov}"


def test_value_weights_favor_economic_signals_over_niceness_signals():
    """§D1's core rebalancing: price + market_opportunity together must meet
    or exceed attractiveness + user_fit together, so cheapness/value signals
    are not structurally a minority vote against "this is a nice place"."""
    cfg = _cfg()
    economic = cfg.value_weight_price + cfg.value_weight_opportunity
    niceness = cfg.value_weight_attractiveness + cfg.value_weight_user_fit
    assert economic >= niceness


def test_base_value_components_are_preserved_for_explainability():
    cfg = _cfg()
    c = PortfolioCandidate(destination_id="A", price_per_person=15.0, attractiveness_score=80.0)
    _, comps = base_value(c, price_floor=5.0, price_ceiling=40.0, cfg=cfg)
    assert set(comps) == {
        "price", "attractiveness", "user_fit", "market_opportunity",
        "trip_quality", "novelty",
    }


# ======================================================================
# The Cologne problem — the core portfolio property
# ======================================================================
def _cologne_fixture():
    """A destination's ``market_opportunity`` is *not* interchangeable with
    its price or its attractiveness — it is a separate signal answering "is
    this price a good deal for what this market normally costs" (Phase 1/2).
    Group A's opportunity is high (genuinely good local deals); Group C's is
    deliberately low (an expensive price that is *also* a poor deal, not
    merely "not the cheapest") — exactly what a real acquisition-stage
    Opportunity score would report for a market like that, and exactly why
    this fixture is a fair adversarial test rather than one rigged purely on
    attractiveness."""
    by_id = _catalog()
    group_a = {  # cheap, nearby, similar, genuinely good local deals
        "Dusseldorf": 6, "Dortmund": 8, "Antwerp": 10, "Brussels": 9,
        "Maastricht": 7, "Groningen": 12, "Munster": 11, "Charleroi": 13,
    }
    group_b = {  # distinctive, pricier, fair value for what they are
        "Barcelona": 34, "Prague": 28, "Vienna": 32, "Copenhagen": 38,
    }
    group_c = {  # expensive AND a poor deal
        "Reykjavik": 55, "Tromso": 60,
    }
    opportunity = {**{k: 0.75 for k in group_a}, **{k: 0.60 for k in group_b},
                   **{k: 0.25 for k in group_c}}
    from detoura.services.attractiveness_model import CatalogAttractivenessContext, derive_profile
    ctx = CatalogAttractivenessContext.build(list(by_id.values()))
    candidates = []
    for name, price in {**group_a, **group_b, **group_c}.items():
        profile = derive_profile(by_id[name], ctx=ctx)
        candidates.append(PortfolioCandidate(
            destination_id=name, price_per_person=float(price),
            market_opportunity=opportunity[name], attractiveness_score=profile.aggregate_score,
            user_fit=0.5, novelty=0.5,
        ))
    return candidates, by_id, group_a, group_b, group_c


def test_cologne_top10_is_not_all_cheap_nearby_cities():
    candidates, by_id, group_a, group_b, group_c = _cologne_fixture()
    result = select_portfolio(candidates, by_id, size=10, cfg=_cfg())
    selected_ids = {d.destination_id for d in result.selected}
    # not a total collapse into Group A
    assert not group_a.keys() <= selected_ids
    # at least some Group B distinctive destinations must appear
    assert len(selected_ids & group_b.keys()) >= 2
    # the genuinely poor-value Group C must not appear
    assert not (selected_ids & group_c.keys())


def test_cologne_diversification_improves_geographic_concentration_vs_naive_price_sort():
    candidates, by_id, *_ = _cologne_fixture()
    cfg = _cfg()
    naive_order = sorted(candidates, key=lambda c: c.price_per_person)[:10]
    naive_destinations = [by_id[c.destination_id] for c in naive_order]
    from detoura.services.geo import mean_pairwise_distance_km
    naive_distance = mean_pairwise_distance_km(naive_destinations)

    result = select_portfolio(candidates, by_id, size=10, cfg=cfg)
    assert result.metrics["mean_pairwise_distance_km"] > naive_distance


def test_cologne_diversification_improves_mean_attractiveness_vs_naive_price_sort():
    candidates, by_id, *_ = _cologne_fixture()
    cfg = _cfg()
    naive_order = sorted(candidates, key=lambda c: c.price_per_person)[:10]
    naive_mean_attract = sum(c.attractiveness_score for c in naive_order) / len(naive_order)

    result = select_portfolio(candidates, by_id, size=10, cfg=cfg)
    assert result.metrics["mean_attractiveness"] > naive_mean_attract


def test_nearby_exceptional_destination_still_makes_the_cut():
    """§C5: an exceptional nearby deal must not be excluded purely for being
    close — only excessive *saturation* of near-identical nearby picks is
    penalised."""
    candidates, by_id, group_a, *_ = _cologne_fixture()
    result = select_portfolio(candidates, by_id, size=10, cfg=_cfg())
    selected_ids = {d.destination_id for d in result.selected}
    # the single cheapest, most exceptional nearby deal must survive
    cheapest_nearby = min(group_a, key=lambda k: group_a[k])
    assert cheapest_nearby in selected_ids


def test_diversity_does_not_replace_superior_trips_with_junk():
    """§C6 tradeoff: diversification must never promote a clearly worse
    candidate over a clearly better one just to tick a diversity box."""
    by_id = _catalog()
    cfg = _cfg()
    excellent = PortfolioCandidate(
        destination_id="Barcelona", price_per_person=20.0,
        market_opportunity=0.9, attractiveness_score=95.0, user_fit=0.9,
    )
    junk = PortfolioCandidate(
        destination_id="Reykjavik", price_per_person=20.0,
        market_opportunity=0.1, attractiveness_score=10.0, user_fit=0.1,
    )
    result = select_portfolio([excellent, junk], by_id, size=2, cfg=cfg)
    assert result.selected[0].destination_id == "Barcelona"


def test_country_diversity_is_a_diagnostic_not_a_quota():
    """§C7: a portfolio may legitimately contain multiple destinations from
    the same country when they are each genuinely good — no forced equal
    representation."""
    by_id = _catalog()
    cfg = _cfg()
    candidates = [
        PortfolioCandidate(destination_id="Rome", price_per_person=25.0, attractiveness_score=90.0),
        PortfolioCandidate(destination_id="Florence", price_per_person=28.0, attractiveness_score=88.0),
        PortfolioCandidate(destination_id="Venice", price_per_person=30.0, attractiveness_score=87.0),
    ]
    result = select_portfolio(candidates, by_id, size=3, cfg=cfg)
    assert len(result.selected) == 3  # none forced out purely for same-country


# ======================================================================
# Determinism
# ======================================================================
def test_selection_is_deterministic_across_repeated_runs():
    candidates, by_id, *_ = _cologne_fixture()
    cfg = _cfg()
    r1 = select_portfolio(candidates, by_id, size=10, cfg=cfg)
    r2 = select_portfolio(candidates, by_id, size=10, cfg=cfg)
    assert [d.destination_id for d in r1.selected] == [d.destination_id for d in r2.selected]


def test_empty_candidates_returns_empty_result():
    result = select_portfolio([], {}, size=10, cfg=_cfg())
    assert result.selected == []
    assert result.rejected == []


def test_fewer_candidates_than_size_selects_all():
    by_id = _catalog()
    candidates = [
        PortfolioCandidate(destination_id="Rome", price_per_person=25.0, attractiveness_score=90.0),
        PortfolioCandidate(destination_id="Paris", price_per_person=30.0, attractiveness_score=95.0),
    ]
    result = select_portfolio(candidates, by_id, size=10, cfg=_cfg())
    assert len(result.selected) == 2
    assert result.rejected == []


# ======================================================================
# Itinerary adapter
# ======================================================================
def _itinerary(dest_id: str, *, rank: int, total_cost: float) -> Itinerary:
    return Itinerary(
        rank=rank, score=0.5, total_cost=total_cost, currency="EUR",
        duration_days=3.0, origin_airport="CGN", return_airport="CGN",
        cities=[dest_id], legs=[], total_travel_minutes=180,
        departure=datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc),
        arrival=datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc),
    )


def _req() -> TripRequest:
    from datetime import date
    return TripRequest(
        origin="CGN", budget=2000.0, travelers=1, duration_days=3,
        date_from=date(2026, 10, 12), date_to=date(2026, 10, 14),
    )


def test_candidates_from_itineraries_builds_one_candidate_per_destination():
    by_id = _catalog()
    itineraries = [_itinerary("Barcelona", rank=1, total_cost=34.0), _itinerary("Prague", rank=2, total_cost=28.0)]
    profiles = {}
    candidates = candidates_from_itineraries(
        itineraries, request=_req(), destinations_by_id=by_id, attractiveness_by_id=profiles,
    )
    assert candidates is not None
    assert {c.destination_id for c in candidates} == {"Barcelona", "Prague"}


def test_candidates_from_itineraries_threads_real_opportunity_and_novelty():
    """Regression test: the adapter must use caller-supplied
    market_opportunity/novelty when given, not silently leave them at the
    neutral default (the exact gap independent adversarial QA found)."""
    by_id = _catalog()
    itineraries = [_itinerary("Barcelona", rank=1, total_cost=34.0)]
    candidates = candidates_from_itineraries(
        itineraries, request=_req(), destinations_by_id=by_id, attractiveness_by_id={},
        opportunity_by_dest_id={"Barcelona": 0.72}, novelty_by_dest_id={"Barcelona": 0.4},
    )
    assert candidates is not None
    c = candidates[0]
    assert c.market_opportunity == 0.72
    assert c.novelty == 0.4


def test_candidates_from_itineraries_defaults_to_unknown_without_maps():
    by_id = _catalog()
    itineraries = [_itinerary("Barcelona", rank=1, total_cost=34.0)]
    candidates = candidates_from_itineraries(
        itineraries, request=_req(), destinations_by_id=by_id, attractiveness_by_id={},
    )
    assert candidates[0].market_opportunity is None
    assert candidates[0].novelty is None


def test_candidates_from_itineraries_returns_none_for_multi_city():
    by_id = _catalog()
    multi = Itinerary(
        rank=1, score=0.5, total_cost=100.0, currency="EUR", duration_days=5.0,
        origin_airport="CGN", return_airport="CGN", cities=["Barcelona", "Madrid"],
        legs=[], total_travel_minutes=300,
        departure=datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc),
        arrival=datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc),
    )
    result = candidates_from_itineraries(
        [multi], request=_req(), destinations_by_id=by_id, attractiveness_by_id={},
    )
    assert result is None


def test_candidates_from_itineraries_returns_none_for_unrecognized_destination():
    by_id = _catalog()
    unknown_city = _itinerary("__not_in_catalog__", rank=1, total_cost=10.0)
    result = candidates_from_itineraries(
        [unknown_city], request=_req(), destinations_by_id=by_id, attractiveness_by_id={},
    )
    assert result is None


def test_selection_deterministic_across_python_process_hash_seeds():
    """Regression test for a real nondeterminism bug found by independent
    adversarial QA: candidates tied on `base_value` and the default
    `pre_rank=0` (exactly this fixture's shape) previously resolved their
    tie via `set` iteration order, which depends on `PYTHONHASHSEED` — a
    different OS process could select a genuinely different destination from
    byte-identical input. Runs the exact selection in a fresh subprocess
    under several explicit hash seeds and asserts the selected destination
    ids are identical every time."""
    import json
    import subprocess
    import sys

    script = """
import sys, json
sys.path.insert(0, "src")
from detoura.data.destinations import acquisition_catalog
from detoura.services.portfolio import PortfolioCandidate, select_portfolio
by_id = {d.id: d for d in acquisition_catalog()}
names = ["Dusseldorf", "Dortmund", "Antwerp", "Brussels", "Maastricht",
         "Groningen", "Munster", "Charleroi", "Barcelona", "Prague"]
candidates = [
    PortfolioCandidate(destination_id=n, price_per_person=20.0,
                       market_opportunity=0.5, attractiveness_score=50.0,
                       user_fit=0.5, novelty=0.5)
    for n in names
]
result = select_portfolio(candidates, by_id, size=5)
print(json.dumps([d.destination_id for d in result.selected]))
"""
    seeds_results = []
    for seed in ("0", "1", "3", "4", "42"):
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parents[1]),
            env={"PYTHONHASHSEED": seed, "PATH": __import__("os").environ.get("PATH", "")},
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        seeds_results.append(json.loads(proc.stdout.strip()))
    assert all(r == seeds_results[0] for r in seeds_results), seeds_results


def test_apply_portfolio_to_itineraries_reorders_and_renumbers_rank():
    itineraries = [
        _itinerary("Dusseldorf", rank=1, total_cost=6.0),
        _itinerary("Barcelona", rank=2, total_cost=34.0),
    ]
    by_id = _catalog()
    candidates, *_ = _cologne_fixture()
    # Just use the two matching candidates for a focused, deterministic check.
    two = [c for c in candidates if c.destination_id in ("Dusseldorf", "Barcelona")]
    result = select_portfolio(two, by_id, size=2, cfg=_cfg())
    reordered = apply_portfolio_to_itineraries(itineraries, result)
    assert [it.cities[0] for it in reordered] == [d.destination_id for d in result.selected]
    assert [it.rank for it in reordered] == list(range(1, len(reordered) + 1))
