"""V9 Phase 1 — Price Memory persistence, aggregation, scoring, separation."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from detoura.models.search_intel import (
    HistoricalPriceSignal,
    MarketConfidence,
    MarketKey,
    PriceObservation,
    SearchModeTag,
    TripShape,
    travelers_bucket,
)
from detoura.persistence import price_memory as pm
from detoura.persistence.db import SCHEMA_VERSION, Database
from detoura.search_intel_config import SearchIntelConfig
from detoura.services.acquisition_scoring import (
    CandidateInput,
    allocate,
    score_candidates,
)
from detoura.services.market_intel import market_signals, primary_signal

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _db() -> Database:
    return Database(":memory:")


def _obs(**kw) -> PriceObservation:
    base = dict(
        observation_id=pm.new_observation_id(),
        observed_at=NOW,
        provider="duffel", origin="CGN", destination="BCN",
        departure_date=date(2026, 9, 1), trip_shape=TripShape.ONE_WAY,
        travelers=1, travelers_bucket="1",
        total_amount_minor=12000, per_person_minor=12000, currency="EUR",
        direct=True, stops=0, search_id="s1", acquisition_call_id="c1",
        search_mode=SearchModeTag.SMART,
    )
    base.update(kw)
    return PriceObservation(**base)


# ======================================================================
# Persistence
# ======================================================================
def test_observation_survives_a_fresh_database_object(tmp_path):
    path = str(tmp_path / "pm.db")
    d1 = Database(path)
    pm.record_observations(d1, [_obs(acquisition_call_id="c1")])
    d1.close()
    # a completely new Database over the same file — models process restart
    d2 = Database(path)
    mk = MarketKey.build(provider="duffel", origin="CGN", destination="BCN",
                         departure_date=date(2026, 9, 1))
    assert len(pm.observations_for_market(d2, mk)) == 1


def test_migration_from_v4_database_is_non_destructive(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    # hand-build a minimal v4-shaped DB with a booking economics row
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER NOT NULL);"
        "INSERT INTO schema_version VALUES (4);"
        "CREATE TABLE booking_economics (booking_id TEXT PRIMARY KEY, x INTEGER);"
        "INSERT INTO booking_economics VALUES ('bk_keep', 42);"
    )
    conn.commit()
    conn.close()
    d = Database(path)  # runs _migrate
    # Asserted against the live constant, not a hardcoded number - this test
    # exists to prove migration is non-destructive, not to pin the current
    # schema version (which every phase that adds tables bumps).
    assert d.query_one("SELECT version FROM schema_version")["version"] == SCHEMA_VERSION
    assert d.query_one("SELECT x FROM booking_economics WHERE booking_id='bk_keep'")["x"] == 42
    assert d.query("SELECT * FROM price_observations") == []


def test_money_and_currency_integrity():
    d = _db()
    pm.record_observations(d, [
        _obs(per_person_minor=9999, total_amount_minor=19998, currency="usd",
             travelers=2, travelers_bucket="2", acquisition_call_id="cA"),
    ])
    row = d.query("SELECT per_person_minor, currency, travelers FROM price_observations")[0]
    assert row["per_person_minor"] == 9999
    assert row["currency"] == "USD"  # normalized upper on write
    assert row["travelers"] == 2


def test_retention_prune_only_touches_observations():
    d = _db()
    d.query("SELECT 1")  # ensure schema
    with d.write() as c:
        c.execute("INSERT INTO booking_economics (booking_id, journey_reference,"
                  " created_at, currency, service_tier, markup_policy_id,"
                  " markup_policy_version, supplier_transport_minor,"
                  " supplier_baggage_minor, supplier_fees_minor, service_fee_minor,"
                  " markup_minor, discount_minor, tax_minor, customer_price_minor,"
                  " breakdown_json, snapshot_json) VALUES"
                  " ('bk1','J',?, 'EUR','BASIC','p',1,0,0,0,0,0,0,0,0,'{}','{}')",
                  (NOW.isoformat(),))
        c.execute("INSERT INTO audit_events (ts, actor, action) VALUES (?,?,?)",
                  (NOW.isoformat(), "ops", "TEST"))
    old = _obs(observed_at=NOW - timedelta(days=400), acquisition_call_id="cOld")
    fresh = _obs(observed_at=NOW - timedelta(days=10), acquisition_call_id="cNew")
    pm.record_observations(d, [old, fresh])
    deleted = pm.prune(d, retention_days=180, now=NOW)
    assert deleted == 1
    assert len(d.query("SELECT 1 FROM price_observations")) == 1
    assert len(d.query("SELECT 1 FROM booking_economics")) == 1  # untouched
    assert len(d.query("SELECT 1 FROM audit_events")) == 1       # untouched


# ======================================================================
# Separation — Price Memory is never a bookable/current price
# ======================================================================
def test_price_observation_has_no_bookable_amount_accessor():
    o = _obs()
    for attr in ("as_quote", "current_price", "bookable_amount", "to_fare", "amount"):
        assert not hasattr(o, attr), f"PriceObservation must not expose {attr}"


def test_historical_signal_is_flagged_not_a_quote():
    d = _db()
    pm.record_observations(d, [_obs(acquisition_call_id=f"c{i}") for i in range(4)])
    mk = MarketKey.build(provider="duffel", origin="CGN", destination="BCN",
                         departure_date=date(2026, 9, 1))
    sig = primary_signal(market_signals(d, mk))
    assert sig.not_a_quote is True
    for attr in ("as_quote", "current_price", "bookable_amount"):
        assert not hasattr(sig, attr)


def test_checkout_and_revalidation_never_import_price_memory():
    import detoura.services.revalidation as rv
    import detoura.services.booking_commercial as bc
    import detoura.services.booking_flow as bf
    import detoura.services.recheck as rc
    src = "".join(
        open(m.__file__).read() for m in (rv, bc, bf, rc)
    )
    assert "price_memory" not in src
    assert "market_intel" not in src
    assert "search_intel" not in src


def test_cache_and_price_memory_are_independent_modules():
    import detoura.providers.cache as cache
    import detoura.persistence.price_memory as mem
    assert "price_memory" not in open(cache.__file__).read()
    assert "ExpiringProviderCache" not in open(mem.__file__).read()


# ======================================================================
# Aggregation
# ======================================================================
def test_median_and_percentiles():
    d = _db()
    prices = [10000, 11000, 12000, 13000, 20000]
    pm.record_observations(d, [
        _obs(per_person_minor=p, total_amount_minor=p, acquisition_call_id=f"c{i}")
        for i, p in enumerate(prices)
    ])
    mk = MarketKey.build(provider="duffel", origin="CGN", destination="BCN",
                         departure_date=date(2026, 9, 1))
    sig = primary_signal(market_signals(d, mk, now=NOW))
    assert sig.sample_count == 5
    assert sig.median_observed_minor == 12000
    assert sig.cheap_reference_minor <= sig.median_observed_minor <= sig.expensive_reference_minor
    assert sig.min_observed_minor == 10000
    assert sig.max_observed_minor == 20000


def test_sparse_history_suppresses_percentiles_and_is_low_confidence():
    d = _db()
    pm.record_observations(d, [_obs(observed_at=NOW - timedelta(days=1))])
    mk = MarketKey.build(provider="duffel", origin="CGN", destination="BCN",
                         departure_date=date(2026, 9, 1))
    sig = primary_signal(market_signals(d, mk, now=NOW))
    assert sig.sample_count == 1
    assert sig.median_observed_minor is None       # suppressed below min_samples
    assert sig.confidence.verdict in (MarketConfidence.LOW, MarketConfidence.NONE)


def test_stale_history_is_not_as_trustworthy_as_recent():
    d = _db()
    # many stale observations
    pm.record_observations(d, [
        _obs(observed_at=NOW - timedelta(days=120), per_person_minor=12000,
             acquisition_call_id=f"cold{i}")
        for i in range(10)
    ])
    mk_stale = MarketKey.build(provider="duffel", origin="CGN", destination="BCN",
                               departure_date=date(2026, 9, 1))
    stale = primary_signal(market_signals(d, mk_stale, now=NOW))

    d2 = _db()
    pm.record_observations(d2, [
        _obs(observed_at=NOW - timedelta(days=2), per_person_minor=12000,
             destination="MAD", acquisition_call_id=f"warm{i}")
        for i in range(10)
    ])
    mk_fresh = MarketKey.build(provider="duffel", origin="CGN", destination="MAD",
                               departure_date=date(2026, 9, 1))
    fresh = primary_signal(market_signals(d2, mk_fresh, now=NOW))

    assert fresh.confidence.score > stale.confidence.score
    assert fresh.confidence.recency_component > stale.confidence.recency_component


def test_currencies_are_not_merged():
    d = _db()
    pm.record_observations(d, [
        _obs(currency="EUR", per_person_minor=10000, acquisition_call_id=f"e{i}")
        for i in range(4)
    ] + [
        _obs(currency="USD", per_person_minor=25000, acquisition_call_id=f"u{i}")
        for i in range(4)
    ])
    mk = MarketKey.build(provider="duffel", origin="CGN", destination="BCN",
                         departure_date=date(2026, 9, 1))
    sigs = market_signals(d, mk, now=NOW)
    assert set(sigs) == {"EUR", "USD"}
    assert sigs["EUR"].median_observed_minor == 10000
    assert sigs["USD"].median_observed_minor == 25000
    assert sigs["EUR"].sample_count == 4 and sigs["USD"].sample_count == 4


def test_batch_lookup_is_one_query(monkeypatch):
    d = _db()
    pm.record_observations(d, [
        _obs(destination=dst, acquisition_call_id=f"{dst}{i}")
        for dst in ("BCN", "MAD", "VIE") for i in range(3)
    ])
    from detoura.services import market_intel
    markets = [
        MarketKey.build(provider="duffel", origin="CGN", destination=dst,
                        departure_date=date(2026, 9, 1))
        for dst in ("BCN", "MAD", "VIE", "PRG")  # PRG unseen
    ]
    calls = {"n": 0}
    real_query = d.query
    monkeypatch.setattr(d, "query", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real_query(*a, **k))[1])
    out = market_intel.batch_market_signals(d, markets, now=NOW)
    assert calls["n"] == 1  # a single SELECT for all four markets
    assert len(out) == 3    # PRG has no history -> absent
    for key_tuple, sigs in out.items():
        assert sigs["EUR"].sample_count == 3


# ======================================================================
# Scoring / explore-exploit
# ======================================================================
def _sig(d, dst, n, *, days_old=1, price=12000) -> HistoricalPriceSignal | None:
    pm.record_observations(d, [
        _obs(destination=dst, per_person_minor=price,
             observed_at=NOW - timedelta(days=days_old), acquisition_call_id=f"{dst}{i}")
        for i in range(n)
    ])
    mk = MarketKey.build(provider="duffel", origin="CGN", destination=dst,
                         departure_date=date(2026, 9, 1))
    return primary_signal(market_signals(d, mk, now=NOW))


def test_exploit_candidate_chosen_when_confidence_high():
    d = _db()
    strong = _sig(d, "BCN", 10)
    weak = _sig(d, "MAD", 1, days_old=90)
    cands = [
        CandidateInput("CGN→BCN", 0, strong, 0.5, True),
        CandidateInput("CGN→MAD", 1, weak, 0.5, True),
    ]
    scored = {s.market: s for s in score_candidates(cands)}
    assert scored["CGN→BCN"].stance.value == "EXPLOIT"
    assert scored["CGN→MAD"].stance.value == "EXPLORE"


def test_unseen_destination_stays_reachable_under_a_budget():
    d = _db()
    known = [
        CandidateInput(f"CGN→K{i}", i, _sig(d, f"K{i}", 12), 0.9, True)
        for i in range(6)
    ]
    unseen = [
        CandidateInput(f"CGN→U{i}", 6 + i, None, 0.1, True) for i in range(4)
    ]
    scored = score_candidates(known + unseen)
    chosen = allocate(scored, slots=5)
    assert len(chosen) == 5
    assert any(c.stance.value == "EXPLORE" for c in chosen), (
        "at least one EXPLORE slot must survive allocation"
    )


def test_historical_cheapness_alone_does_not_dominate():
    d = _db()
    cheap_known = _sig(d, "BCN", 12, price=5000)
    pricier_known = _sig(d, "MAD", 12, price=15000)
    # same everything except price; preference favours MAD strongly
    cands = [
        CandidateInput("CGN→BCN", 0, cheap_known, 0.1, True),
        CandidateInput("CGN→MAD", 1, pricier_known, 0.95, True),
    ]
    scored = {s.market: s for s in score_candidates(cands)}
    # the cheaper market is not allowed to run away with it purely on price
    assert scored["CGN→MAD"].score >= scored["CGN→BCN"].score - 0.15


def test_empty_price_memory_scores_everything_as_explore():
    scored = score_candidates([
        CandidateInput("CGN→BCN", 0, None, 0.6, True),
        CandidateInput("CGN→MAD", 1, None, 0.4, True),
    ])
    assert all(s.stance.value == "EXPLORE" for s in scored)
    assert all(0.0 <= s.score <= 1.0 for s in scored)
