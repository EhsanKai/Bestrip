"""V9 Phase 1 QA fix round — adversarial provenance tests.

Every test here would have FAILED before the fix that moved EXPLOIT/EXPLORE
provenance from a destination-keyed map onto the AcquisitionEdge itself.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from detoura.data.destinations import DESTINATIONS
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.models.search_intel import (
    AcquisitionStance,
    CandidateProvenance,
    EdgeKind,
)
from detoura.providers.duffel import DuffelTransportProvider
from detoura.providers.http import HttpResponse
from detoura.persistence import price_memory as pm
from detoura.persistence.db import Database
from detoura.search_intel_config import SearchIntelConfig
from detoura.services.acquisition import ProviderCallBudget, build_plan, days_for_request
from detoura.services.live_search import live_search
from detoura.services.search_intel_recorder import SearchIntelRecorder
from detoura.services.selection_store import SelectionStore
from tests import duffel_fixtures as fx


def _req(**kw) -> TripRequest:
    f = dict(origin="Köln", budget=4000.0, travelers=2, duration_days=4,
             date_from=date(2026, 10, 12), date_to=date(2026, 10, 26),
             preferences=TravelPreferences(history=0.9, culture=0.8))
    f.update(kw)
    return TripRequest(**f)


class _Stub:
    def __init__(self):
        self.calls = 0

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.calls += 1
        sl = json.loads(body)["data"]["slices"][0]
        offer = fx._offer(
            f"off_{self.calls:020d}",
            [fx._slice(sl["origin"], sl["destination"], "PT2H", [
                fx._segment(sl["origin"], sl["destination"],
                            "2026-10-14T08:00:00", "2026-10-14T10:00:00",
                            "PT2H", carrier="LH")])],
            total_amount="95.00", total_currency="EUR",
            expires_at="2027-01-01T00:00:00Z",
        )
        return HttpResponse(status=200, body=json.dumps(fx.response([offer])))


def _duffel(stub):
    return DuffelTransportProvider(access_token="duffel_test_x", http_client=stub, max_offers=20)


def _budget(**kw):
    f = dict(max_offer_requests=60, max_destinations=3, max_date_variants=1,
             max_airport_variants=1)
    f.update(kw)
    return ProviderCallBudget(**f)


def _days(req, n=1):
    return days_for_request(req, req.candidate_start_dates(), max_days=n)


def _warm(db, *, budget=None, runs=8, cfg=None):
    """Run several recorded searches to build history."""
    budget = budget or _budget()
    cfg = cfg or SearchIntelConfig()
    for _ in range(runs):
        req = _req()
        rec = SearchIntelRecorder(db, req, mode="SMART", provider="duffel",
                                  provider_call_budget=budget.max_offer_requests, cfg=cfg)
        live_search(req, duffel=_duffel(_Stub()), selection_store=SelectionStore(),
                    destinations=DESTINATIONS, airports=["CGN"], days=_days(req),
                    budget=budget, recorder=rec)


# ======================================================================
# build_plan attaches provenance to BOTH directions + inter-city
# ======================================================================
def test_build_plan_stamps_both_edge_directions_with_the_same_candidate_decision():
    req = _req()
    prov = {
        "Barcelona": CandidateProvenance(AcquisitionStance.EXPLOIT, "exploit: history high", 0, date(2026, 10, 12)),
        "Vienna": CandidateProvenance(AcquisitionStance.EXPLORE, "explore: no history", 1, date(2026, 10, 12)),
    }
    plan = build_plan(
        req, destinations=DESTINATIONS, airports=["CGN"],
        days=_days(req), budget=_budget(max_destinations=2, include_inter_city=True),
        candidate_provenance=prov,
    )
    by_pair = {(e.origin, e.destination): e for e in plan.edges}
    out = by_pair[("CGN", "Barcelona")]
    ret = by_pair[("Barcelona", "CGN")]
    assert out.provenance.kind is EdgeKind.OUTBOUND
    assert ret.provenance.kind is EdgeKind.RETURN
    assert out.provenance.stance is AcquisitionStance.EXPLOIT
    assert ret.provenance.stance is AcquisitionStance.EXPLOIT   # <-- was EXPLORE before the fix
    assert out.provenance.candidate_rank == ret.provenance.candidate_rank == 0
    assert ret.provenance.reason and "Barcelona" in ret.provenance.reason

    inter = by_pair[("Barcelona", "Vienna")]
    assert inter.provenance.kind is EdgeKind.INTER_CITY
    # one EXPLORE endpoint -> the hop is EXPLORE
    assert inter.provenance.stance is AcquisitionStance.EXPLORE
    assert "between selected markets Barcelona and Vienna" in inter.provenance.reason
    assert inter.provenance.secondary_candidate_id == "Vienna"


def test_inter_city_is_exploit_only_when_both_endpoints_are_exploit():
    req = _req()
    prov = {
        "Barcelona": CandidateProvenance(AcquisitionStance.EXPLOIT, "x", 0, date(2026, 10, 12)),
        "Madrid": CandidateProvenance(AcquisitionStance.EXPLOIT, "y", 1, date(2026, 10, 12)),
    }
    plan = build_plan(req, destinations=DESTINATIONS, airports=["CGN"], days=_days(req),
                      budget=_budget(max_destinations=2, include_inter_city=True),
                      candidate_provenance=prov)
    inter = next(e for e in plan.edges if (e.origin, e.destination) == ("Barcelona", "Madrid"))
    assert inter.provenance.stance is AcquisitionStance.EXPLOIT


# A. return-leg stance propagation (recorded)
# B. exact trace counters
# C. persisted observations consistent both directions
def test_A_B_C_return_leg_stance_reason_rank_and_exact_counters():
    db = Database(":memory:")
    _warm(db, runs=9)  # build enough history for some markets to become EXPLOIT

    budget = _budget()
    req = _req()
    rec = SearchIntelRecorder(db, req, mode="SMART", provider="duffel",
                              provider_call_budget=budget.max_offer_requests, cfg=SearchIntelConfig())
    result = live_search(req, duffel=_duffel(_Stub()), selection_store=SelectionStore(),
                         destinations=DESTINATIONS, airports=["CGN"], days=_days(req),
                         budget=budget, recorder=rec)
    trace = result.search_trace

    # B — exact counters, recomputed from the call outcomes, no >= slop
    exploit = sum(1 for c in trace.call_outcomes if c.stance is AcquisitionStance.EXPLOIT)
    explore = sum(1 for c in trace.call_outcomes if c.stance is AcquisitionStance.EXPLORE)
    assert trace.calls_exploit == exploit
    assert trace.calls_explore == explore
    assert trace.calls_exploit + trace.calls_explore == len(trace.call_outcomes)
    assert trace.explore_fraction == round(explore / (exploit + explore), 4)
    assert exploit >= 1, "warm-up should have produced at least one EXPLOIT market"

    # A / C — every OUTBOUND edge and its RETURN sibling share the stance
    rows = db.query(
        "SELECT origin, destination, edge_kind, exploration, candidate_reason,"
        " candidate_rank FROM price_observations WHERE search_id = ?",
        (rec.search_id,),
    )
    by_edge = {(r["origin"], r["destination"]): r for r in rows}
    origin_iata = "CGN"
    checked = 0
    for (o, d), r in by_edge.items():
        if r["edge_kind"] != "OUTBOUND":
            continue
        ret = by_edge.get((d, origin_iata))
        assert ret is not None, f"no RETURN row for {d}"
        assert ret["edge_kind"] == "RETURN"
        assert ret["exploration"] == r["exploration"]          # same stance
        assert ret["candidate_rank"] == r["candidate_rank"]     # same rank
        assert ret["candidate_reason"], "RETURN reason must not be empty"
        checked += 1
    assert checked >= 1


# D. cold start — both directions consistently EXPLORE
def test_D_cold_start_bidirectional_edges_are_consistently_explore():
    db = Database(":memory:")
    budget = _budget()
    req = _req()
    rec = SearchIntelRecorder(db, req, mode="SMART", provider="duffel",
                              provider_call_budget=budget.max_offer_requests, cfg=SearchIntelConfig())
    result = live_search(req, duffel=_duffel(_Stub()), selection_store=SelectionStore(),
                         destinations=DESTINATIONS, airports=["CGN"], days=_days(req),
                         budget=budget, recorder=rec)
    t = result.search_trace
    assert t.calls_exploit == 0
    assert t.calls_explore == len(t.call_outcomes)
    rows = db.query("SELECT exploration, edge_kind FROM price_observations WHERE search_id = ?",
                    (rec.search_id,))
    assert rows and all(r["exploration"] == 1 for r in rows)
    assert {r["edge_kind"] for r in rows} <= {"OUTBOUND", "RETURN", "INTER_CITY"}


# E. inter-city provenance is explicit, never silently defaulted
def test_E_inter_city_edges_have_explicit_non_empty_provenance():
    db = Database(":memory:")
    budget = _budget(include_inter_city=True)
    req = _req()
    rec = SearchIntelRecorder(db, req, mode="SMART", provider="duffel",
                              provider_call_budget=budget.max_offer_requests, cfg=SearchIntelConfig())
    live_search(req, duffel=_duffel(_Stub()), selection_store=SelectionStore(),
                destinations=DESTINATIONS, airports=["CGN"], days=_days(req),
                budget=budget, recorder=rec)
    rows = db.query(
        "SELECT candidate_reason, secondary_market, exploration FROM price_observations"
        " WHERE search_id = ? AND edge_kind = 'INTER_CITY'", (rec.search_id,))
    if not rows:
        pytest.skip("budget did not include an inter-city call")
    for r in rows:
        assert r["candidate_reason"].startswith("inter-city edge between selected markets")
        assert r["secondary_market"]
        assert r["candidate_reason"] != "unplanned edge"


# F. multi-date attribution is truthful
def test_F_multi_date_records_the_scoring_reference_date():
    db = Database(":memory:")
    req = _req()
    budget = _budget(max_date_variants=2)
    days = _days(req, n=2)
    assert len(days) == 2
    rec = SearchIntelRecorder(db, req, mode="SMART", provider="duffel",
                              provider_call_budget=budget.max_offer_requests, cfg=SearchIntelConfig())
    live_search(req, duffel=_duffel(_Stub()), selection_store=SelectionStore(),
                destinations=DESTINATIONS, airports=["CGN"], days=days,
                budget=budget, recorder=rec)
    rows = db.query(
        "SELECT departure_date, scoring_reference_date FROM price_observations"
        " WHERE search_id = ?", (rec.search_id,))
    assert rows
    ref = days[0].isoformat()
    # every row's scoring_reference_date is the first (scored) date — even the
    # rows whose own departure_date is the second variant. The decision was
    # shared; the field says so truthfully.
    assert all(r["scoring_reference_date"] == ref for r in rows if r["scoring_reference_date"])
    assert any(r["departure_date"] != ref for r in rows), "expected a second-date row"


# G. currency comes from the established invariant, not a literal
def test_G_currency_is_base_currency_by_invariant():
    from detoura.models.money import BASE_CURRENCY
    db = Database(":memory:")
    req = _req()
    budget = _budget()
    rec = SearchIntelRecorder(db, req, mode="SMART", provider="duffel",
                              provider_call_budget=budget.max_offer_requests, cfg=SearchIntelConfig())
    live_search(req, duffel=_duffel(_Stub()), selection_store=SelectionStore(),
                destinations=DESTINATIONS, airports=["CGN"], days=_days(req),
                budget=budget, recorder=rec)
    rows = db.query("SELECT DISTINCT currency FROM price_observations WHERE search_id = ?",
                    (rec.search_id,))
    assert [r["currency"] for r in rows] == [BASE_CURRENCY]
    # and the recorder module encodes the invariant, not a bare "EUR"
    src = open(__import__("detoura.services.search_intel_recorder", fromlist=["x"]).__file__).read()
    assert 'currency="EUR"' not in src
    assert "BASE_CURRENCY" in src


# H. winner attribution follows one canonical rule (rank, not list position)
def test_H_winner_is_lowest_rank_not_first_in_list():
    db = Database(":memory:")
    req = _req()
    rec = SearchIntelRecorder(db, req, cfg=SearchIntelConfig(top_k=2), provider_call_budget=10)

    class _L:
        def __init__(self, c): self.acquisition_call_id = c

    class _T:
        def __init__(self, rank, cids): self.rank = rank; self.legs = [_L(c) for c in cids]

    # list order deliberately NOT rank order
    recs = [
        _T(3, ["c-third"]),
        _T(1, ["c-winner"]),      # lowest rank -> winner, even though index 1
        _T(2, ["c-second"]),
    ]
    rec._by_call = {c: object() for c in ("c-third", "c-winner", "c-second")}
    rec.attribute(recs)
    assert rec._winner_calls == {"c-winner"}
    assert rec._top_k_calls == {"c-winner", "c-second"}   # 2 lowest ranks
    assert rec._candidate_calls == {"c-third", "c-winner", "c-second"}


def test_H_no_rank_means_never_winner():
    db = Database(":memory:")
    rec = SearchIntelRecorder(db, _req(), cfg=SearchIntelConfig(), provider_call_budget=10)

    class _T:
        def __init__(self, cids): self.legs = [type("L", (), {"acquisition_call_id": c})() for c in cids]

    rec._by_call = {"c1": object()}
    rec.attribute([_T(["c1"])])   # no .rank attribute anywhere
    assert rec._winner_calls == set()
    assert rec._top_k_calls == set()
    assert rec._candidate_calls == {"c1"}


# Dataset trustworthiness — pre-fix rows are identifiable and excluded
def test_pre_fix_observations_are_excluded_from_aggregates_and_prunable():
    from datetime import datetime, timezone
    from detoura.models.search_intel import PriceObservation, SearchModeTag, TripShape, MarketKey
    from detoura.services.market_intel import market_signals, primary_signal
    db = Database(":memory:")

    def _o(ver, i):
        return PriceObservation(
            observation_id=pm.new_observation_id(), observed_at=datetime.now(timezone.utc),
            provider="duffel", origin="CGN", destination="BCN",
            departure_date=date(2026, 9, 1), trip_shape=TripShape.ONE_WAY,
            travelers=1, travelers_bucket="1", total_amount_minor=10000,
            per_person_minor=10000, currency="EUR", search_id="s",
            acquisition_call_id=f"c{ver}{i}", search_mode=SearchModeTag.SMART,
            provenance_version=ver,
        )
    pm.record_observations(db, [_o(1, i) for i in range(5)] + [_o(2, i) for i in range(4)])

    mk = MarketKey.build(provider="duffel", origin="CGN", destination="BCN",
                         departure_date=date(2026, 9, 1))
    sig = primary_signal(market_signals(db, mk))
    assert sig.sample_count == 4  # only the version-2 rows

    deleted = pm.prune_stale_provenance(db)
    assert deleted == 5
    assert db.query_one("SELECT COUNT(*) n FROM price_observations")["n"] == 4
    # economics/audit untouched — different tables, never referenced
    assert "booking_economics" not in open(pm.__file__).read().split("prune_stale_provenance")[1][:400]
