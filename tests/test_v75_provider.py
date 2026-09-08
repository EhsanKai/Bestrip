"""V7.5: real provider foundation.

Three things are defended here, and they are the three ways this feature could
lie or fall over.

**The optimizer must never touch the network.** Measured on V7's own HEAD, one
SMART search issues 2,177 upstream route lookups. Wiring a real API into beam
expansion is not slow, it is impossible - so acquisition happens once, up
front, inside a budget, and the search reads a snapshot.

**Real data must not weaken V7's honesty.** Missing baggage stays UNKNOWN. A
foreign currency is converted or refused, never relabelled. A connection
airport is not a visited city.

**Failure must not become a fact.** A timeout is not "no flights", a bounded
search is not an exhausted one, and an expired offer is not a price.

The Duffel payloads these run against are **modelled on documented shapes, not
captured from the API**. Until a real sandbox response has been compared to
them the live path is UNVERIFIED, and nothing here may be read as evidence
otherwise.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from detoura.config import PlannerConfig
from detoura.data.destinations import DESTINATIONS
from detoura.models.baggage import BaggageStatus
from detoura.models.provider_reference import OfferFreshness, ProviderOfferReference
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.providers.cache import ExpiringProviderCache
from detoura.providers.duffel import (
    DuffelConfigurationError,
    DuffelTransportProvider,
    is_test_token,
    redact,
)
from detoura.providers.failures import ProviderFailureKind
from detoura.providers.transport import SyntheticTransportDataProvider
from detoura.search_modes import SearchMode, apply_mode
from detoura.services.acquisition import (
    ProviderCallBudget,
    SnapshotTransportProvider,
    acquire,
    build_plan,
    days_for_request,
    rank_candidates,
)
from detoura.services.planner import TravelPlanner

from . import duffel_fixtures as fx

TOKEN = "duffel_test_notarealtoken"


def provider(**kw) -> DuffelTransportProvider:
    return DuffelTransportProvider(access_token=TOKEN, **kw)


def a_request(**kw) -> TripRequest:
    fields = dict(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=date(2026, 9, 10), date_to=date(2026, 9, 24),
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
    )
    fields.update(kw)
    return TripRequest(**fields)


# ---------------------------------------------------------------------------
# Token safety — fail closed, never leak
# ---------------------------------------------------------------------------
def test_a_missing_token_is_refused_before_any_request():
    with pytest.raises(DuffelConfigurationError, match="not set"):
        DuffelTransportProvider(access_token="")


@pytest.mark.parametrize("token", [
    "duffel_live_abc", "abc", "DUFFEL_TEST_abc", " duffel_test_abc", "live_abc",
])
def test_a_token_that_is_not_clearly_test_mode_fails_closed(token):
    """"Might be live" is the same as live when the downside is a real order."""
    with pytest.raises(DuffelConfigurationError, match="test-mode"):
        DuffelTransportProvider(access_token=token)
    assert not is_test_token(token)


def test_a_non_test_token_can_only_be_used_deliberately():
    built = DuffelTransportProvider(
        access_token="duffel_live_abc", allow_non_test_token=True
    )
    assert built is not None, "the escape hatch must exist, but be explicit"


def test_the_token_never_appears_in_a_redaction():
    assert TOKEN not in redact(TOKEN)
    assert redact(TOKEN) == "duffel_test_<redacted>"
    assert redact(None) == "<unset>"
    assert "live" not in redact("duffel_live_secret").replace("non-test", "")


def test_the_token_never_appears_in_the_refusal_message():
    secret = "duffel_live_SUPERSECRETVALUE"
    with pytest.raises(DuffelConfigurationError) as caught:
        DuffelTransportProvider(access_token=secret)
    assert secret not in str(caught.value)
    assert "SUPERSECRET" not in str(caught.value)


# ---------------------------------------------------------------------------
# Structural mapping — a connection is not a city
# ---------------------------------------------------------------------------
def test_a_direct_flight_maps_to_one_leg():
    option = provider().parse_offers(fx.DIRECT, "CGN", "BCN")[0]
    assert (option.origin, option.destination) == ("CGN", "BCN")
    assert option.duration_minutes == 130
    assert option.price_per_person == 142.51
    assert option.transport_type.value == "flight"


def test_a_connection_airport_does_not_become_a_visited_city():
    """CGN -> LHR -> MAD is one leg to Madrid, not a visit to London.

    The sharpest structural risk in the whole integration: mapping segments to
    legs would make the beam count Heathrow as a destination, inflate the
    trip's city count and let a layover masquerade as a place the traveller
    went.
    """
    option = provider().parse_offers(fx.ONE_STOP, "CGN", "MAD")[0]
    assert (option.origin, option.destination) == ("CGN", "MAD")
    assert "LHR" not in (option.origin, option.destination)
    # The connection survives as metadata, for display and for booking.
    assert len(option.provider_ref.raw_segments) == 2
    assert option.provider_ref.raw_segments[0]["destination"] == "LHR"


def test_a_multi_slice_offer_is_refused_because_its_total_cannot_be_split():
    """A return offer quotes one total for both directions.

    Nothing in it says how that divides, so the outbound leg cannot be priced
    from it. An earlier cut attached the whole round-trip figure to the one-way
    leg - a one-way flight priced at the return fare. Splitting evenly would be
    worse: an invented number wearing the provider's authority. Refused and
    recorded instead.
    """
    duffel = provider()
    assert duffel.parse_offers(fx.MULTI_SLICE, "CGN", "BCN") == []
    assert duffel.offers_dropped == [
        ("off_return", ProviderFailureKind.MALFORMED_RESPONSE)
    ]


def test_carrier_and_flight_number_survive():
    option = provider().parse_offers(fx.DIRECT, "CGN", "BCN")[0]
    assert "VY" in option.operator and "1234" in option.operator
    assert option.provider_ref.owner_iata == "VY"


# ---------------------------------------------------------------------------
# Money — never relabel a currency
# ---------------------------------------------------------------------------
def test_a_foreign_currency_is_converted_not_relabelled():
    option = provider().parse_offers(fx.USD_OFFER, "CGN", "BCN")[0]
    # USD 160 for the party at the fixed 0.92 rate, halved for two travellers.
    assert option.price_per_person != 160.0, "a USD amount was kept as EUR"
    assert option.price_per_person == pytest.approx(147.2, abs=0.01)


def test_an_unconvertible_currency_is_a_typed_failure_not_a_missing_flight():
    """A currency with no rate is our misconfiguration, not an empty market."""
    duffel = provider()
    assert duffel.parse_offers(fx.UNCONVERTIBLE_CURRENCY, "CGN", "BCN") == []
    assert duffel.offers_dropped == [
        ("off_xyz", ProviderFailureKind.CURRENCY_UNAVAILABLE)
    ]
    assert ProviderFailureKind.CURRENCY_UNAVAILABLE.is_infrastructure


def test_a_negative_or_unparseable_amount_is_rejected():
    duffel = provider()
    duffel.parse_offers(fx.MALFORMED_OFFERS, "CGN", "BCN")
    assert any(kind is ProviderFailureKind.MALFORMED_RESPONSE
               for _, kind in duffel.offers_dropped)


# ---------------------------------------------------------------------------
# Baggage — V7's guarantees, applied to real data
# ---------------------------------------------------------------------------
def test_an_included_cabin_bag_is_included():
    option = provider().parse_offers(fx.DIRECT, "CGN", "BCN")[0]
    assert option.baggage.cabin_bag.status is BaggageStatus.INCLUDED


@pytest.mark.parametrize("fixture,dest", [
    (fx.BAGGAGE_SILENT, "BCN"),
    (fx.BAGGAGE_ABSENT, "BCN"),
    (fx.BAGGAGE_MIXED_LEGS, "MAD"),
])
def test_missing_baggage_information_stays_unknown(fixture, dest):
    """Never INCLUDED, never a fee of zero. The V7 Phase 3 rule, at the source."""
    option = provider().parse_offers(fixture, "CGN", dest)[0]
    assert option.baggage.cabin_bag.status is BaggageStatus.UNKNOWN
    assert option.baggage.cabin_bag.price is None
    assert option.baggage.cabin_bag.extra_cost_per_traveller is None


def test_a_bag_included_on_one_leg_only_is_not_included_for_the_journey():
    """The traveller still has to get it onto the second aircraft."""
    option = provider().parse_offers(fx.BAGGAGE_MIXED_LEGS, "CGN", "MAD")[0]
    assert option.baggage.cabin_bag.status is not BaggageStatus.INCLUDED


def test_a_purchasable_bag_becomes_extra_with_its_quoted_price():
    option = provider().parse_offers(fx.CABIN_INCLUDED_CHECKED_FOR_SALE, "CGN", "BCN")[0]
    checked = option.baggage.checked_bag
    assert checked.status is BaggageStatus.EXTRA
    assert checked.price is not None and checked.price.amount == 25.0


def test_duffel_never_claims_to_know_about_personal_items():
    """Duffel has no personal-item concept, so we have not been told."""
    option = provider().parse_offers(fx.DIRECT, "CGN", "BCN")[0]
    assert option.baggage.personal_item.status is BaggageStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Offer freshness
# ---------------------------------------------------------------------------
def test_expiry_is_carried_and_an_absent_expiry_is_unknown_not_fresh():
    with_expiry = provider().parse_offers(fx.DIRECT, "CGN", "BCN")[0]
    assert with_expiry.provider_ref.expires_at is not None

    without = provider().parse_offers(fx.NO_EXPIRY, "CGN", "BCN")[0]
    assert without.provider_ref.expires_at is None
    assert without.provider_ref.freshness_at() is OfferFreshness.UNKNOWN, (
        "an undated quote has not been shown to be current"
    )


def test_an_expired_offer_reports_expired():
    option = provider().parse_offers(fx.EXPIRED, "CGN", "BCN")[0]
    assert option.provider_ref.is_expired_at() is True


def test_hold_capability_is_tri_state():
    instant = provider().parse_offers(fx.DIRECT, "CGN", "BCN")[0]
    holdable = provider().parse_offers(fx.HOLDABLE, "CGN", "BCN")[0]
    assert instant.provider_ref.hold_supported is False
    assert holdable.provider_ref.hold_supported is True
    # And unknown stays reachable for a provider that says nothing.
    assert ProviderOfferReference(provider="x", offer_id="y").hold_supported is None


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------
def test_one_broken_offer_does_not_cost_the_whole_page():
    duffel = provider()
    options = duffel.parse_offers(fx.MIXED_VALID_AND_BROKEN, "CGN", "BCN")
    assert len(options) == 1, "the usable offer must survive its broken neighbours"
    assert duffel.offers_seen == 6 and len(duffel.offers_dropped) == 5


def test_an_empty_page_is_an_answer_not_an_error():
    assert provider().parse_offers(fx.NO_OFFERS, "CGN", "BCN") == []


def test_a_garbage_body_does_not_raise():
    assert provider().parse_offers({}, "CGN", "BCN") == []
    assert provider().parse_offers({"data": {}}, "CGN", "BCN") == []


# ---------------------------------------------------------------------------
# Expiry-aware cache
# ---------------------------------------------------------------------------
def test_the_cache_never_serves_an_offer_past_its_provider_expiry():
    now = [datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)]
    cache = ExpiringProviderCache(ttl_seconds=3600, clock=lambda: now[0])
    expiry = datetime(2026, 10, 15, 12, 10, tzinfo=timezone.utc)
    cache.get_or_compute("k", lambda: ["first"], expires_at=expiry)
    assert cache.get_or_compute("k", lambda: ["second"]) == ["first"]
    now[0] = datetime(2026, 10, 15, 12, 9, 45, tzinfo=timezone.utc)  # inside margin
    assert cache.get_or_compute("k", lambda: ["second"]) == ["second"]
    assert cache.expired_evictions == 1


def test_a_provider_failure_is_never_cached_as_an_empty_answer():
    """The V6.5 invariant, restated for real offers."""
    cache = ExpiringProviderCache()
    with pytest.raises(RuntimeError):
        cache.get_or_compute("k", lambda: (_ for _ in ()).throw(RuntimeError("timeout")))
    assert len(cache) == 0
    assert cache.get_or_compute("k", lambda: ["recovered"]) == ["recovered"]


def test_the_cache_is_bounded():
    cache = ExpiringProviderCache(max_entries=4)
    for index in range(20):
        cache.get_or_compute(index, lambda i=index: [i])
    assert len(cache) == 4
    assert cache.size_evictions == 16


def test_an_already_expired_answer_is_not_stored_at_all():
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    cache = ExpiringProviderCache(clock=lambda: now)
    cache.get_or_compute("k", lambda: ["dead"], expires_at=now - timedelta(minutes=5))
    assert len(cache) == 0


# ---------------------------------------------------------------------------
# Candidate discovery — not catalog order
# ---------------------------------------------------------------------------
def test_candidate_selection_is_not_catalog_order():
    chosen, _ = rank_candidates(DESTINATIONS, a_request(), limit=8)
    assert [d.id for d in chosen] != [d.id for d in DESTINATIONS][:8]


def test_candidate_selection_is_deterministic():
    first, _ = rank_candidates(DESTINATIONS, a_request(), limit=8)
    second, _ = rank_candidates(DESTINATIONS, a_request(), limit=8)
    assert [d.id for d in first] == [d.id for d in second]


def test_exploration_slots_reach_beyond_the_top_ranked():
    """A pool filled purely by affinity deletes the product's reason to exist."""
    with_exploration, _ = rank_candidates(
        DESTINATIONS, a_request(), limit=8, exploration_share=0.25
    )
    pure_affinity, _ = rank_candidates(
        DESTINATIONS, a_request(), limit=8, exploration_share=0.0
    )
    assert set(d.id for d in with_exploration) != set(d.id for d in pure_affinity)


def test_a_must_visit_city_is_never_dropped_from_the_pool():
    request = a_request(must_visit=["Zurich"])
    chosen, dropped = rank_candidates(DESTINATIONS, request, limit=4)
    assert "Zurich" in [d.id for d in chosen]
    assert "Zurich" not in [d.id for d in dropped]


def test_an_avoided_city_is_never_acquired():
    request = a_request(avoid_destinations=["Berlin"])
    chosen, _ = rank_candidates(DESTINATIONS, request, limit=12)
    assert "Berlin" not in [d.id for d in chosen]


# ---------------------------------------------------------------------------
# The architectural invariant
# ---------------------------------------------------------------------------
def _snapshot_for(request, *, budget_n=400, destinations=8):
    budget = ProviderCallBudget(
        max_offer_requests=budget_n, max_destinations=destinations,
        max_date_variants=5, max_airport_variants=2,
    )
    days = days_for_request(request, request.candidate_start_dates(), max_days=5)
    plan = build_plan(request, destinations=DESTINATIONS,
                      airports=["CGN", "DUS"], days=days, budget=budget)
    upstream = SyntheticTransportDataProvider()
    snapshot = acquire(plan, lambda e: upstream.search(e.origin, e.destination, e.day))
    return plan, snapshot, upstream


def test_beam_search_makes_zero_provider_calls():
    """The invariant the whole acquisition stage exists to guarantee."""
    request = a_request()
    _, snapshot, upstream = _snapshot_for(request)
    after_acquisition = upstream.search_calls

    served = SnapshotTransportProvider(snapshot)
    planner = TravelPlanner(
        config=apply_mode(PlannerConfig(), SearchMode.SMART), transport_provider=served
    )
    planner.plan(request)

    assert upstream.search_calls == after_acquisition, (
        "beam search reached the provider: acquisition is not actually bounded"
    )
    assert served.lookups > 0, "the optimizer did search the snapshot"


def test_the_snapshot_provider_cannot_reach_the_network_by_construction():
    """Guaranteed by having no client at all, not by reviewing call sites."""
    served = SnapshotTransportProvider(_snapshot_for(a_request())[1])
    for attribute in ("http", "host", "_token", "client", "session"):
        assert not hasattr(served, attribute)


def test_provider_calls_stay_inside_the_budget():
    request = a_request()
    for budget_n in (25, 50, 100, 200):
        budget = ProviderCallBudget(max_offer_requests=budget_n, max_destinations=8,
                                    max_date_variants=5, max_airport_variants=2)
        days = days_for_request(request, request.candidate_start_dates(), max_days=5)
        plan = build_plan(request, destinations=DESTINATIONS,
                          airports=["CGN", "DUS"], days=days, budget=budget)
        assert plan.planned_request_count <= budget_n
        upstream = SyntheticTransportDataProvider()
        acquire(plan, lambda e: upstream.search(e.origin, e.destination, e.day))
        assert upstream.search_calls <= budget_n


def test_a_fifty_city_catalog_still_cannot_cause_thousands_of_calls():
    """The scaling question, asked directly.

    The catalog is stretched to 50 by cloning ids; only the *count* matters
    here, because the budget must bind on pool size rather than on which
    cities happen to be in it.
    """
    catalog = list(DESTINATIONS)
    while len(catalog) < 50:
        catalog.extend(DESTINATIONS)
    catalog = [
        d.model_copy(update={"id": f"{d.id}-{i}"}) for i, d in enumerate(catalog[:50])
    ]
    request = a_request()
    budget = ProviderCallBudget(max_offer_requests=200, max_destinations=8,
                                max_date_variants=5, max_airport_variants=2)
    days = days_for_request(request, request.candidate_start_dates(), max_days=5)
    plan = build_plan(request, destinations=catalog, airports=["CGN", "DUS"],
                      days=days, budget=budget)
    assert plan.planned_request_count <= 200
    assert len(plan.destinations) == 8


def test_bounded_coverage_is_reported_not_hidden():
    """A search that stopped looking must not say the trips do not exist."""
    request = a_request()
    plan, snapshot, _ = _snapshot_for(request, budget_n=50)
    assert plan.is_truncated
    kinds = {issue.kind for issue in snapshot.issues}
    assert ProviderFailureKind.CALL_BUDGET_EXHAUSTED in kinds
    assert ProviderFailureKind.CALL_BUDGET_EXHAUSTED.is_infrastructure, (
        "budget exhaustion must never be reportable as 'no trips found'"
    )


def test_a_failing_edge_is_recorded_not_treated_as_an_empty_route():
    request = a_request()
    budget = ProviderCallBudget(max_offer_requests=20, max_destinations=3,
                                max_date_variants=2, max_airport_variants=1)
    days = days_for_request(request, request.candidate_start_dates(), max_days=2)
    plan = build_plan(request, destinations=DESTINATIONS, airports=["CGN"],
                      days=days, budget=budget)

    def always_fails(edge):
        raise TimeoutError("upstream timed out")

    snapshot = acquire(plan, always_fails)
    assert snapshot.offer_count == 0
    assert any(i.kind is ProviderFailureKind.UNAVAILABLE for i in snapshot.issues)


def test_the_snapshot_reports_its_earliest_expiry():
    request = a_request()
    _, snapshot, _ = _snapshot_for(request)
    # Synthetic fares carry no provider reference, so nothing states an expiry.
    assert snapshot.earliest_expiry is None
    assert snapshot.generated_at is not None


# ---------------------------------------------------------------------------
# V7 must be untouched
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode,duration,states,score,route", [
    ("QUICK", 5, 1196, 0.719869, "CGN -> Berlin -> Munich -> CGN"),
    ("SMART", 5, 9730, 0.734457, "CGN -> Berlin -> Munich -> CGN"),
    ("SMART", 7, 10066, 0.736198, "CGN -> Berlin -> Prague -> Vienna -> CGN"),
])
def test_synthetic_golden_signatures_are_unchanged(mode, duration, states, score, route):
    request = a_request(duration_days=duration, preferred_destinations=["Berlin"])
    result = TravelPlanner(
        config=apply_mode(PlannerConfig(), SearchMode(mode))
    ).plan(request)
    assert result.metadata.states_generated == states
    assert result.recommendations[0].score == score
    assert result.recommendations[0].route_label() == route


def test_the_synthetic_provider_still_reports_no_baggage_and_no_reference():
    """V7.5 adds seams, not claims. A fabricated fare knows nothing."""
    option = SyntheticTransportDataProvider().search("CGN", "Berlin", date(2026, 9, 10))[0]
    assert option.baggage is None
    assert option.provider_ref is None


# ---------------------------------------------------------------------------
# The probe CLI
#
# Added after an independent review found the probe broken by a fix elsewhere:
# it still called `search(..., travelers=...)` after that kwarg moved to
# `fetch_offers`, raising TypeError on every real invocation. Nothing caught it
# because nothing exercised this file. A tool whose whole job is to be run by
# hand, once, against a live sandbox is exactly the code least likely to be
# noticed when it rots - so it gets tests.
# ---------------------------------------------------------------------------
def test_the_probe_exits_cleanly_and_sends_nothing_without_a_token(monkeypatch, capsys):
    from detoura.tools import duffel_probe

    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    code = duffel_probe.main(
        ["--origin", "CGN", "--destination", "BCN", "--date", "2026-10-15"]
    )
    assert code == duffel_probe.EXIT_NO_TOKEN
    assert "No request was sent." in capsys.readouterr().out


def test_the_probe_refuses_a_live_looking_token_without_echoing_it(monkeypatch, capsys):
    from detoura.tools import duffel_probe

    secret = "duffel_live_SUPERSECRETVALUE"
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", secret)
    code = duffel_probe.main(
        ["--origin", "CGN", "--destination", "BCN", "--date", "2026-10-15"]
    )
    output = capsys.readouterr().out
    assert code == duffel_probe.EXIT_NO_TOKEN
    assert secret not in output and "SUPERSECRET" not in output
    assert "No request was sent." in output


def test_the_probe_uses_the_network_path_and_survives_a_real_call(monkeypatch, capsys):
    """The regression that slipped through: `search()` refuses by design.

    The probe must call `fetch_offers`. Driven here through a fake HTTP client
    so the whole CLI runs end to end with no network.
    """
    import json as _json

    from detoura.providers import http as http_module
    from detoura.tools import duffel_probe

    class _Stub:
        def request(self, method, url, *, headers=None, params=None, body=None,
                    timeout=None):
            return http_module.HttpResponse(
                status=200, body=_json.dumps(fx.DIRECT), headers={}
            )

    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_probe")
    monkeypatch.setattr(duffel_probe, "DuffelTransportProvider",
                        lambda **kw: DuffelTransportProvider(
                            access_token=kw["access_token"], http_client=_Stub()))
    code = duffel_probe.main(
        ["--origin", "CGN", "--destination", "BCN", "--date", "2026-10-15"]
    )
    output = capsys.readouterr().out
    assert code == duffel_probe.EXIT_OK
    assert "CGN -> BCN" in output
    assert "No order was created." in output
    assert "duffel_test_probe" not in output, "the token must never be printed"
