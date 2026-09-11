"""V9 Phase 2.5 — controlled end-to-end acquisition demo (§37).

Using the deterministic fixture source (no real external network source is
configured/approved for Phase 2.5 — see docs/V9_MARKET_PRIOR_SOURCE_OPTIONS.md):

    BootstrapJob -> task planner -> fetch -> parse -> validate -> import
                 -> BootstrapMarketPrior -> Opportunity Score -> Candidate Funnel

Demonstrates two things:

1. A market with **no live Price Memory** becomes better-informed once the
   Bootstrap Market Prior is populated for it (PRIOR-known, EXPLOIT).
2. The prior is still never a bookable price: live Duffel acquisition would
   still be required before this market could ever reach checkout — the
   prior signal is stamped ``not_a_quote`` end to end and the market's
   knowledge state is ``PRIOR``, never ``LIVE``.

Run::

    python3 scripts/bootstrap_e2e_demo.py
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.models.market_prior_acquisition import (  # noqa: E402
    AuthorizationStatus, SourceRegistration, SourceType,
)
from detoura.persistence import market_prior_acquisition as store  # noqa: E402
from detoura.persistence import market_priors as prior_store  # noqa: E402
from detoura.persistence.db import Database  # noqa: E402
from detoura.services.bootstrap_executor import run_job_slice  # noqa: E402
from detoura.services.bootstrap_fetchers import FixtureSourceFetcher  # noqa: E402
from detoura.services.bootstrap_planner import plan_cells  # noqa: E402
from detoura.services.market_prior_signal import batch_prior_signals, primary_prior_signal  # noqa: E402
from detoura.services.opportunity import Knowledge, OpportunityInput, score_opportunities  # noqa: E402

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def main() -> None:
    db = Database(":memory:")

    source = SourceRegistration(
        source_id="fixture-europe-demo", source_name="Fixture Europe Demo",
        source_type=SourceType.FILE_IMPORT, authorization_status=AuthorizationStatus.APPROVED,
        authorization_basis="Deterministic in-repo fixture; no external network access.",
        created_at=NOW, updated_at=NOW,
    )
    store.upsert_source(db, source)

    origin, destination, horizon = "LHR", "TIA", 30  # Tirana: a plausible cold market, no live history
    job_id = store.create_job(
        db, source_id=source.source_id, origins=[origin], destinations=[destination],
        horizon_days=[horizon], request_budget=10, dry_run=False, now=NOW,
    )
    cells = plan_cells([origin], [destination], [horizon])
    plan = store.plan_tasks(db, job_id=job_id, source_id=source.source_id, cells=cells, now=NOW)

    fetcher = FixtureSourceFetcher(source_date=date(2026, 5, 20))
    result = run_job_slice(db, job_id, fetcher=fetcher, source=source, max_tasks=10, now=NOW)

    signals = batch_prior_signals(db, origin_airports=[origin], destination_airports=[destination], now=NOW)
    signal = primary_prior_signal(signals.get(destination, {}))

    opp = score_opportunities([OpportunityInput(
        market=f"{origin}->{destination}", pre_rank=0, live=None, prior=signal,
        preference_affinity=0.6,
    )])[0]

    before_after = {
        "before_bootstrap": {"knowledge": "UNKNOWN", "prior_available": False},
        "after_bootstrap": {
            "knowledge": opp.knowledge, "stance": opp.stance.value,
            "prior_available": signal.prior_available if signal else False,
            "prior_confidence": signal.confidence.value if signal else None,
            "not_a_quote": signal.not_a_quote if signal else True,
        },
    }

    print(json.dumps({
        "job_plan": plan,
        "execution_result": result.as_dict(),
        "market_prior_rows": prior_store.coverage_summary(db)["rows"],
        "opportunity_score": opp.score,
        "before_after": before_after,
        "still_requires_live_acquisition_before_booking": (
            opp.knowledge != Knowledge.LIVE.value
            and (signal is None or signal.not_a_quote is True)
        ),
        "conclusion": (
            "The TIA market went from fully UNKNOWN to PRIOR-known/EXPLOIT purely "
            "from the Bootstrap Market Prior - and its signal is still stamped "
            "not_a_quote=true. A live Duffel acquisition (Price Memory) is still "
            "required before this market could ever produce a bookable fare."
        ),
    }, indent=2))


if __name__ == "__main__":
    main()
