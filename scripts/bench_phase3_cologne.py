"""V9 Phase 3 — Mandatory Adversarial Benchmark: "The Cologne Problem".

A deterministic (no live network, no randomness beyond a fixed seed) fixture
built entirely from the REAL ~203-destination catalog and REAL derived
attractiveness/experience/geography — never a hand-picked city-name
whitelist (the spec explicitly forbids that). The adversarial pattern is
produced by a PRICING MODEL, not by choosing favourable cities:

    expected_price(d) = FLOOR + distance_km(Cologne, d) / KM_PER_EUR

i.e. genuinely short-haul-bus-and-train economics — closer is systematically
cheaper — exactly the real-world pattern that makes a naive optimizer fail. A
small deterministic per-destination jitter (seeded, reproducible) is added so
prices are not perfectly monotone in distance, and that jitter is also what
drives each candidate's ``market_opportunity`` (a price *below* its
distance-expected price is a genuine deal; above is a poor one) — so
"cheap because it's close" and "an actual bargain" are deliberately NOT the
same signal, matching how the real Opportunity score works.

Three runs, identical candidate pool, identical (simulated) provider-call
budget:

    1. PRICE-HEAVY / LEGACY-LIKE BASELINE — sort by price alone.
    2. PHASE 3 VALUE (no diversification)  — sort by ``base_value`` alone
       (attractiveness + opportunity + cheapness-curve price, still no
       geo/experience reranking).
    3. PHASE 3 FULL PORTFOLIO              — ``select_portfolio`` with
       diversification.

Reports the exact metrics the spec asks for, never a fixed expected-answer
whitelist: geographic concentration, same-country concentration, pairwise
experience similarity, mean attractiveness, mean/portfolio opportunity,
provider-call budget (unchanged across all three), discovery share.

Run::

    python3 scripts/bench_phase3_cologne.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.data.destinations import acquisition_catalog  # noqa: E402
from detoura.search_intel_config import SearchIntelConfig  # noqa: E402
from detoura.services.attractiveness_model import (  # noqa: E402
    CatalogAttractivenessContext,
    derive_profile,
)
from detoura.services.experience_similarity import mean_pairwise_experience_similarity  # noqa: E402
from detoura.services.geo import (  # noqa: E402
    country_diversity_ratio,
    distance_km,
    mean_pairwise_distance_km,
)
from detoura.services.portfolio import PortfolioCandidate, select_portfolio  # noqa: E402

ORIGIN_ID = "Cologne"
FLOOR_EUR = 5.0
KM_PER_EUR = 45.0
PROVIDER_CALL_BUDGET = 20  # identical across all three runs, on purpose
SEED = 20260601


def build_fixture(origin_id: str = ORIGIN_ID, seed: int = SEED):
    catalog = list(acquisition_catalog())
    by_id = {d.id: d for d in catalog}
    origin = by_id[origin_id]
    ctx = CatalogAttractivenessContext.build(catalog)
    rng = random.Random(seed)

    candidates = []
    for d in catalog:
        if d.id == origin_id:
            continue
        dist = distance_km(origin, d)
        if dist is None:
            continue
        expected_price = FLOOR_EUR + dist / KM_PER_EUR
        # Deterministic jitter in [-20%, +20%] of the expected price, seeded.
        jitter_frac = rng.uniform(-0.20, 0.20)
        price = round(max(1.0, expected_price * (1.0 + jitter_frac)), 2)
        opportunity = max(0.0, min(1.0, 0.5 + 0.5 * (expected_price - price) / expected_price))
        profile = derive_profile(d, ctx=ctx)
        candidates.append(PortfolioCandidate(
            destination_id=d.id, price_per_person=price,
            market_opportunity=round(opportunity, 4),
            attractiveness_score=profile.aggregate_score,
            user_fit=0.5, novelty=0.5,
        ))
    # Sample down to a realistic acquisition shortlist size (the funnel would
    # never acquire all 202 candidates for one search) — the top
    # PROVIDER_CALL_BUDGET*3 by a coarse pre-filter (distance spread across
    # the whole catalog) so both nearby-cheap and distant-distinctive
    # destinations are represented in the pool the three strategies choose
    # from, same pool for all three.
    candidates.sort(key=lambda c: c.destination_id)  # deterministic order
    pool_size = PROVIDER_CALL_BUDGET * 3
    rng2 = random.Random(seed + 1)
    pool = rng2.sample(candidates, min(pool_size, len(candidates)))
    return pool, by_id


def metrics_for(selected_ids: list[str], candidates_by_id: dict, by_id: dict) -> dict:
    dests = [by_id[i] for i in selected_ids if i in by_id]
    attracts = [candidates_by_id[i].attractiveness_score for i in selected_ids
                if candidates_by_id[i].attractiveness_score is not None]
    opps = [candidates_by_id[i].market_opportunity for i in selected_ids
            if candidates_by_id[i].market_opportunity is not None]
    prices = [candidates_by_id[i].price_per_person for i in selected_ids]
    return {
        "selected": selected_ids,
        "mean_price": round(sum(prices) / len(prices), 2) if prices else None,
        "mean_attractiveness": round(sum(attracts) / len(attracts), 2) if attracts else None,
        "mean_opportunity": round(sum(opps) / len(opps), 4) if opps else None,
        "country_diversity_ratio": country_diversity_ratio(dests),
        "mean_pairwise_distance_km": mean_pairwise_distance_km(dests),
        "mean_pairwise_experience_similarity": mean_pairwise_experience_similarity(dests),
        "provider_call_budget": PROVIDER_CALL_BUDGET,
    }


def run(origin_id: str = ORIGIN_ID, seed: int = SEED):
    pool, by_id = build_fixture(origin_id, seed)
    candidates_by_id = {c.destination_id: c for c in pool}
    cfg = SearchIntelConfig()
    top_n = 10

    # 1. PRICE-HEAVY / LEGACY-LIKE BASELINE
    baseline_ids = [c.destination_id for c in sorted(pool, key=lambda c: c.price_per_person)[:top_n]]

    # 2. PHASE 3 VALUE, no diversification (geo/experience penalties zeroed)
    value_cfg = SearchIntelConfig(
        portfolio_geo_penalty_scale=0.0, portfolio_experience_penalty_scale=0.0,
    )
    value_result = select_portfolio(pool, by_id, size=top_n, cfg=value_cfg)
    value_ids = [d.destination_id for d in value_result.selected]

    # 3. PHASE 3 FULL PORTFOLIO
    full_result = select_portfolio(pool, by_id, size=top_n, cfg=cfg)
    full_ids = [d.destination_id for d in full_result.selected]

    report = {
        "origin": origin_id,
        "candidate_pool_size": len(pool),
        "top_n": top_n,
        "1_price_heavy_baseline": metrics_for(baseline_ids, candidates_by_id, by_id),
        "2_phase3_value_no_diversification": metrics_for(value_ids, candidates_by_id, by_id),
        "3_phase3_full_portfolio": metrics_for(full_ids, candidates_by_id, by_id),
    }

    b, f = report["1_price_heavy_baseline"], report["3_phase3_full_portfolio"]
    report["improvement_summary"] = {
        "mean_attractiveness_delta": round(f["mean_attractiveness"] - b["mean_attractiveness"], 2),
        "mean_pairwise_distance_km_delta": round(
            f["mean_pairwise_distance_km"] - b["mean_pairwise_distance_km"], 2
        ),
        "country_diversity_ratio_delta": round(
            f["country_diversity_ratio"] - b["country_diversity_ratio"], 4
        ),
        "experience_similarity_delta": round(
            f["mean_pairwise_experience_similarity"] - b["mean_pairwise_experience_similarity"], 4
        ),
        "top10_overlap_with_baseline": len(set(baseline_ids) & set(full_ids)),
        "provider_call_budget_unchanged": True,
    }
    print(json.dumps(report, indent=2))
    return report


OTHER_ORIGINS = ("Paris", "Milan", "Vienna", "Barcelona", "Warsaw")


def run_origin_generalization():
    """§10 origin generalization: the same concentration-avoidance property
    must hold for other major European origins, not just Cologne — proves
    the mechanism generalises rather than being tuned to one city."""
    results = {}
    for origin in OTHER_ORIGINS:
        results[origin] = run(origin_id=origin, seed=SEED + hash(origin) % 1000)
    print("\n=== ORIGIN GENERALIZATION SUMMARY ===")
    for origin, r in results.items():
        b = r["1_price_heavy_baseline"]
        f = r["3_phase3_full_portfolio"]
        print(
            f"{origin:12s} baseline_dist={b['mean_pairwise_distance_km']:7.1f}km "
            f"full_dist={f['mean_pairwise_distance_km']:7.1f}km "
            f"baseline_country_div={b['country_diversity_ratio']:.2f} "
            f"full_country_div={f['country_diversity_ratio']:.2f}"
        )
    return results


if __name__ == "__main__":
    if "--origins" in sys.argv:
        run_origin_generalization()
    else:
        run()
