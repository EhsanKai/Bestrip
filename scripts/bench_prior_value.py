"""V9 Phase 2 — Benchmark 2: cold vs prior vs live discovery (§38).

A synthetic world with **known hidden opportunities**: three destinations
placed deep in a 60-city catalog slice, with mediocre preference affinity (so a
pure preference/richness ranking would not surface them) but a genuinely
attractive market — cheap, frequent, direct. Three otherwise-identical runs,
same :class:`ProviderCallBudget`, same request, same slot count:

* **cold**   — no Market Prior, no Price Memory. Only preference/richness and
  the EXPLORE floor can find the hidden gems.
* **prior**  — a Bootstrap Market Prior says the hidden gems are attractive;
  no Detoura live history yet.
* **live**   — Detoura has actual live Price Memory (with a history of
  contributing to Top-K/winner results) for the hidden gems; no prior.

Reports, per scenario: candidates selected, whether each hidden gem was
recovered into the shortlist, exploit/explore split, and exploration coverage
— all under the identical hard provider-call budget (never "more calls" — only
"better calls").

Run::

    python3 scripts/bench_prior_value.py
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.data.destinations import DESTINATIONS  # noqa: E402
from detoura.models.search_intel import PriceObservation, SearchModeTag, TripShape  # noqa: E402
from detoura.models.trip import TravelPreferences, TripRequest  # noqa: E402
from detoura.persistence.db import Database  # noqa: E402
from detoura.persistence import price_memory as pm  # noqa: E402
from detoura.services.candidate_funnel import run_funnel  # noqa: E402
from detoura.services.market_prior_import import run_import  # noqa: E402
from detoura.services.market_prior_source import FixtureMarketPriorSource  # noqa: E402

ORIGIN = "CGN"
SLOTS = 8
CATALOG = list(DESTINATIONS[:60])
# Deep in the slice, on purpose - not the alphabetically- or richness-first ones.
HIDDEN_IDS = [d.id for d in CATALOG[-3:] if d.primary_airport]


def _req() -> TripRequest:
    # A preference profile the hidden gems do not particularly match - only
    # their market attractiveness can pull them into the shortlist.
    return TripRequest(
        origin="Köln", budget=3000.0, travelers=2, duration_days=4,
        date_from=date(2026, 10, 12), date_to=date(2026, 10, 26),
        preferences=TravelPreferences(shopping=0.9, museums=0.9),
    )


def _hidden_markets() -> list[tuple[str, str]]:
    by_id = {d.id: d for d in CATALOG}
    return [(ORIGIN, by_id[i].primary_airport) for i in HIDDEN_IDS]


def _seed_prior(db: Database) -> None:
    # everyone (except the hidden gems) gets a mid-priced, unremarkable prior -
    # the hidden gems get ONLY the cheap/confident record below, so their
    # signal is never diluted by an earlier mediocre import on the same bucket.
    hidden_markets = set(_hidden_markets())
    markets = [
        (ORIGIN, d.primary_airport) for d in CATALOG
        if d.primary_airport and (ORIGIN, d.primary_airport) not in hidden_markets
    ]
    run_import(db, FixtureMarketPriorSource(markets=markets, source_date=date(2026, 5, 1)))

    class _Hidden:
        def meta(self):
            from detoura.services.market_prior_source import SourceMeta
            return SourceMeta(source="fixture", source_version="hidden-gem")

        def records(self):
            # >= prior_min_rows_for_band buckets, so the relative-price band
            # is actually credited rather than suppressed as too thin.
            for o, a in _hidden_markets():
                for hd in (14, 30):
                    yield {
                        "origin_airport": o, "destination_airport": a,
                        "horizon_days": hd, "currency": "EUR",
                        "sample_count": 40, "median_minor": 4500,
                        "observed_low_minor": 3800, "observed_high_minor": 6000,
                        "direct_possible": True, "weekly_frequency": 21,
                        "carrier_count": 3, "confidence": "HIGH",
                        "source_date": "2026-05-01",
                    }
    run_import(db, _Hidden())


def _seed_live(db: Database) -> None:
    now = datetime.now(timezone.utc)
    obs = []
    for i, (o, a) in enumerate(_hidden_markets()):
        for j in range(6):
            obs.append(PriceObservation(
                observation_id=pm.new_observation_id(),
                observed_at=now - timedelta(hours=j),
                provider="duffel", origin=o, destination=a,
                departure_date=date(2026, 10, 14), trip_shape=TripShape.ONE_WAY,
                travelers=2, travelers_bucket="2",
                total_amount_minor=9000, per_person_minor=4500, currency="EUR",
                direct=True, stops=0, search_id=f"seed{i}",
                acquisition_call_id=f"seed{i}:{j}", search_mode=SearchModeTag.SMART,
                entered_candidate_set=True, contributed_to_top_k=True,
                contributed_to_winner=(j == 0), provenance_version=2,
            ))
    pm.record_observations(db, obs)


def _run(db: Database) -> dict:
    result = run_funnel(
        db, _req(), CATALOG, origin_airports=[ORIGIN],
        departure_date=date(2026, 10, 14), slots=SLOTS,
    )
    chosen_ids = {d.id for d in result.chosen}
    recovered = [i for i in HIDDEN_IDS if i in chosen_ids]
    return {
        "catalog_total": result.trace.catalog_total,
        "shortlisted_total": result.trace.shortlisted_total,
        "exploit_candidates": result.trace.exploit_candidates,
        "explore_candidates": result.trace.explore_candidates,
        "hidden_gems": HIDDEN_IDS,
        "hidden_gems_recovered": recovered,
        "hidden_recovery_rate": round(len(recovered) / len(HIDDEN_IDS), 4) if HIDDEN_IDS else None,
        "chosen": sorted(chosen_ids),
    }


def main() -> None:
    cold_db = Database(":memory:")
    prior_db = Database(":memory:")
    _seed_prior(prior_db)
    live_db = Database(":memory:")
    _seed_live(live_db)

    report = {
        "budget_slots": SLOTS,
        "catalog_size": len(CATALOG),
        "hidden_gems": HIDDEN_IDS,
        "cold": _run(cold_db),
        "prior": _run(prior_db),
        "live": _run(live_db),
        "note": "Identical slots/budget in all three runs - prior/live change "
                "WHICH markets are chosen, never HOW MANY provider calls a "
                "downstream acquisition may make.",
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
