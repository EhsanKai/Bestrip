"""V9 Phase 2 — Bootstrap Market Prior: data boundaries, import, opportunity
scoring (§32, §33, §34, §35)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from detoura.models.market_prior import (
    BootstrapMarketPrior,
    HistoricalMarketPriorSignal,
    HorizonBucket,
    MarketDataSource,
    PriorConfidence,
    SeasonBucket,
)
from detoura.models.search_intel import (
    HistoricalPriceSignal,
    MarketConfidence,
    MarketKey,
    ConfidenceBreakdown,
)
from detoura.persistence import market_priors as mp
from detoura.persistence.db import Database
from detoura.search_intel_config import SearchIntelConfig
from detoura.services.market_prior_import import PriorImportError, normalize, run_import
from detoura.services.market_prior_signal import batch_prior_signals, primary_prior_signal
from detoura.services.market_prior_source import (
    FixtureMarketPriorSource,
    JsonMarketPriorSource,
    SourceMeta,
)
from detoura.services.opportunity import (
    OpportunityInput,
    score_opportunities,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _db() -> Database:
    return Database(":memory:")


def _prior(**kw) -> BootstrapMarketPrior:
    base = dict(
        prior_id=mp.new_prior_id(), source="fixture", source_version="v1",
        imported_at=NOW, source_date=date(2026, 5, 1),
        origin_airport="LHR", destination_airport="BCN",
        season=SeasonBucket.SHOULDER, month=5, horizon_bucket=HorizonBucket.H30,
        currency="EUR", sample_count=30, observed_low_minor=5000,
        median_minor=8000, observed_high_minor=15000,
        confidence=PriorConfidence.MEDIUM,
    )
    base.update(kw)
    return BootstrapMarketPrior(**base)


# ======================================================================
# §32 — data boundaries
# ======================================================================
def test_market_prior_persists_independently_from_price_observations():
    d = _db()
    mp.upsert_priors(d, [_prior()])
    # market_priors has a row; price_observations does not
    assert d.query_one("SELECT COUNT(*) n FROM market_priors")["n"] == 1
    assert d.query_one("SELECT COUNT(*) n FROM price_observations")["n"] == 0


def test_prior_has_no_bookable_amount_accessor():
    p = _prior()
    for attr in ("as_quote", "current_price", "bookable_amount", "amount", "to_fare"):
        assert not hasattr(p, attr)
    sig = HistoricalMarketPriorSignal(
        origin_airport="LHR", destination_airport="BCN", currency="EUR",
        prior_available=True,
    )
    assert sig.not_a_quote is True
    for attr in ("as_quote", "current_price", "bookable_amount"):
        assert not hasattr(sig, attr)


def test_booking_revalidation_commercial_do_not_import_market_prior():
    import detoura.services.revalidation as rv
    import detoura.services.booking_commercial as bc
    import detoura.services.booking_flow as bf
    import detoura.services.recheck as rc
    import detoura.services.guided_booking as gb
    src = "".join(open(m.__file__).read() for m in (rv, bc, bf, rc, gb))
    for bad in ("market_prior", "market_priors", "BootstrapMarketPrior",
                "opportunity", "candidate_funnel"):
        assert bad not in src


def test_price_memory_and_market_prior_are_distinct_repositories():
    import detoura.persistence.price_memory as pm
    import detoura.persistence.market_priors as mpr
    pm_src, mpr_src = open(pm.__file__).read(), open(mpr.__file__).read()
    # Price Memory never touches the prior table, and vice versa — no shared SQL.
    assert "market_prior" not in pm_src
    for stmt in ("FROM price_observations", "INTO price_observations",
                 "UPDATE price_observations", "FROM search_traces"):
        assert stmt not in mpr_src


def test_currencies_are_not_merged_in_prior_signal():
    d = _db()
    mp.upsert_priors(d, [
        _prior(prior_id=mp.new_prior_id(), currency="EUR", median_minor=8000,
               horizon_bucket=HorizonBucket.H30),
        _prior(prior_id=mp.new_prior_id(), currency="USD", median_minor=20000,
               horizon_bucket=HorizonBucket.H30),
        _prior(prior_id=mp.new_prior_id(), currency="EUR", median_minor=8500,
               horizon_bucket=HorizonBucket.H60),
    ])
    sigs = batch_prior_signals(d, origin_airports=["LHR"],
                               destination_airports=["BCN"], now=NOW)["BCN"]
    assert set(sigs) == {"EUR", "USD"}
    assert sigs["EUR"].row_count == 2 and sigs["USD"].row_count == 1


def test_unknown_prior_values_stay_unknown():
    d = _db()
    mp.upsert_priors(d, [_prior(
        prior_id=mp.new_prior_id(), sample_count=None, observed_low_minor=None,
        median_minor=None, observed_high_minor=None, weekly_frequency=None,
    )])
    sig = primary_prior_signal(batch_prior_signals(
        d, origin_airports=["LHR"], destination_airports=["BCN"], now=NOW)["BCN"])
    assert sig.typical_estimate_minor is None  # never 0
    assert sig.direct_supply_signal is None


def test_no_pii_columns_in_market_prior_tables():
    d = _db()
    for tbl in ("market_priors", "market_prior_imports"):
        cols = {r["name"] for r in d.query(f"PRAGMA table_info({tbl})")}
        for bad in ("name", "email", "phone", "passport", "born", "traveler",
                    "given_name", "family_name", "payment"):
            assert not any(bad in c for c in cols), (tbl, bad)


def test_prior_prune_only_touches_priors():
    d = _db()
    with d.write() as c:
        c.execute("INSERT INTO booking_economics (booking_id, journey_reference,"
                  " created_at, currency, service_tier, markup_policy_id,"
                  " markup_policy_version, supplier_transport_minor,"
                  " supplier_baggage_minor, supplier_fees_minor, service_fee_minor,"
                  " markup_minor, discount_minor, tax_minor, customer_price_minor,"
                  " breakdown_json, snapshot_json) VALUES"
                  " ('bk','J',?, 'EUR','BASIC','p',1,0,0,0,0,0,0,0,0,'{}','{}')",
                  (NOW.isoformat(),))
        c.execute("INSERT INTO audit_events (ts, actor, action) VALUES (?,?,?)",
                  (NOW.isoformat(), "ops", "T"))
    mp.upsert_priors(d, [
        _prior(prior_id=mp.new_prior_id(), source_date=date(2024, 1, 1)),   # stale
        _prior(prior_id=mp.new_prior_id(), horizon_bucket=HorizonBucket.H60,
               source_date=date(2026, 5, 20)),                              # fresh
    ])
    deleted = mp.prune(d, retention_days=365, now=NOW)
    assert deleted == 1
    assert d.query_one("SELECT COUNT(*) n FROM market_priors")["n"] == 1
    assert d.query_one("SELECT COUNT(*) n FROM booking_economics")["n"] == 1
    assert d.query_one("SELECT COUNT(*) n FROM audit_events")["n"] == 1


# ======================================================================
# §33 — import
# ======================================================================
def test_import_is_idempotent():
    d = _db()
    src = FixtureMarketPriorSource(markets=(("LHR", "BCN"), ("LHR", "MAD")),
                                   source_date=date(2026, 5, 1))
    r1 = run_import(d, src)
    n1 = mp.coverage_summary(d)["rows"]
    r2 = run_import(d, src)
    assert mp.coverage_summary(d)["rows"] == n1  # no doubling
    assert r2["rows_imported"] == 0 and r2["rows_updated"] == n1


def test_import_rejects_bad_rows_without_aborting():
    d = _db()

    class Src:
        def meta(self): return SourceMeta(source="s")
        def records(self):
            yield {"origin_airport": "LHR", "destination_airport": "XX", "currency": "EUR"}
            yield {"origin_airport": "LHR", "destination_airport": "BCN", "currency": "NOPE"}
            yield {"origin_airport": "LHR", "destination_airport": "LHR", "currency": "EUR"}  # o==d
            yield {"origin_airport": "LHR", "destination_airport": "BCN",
                   "currency": "EUR", "median": 90.0, "horizon_days": 30}
            yield {"origin_airport": "LHR", "destination_airport": "BCN",
                   "currency": "EUR", "observed_low": 200, "observed_high": 50,
                   "horizon_days": 45}  # low > high

    r = run_import(d, Src())
    assert r["rows_seen"] == 5
    assert r["rows_imported"] == 1        # only the well-formed row
    assert r["rows_rejected"] == 4        # bad IATA, bad ccy, o==d, low>high
    assert len(r["rejected"]) == 4
    assert mp.coverage_summary(d)["rows"] == 1


def test_import_drops_unknown_keys_no_pii():
    p = normalize(
        {"origin_airport": "LHR", "destination_airport": "BCN", "currency": "EUR",
         "median": 88.0, "horizon_days": 30, "passenger_name": "Ada Lovelace",
         "email": "ada@example.com"},
        source="s", source_version="", imported_at=NOW, default_source_date=None,
    )
    dumped = p.model_dump()
    assert "passenger_name" not in dumped and "email" not in dumped
    assert p.median_minor == 8800


def test_import_bad_currency_and_dates_rejected():
    for bad in (
        {"origin_airport": "LHR", "destination_airport": "BCN", "currency": "EU"},
        {"origin_airport": "LHR", "destination_airport": "BCN", "currency": "EUR", "month": 13},
        {"origin_airport": "LHR", "destination_airport": "BCN", "currency": "EUR",
         "source_date": "not-a-date"},
    ):
        with pytest.raises(PriorImportError):
            normalize(bad, source="s", source_version="", imported_at=NOW,
                      default_source_date=None)


def test_import_run_recorded_with_provenance_and_metrics():
    d = _db()
    run_import(d, FixtureMarketPriorSource(markets=(("LHR", "BCN"),),
                                           version="feed-2026-05", source_date=date(2026, 5, 1)))
    imp = mp.recent_imports(d)[0]
    assert imp["source"] == "fixture" and imp["source_version"] == "feed-2026-05"
    assert imp["rows_imported"] == 6 and imp["ok"] == 1
    assert imp["source_requests"] == 1  # fixture reports it
    assert imp["source_request_cost_minor"] is None  # UNKNOWN, not 0


def test_import_dry_run_writes_nothing():
    d = _db()
    r = run_import(d, FixtureMarketPriorSource(markets=(("LHR", "BCN"),)), dry_run=True)
    assert r["rows_imported"] == 6  # would-be
    assert mp.coverage_summary(d)["rows"] == 0  # actually persisted nothing


def test_import_from_json_file(tmp_path):
    doc = {
        "source": "operator-approved", "source_version": "2026-Q2",
        "source_date": "2026-04-01",
        "records": [
            {"origin_airport": "CDG", "destination_airport": "FCO", "currency": "EUR",
             "median": 120.0, "horizon_days": 45, "sample_count": 50,
             "direct_possible": True, "weekly_frequency": 40},
        ],
    }
    p = tmp_path / "prior.json"
    p.write_text(json.dumps(doc))
    d = _db()
    r = run_import(d, JsonMarketPriorSource(str(p)))
    assert r["rows_imported"] == 1
    row = d.query("SELECT * FROM market_priors")[0]
    assert row["source"] == "operator-approved" and row["median_minor"] == 12000
    assert row["source_date"] == "2026-04-01"


def test_source_exception_does_not_corrupt_existing_data():
    d = _db()
    mp.upsert_priors(d, [_prior()])

    class Boom:
        def meta(self): return SourceMeta(source="boom")
        def records(self):
            yield {"origin_airport": "LHR", "destination_airport": "MAD",
                   "currency": "EUR", "median": 90.0, "horizon_days": 30}
            raise RuntimeError("feed died mid-stream")

    r = run_import(d, Boom())
    assert r["ok"] is False and "RuntimeError" in r["error"]
    # a source-level exception aborts before persistence: the pre-existing row
    # is untouched and the half-streamed MAD row was never written.
    assert d.query_one("SELECT COUNT(*) n FROM market_priors WHERE destination_airport='BCN'")["n"] == 1
    assert d.query_one("SELECT COUNT(*) n FROM market_priors WHERE destination_airport='MAD'")["n"] == 0
    imp = mp.recent_imports(d)[0]
    assert imp["ok"] == 0


# ======================================================================
# §34 / §35 — opportunity scoring scenarios
# ======================================================================
def _live(median_minor, n, verdict=MarketConfidence.HIGH, recency=0.9):
    mk = MarketKey.build(provider="duffel", origin="LHR", destination="BCN",
                         departure_date=date(2026, 6, 1))
    cb = ConfidenceBreakdown(sample_component=0.8, recency_component=recency,
                             consistency_component=0.9, score=0.8, verdict=verdict)
    return HistoricalPriceSignal(market=mk, currency="EUR", sample_count=n,
                                 median_observed_minor=median_minor, confidence=cb)


def _psig(dst, typ, rel, conf, supply, rows=6, fresh=0.85):
    return HistoricalMarketPriorSignal(
        origin_airport="X", destination_airport=dst, currency="EUR",
        prior_available=True, row_count=rows, typical_estimate_minor=typ,
        relative_price_attractiveness=rel, confidence=conf,
        confidence_components={"freshness": fresh}, direct_supply_signal=supply,
    )


def test_A_cheap_but_rare_does_not_beat_dearer_but_useful():
    cheap_rare = OpportunityInput(
        "→cheap", 0, None, _psig("A", 3500, 0.95, PriorConfidence.MEDIUM, 0.15, rows=2),
        preference_affinity=0.3,
    )
    dearer_good = OpportunityInput(
        "→good", 1, None, _psig("B", 5500, 0.55, PriorConfidence.HIGH, 0.9),
        preference_affinity=0.85, useful_rate=0.7, top_k_rate=0.4, winner_rate=0.2,
    )
    by = {s.market: s for s in score_opportunities([cheap_rare, dearer_good])}
    assert by["→good"].score > by["→cheap"].score


def test_B_fresh_live_dominates_stale_prior():
    fresh_live = OpportunityInput(
        "→live", 0, _live(9000, 12, recency=0.95),
        _psig("X", 4000, 0.9, PriorConfidence.HIGH, 0.9), preference_affinity=0.5,
    )
    stale_prior = OpportunityInput(
        "→prior", 1, None, _psig("Y", 4000, 0.9, PriorConfidence.LOW, 0.9, fresh=0.15),
        preference_affinity=0.5,
    )
    by = {s.market: s for s in score_opportunities([fresh_live, stale_prior])}
    assert by["→live"].knowledge == "LIVE"
    assert by["→live"].components["price_basis"] in ("live", "live+prior")
    assert by["→prior"].knowledge == "PRIOR"
    # the fresh live market outranks the stale low-confidence prior market
    assert by["→live"].score > by["→prior"].score


def test_C_prior_known_no_live_can_enter_exploit():
    only_prior = OpportunityInput(
        "→p", 0, None, _psig("P", 6000, 0.7, PriorConfidence.HIGH, 0.8),
        preference_affinity=0.6,
    )
    s = score_opportunities([only_prior])[0]
    assert s.knowledge == "PRIOR"
    assert s.stance.value == "EXPLOIT"


def test_D_no_live_no_prior_still_explores():
    unknown = OpportunityInput("→u", 0, None, None, preference_affinity=0.4)
    s = score_opportunities([unknown])[0]
    assert s.knowledge == "UNKNOWN"
    assert s.stance.value == "EXPLORE"
    assert 0.0 <= s.score <= 1.0


def _two_priors(d, source_date):
    mp.upsert_priors(d, [
        _prior(prior_id=mp.new_prior_id(), source_date=source_date,
               confidence=PriorConfidence.HIGH),
        _prior(prior_id=mp.new_prior_id(), horizon_bucket=HorizonBucket.H60,
               source_date=source_date, confidence=PriorConfidence.HIGH),
    ])
    return primary_prior_signal(batch_prior_signals(
        d, origin_airports=["LHR"], destination_airports=["BCN"], now=NOW)["BCN"])


def test_E_very_stale_prior_confidence_decays_below_fresh():
    fresh = _two_priors(_db(), date(2026, 5, 20))    # ~12 days old
    stale = _two_priors(_db(), date(2025, 6, 1))     # ~1 year old
    assert fresh.confidence_components["freshness"] > 0.85
    assert stale.confidence_components["freshness"] < 0.2
    # identical source confidence + row count, only age differs → verdict drops
    assert fresh.confidence == PriorConfidence.HIGH
    assert stale.confidence in (PriorConfidence.LOW, PriorConfidence.MEDIUM)
    order = [PriorConfidence.NONE, PriorConfidence.LOW, PriorConfidence.MEDIUM,
             PriorConfidence.HIGH]
    assert order.index(stale.confidence) < order.index(fresh.confidence)


def test_F_single_anomalous_cheap_prior_row_does_not_monopolise():
    d = _db()
    # one very cheap row, plus normal ones on other buckets
    mp.upsert_priors(d, [
        _prior(prior_id=mp.new_prior_id(), horizon_bucket=HorizonBucket.H14,
               median_minor=1500, typical_minor=1500),
        _prior(prior_id=mp.new_prior_id(), horizon_bucket=HorizonBucket.H30, median_minor=8000),
        _prior(prior_id=mp.new_prior_id(), horizon_bucket=HorizonBucket.H60, median_minor=8500),
        _prior(prior_id=mp.new_prior_id(), horizon_bucket=HorizonBucket.H90, median_minor=9000),
    ])
    sig = primary_prior_signal(batch_prior_signals(
        d, origin_airports=["LHR"], destination_airports=["BCN"], now=NOW)["BCN"])
    # median of typicals absorbs the outlier — the band is not the €15 row
    assert sig.typical_estimate_minor is not None
    assert sig.typical_estimate_minor >= 6000
