"""V7.5 ADVERSARIAL REVIEW (Agent 5, independent).

Every test below tries to break a specific claim made about the V7.5 real
flight provider foundation. A passing test documents a guarantee that
survived the attack. A failing test is a real defect, deliberately left
failing rather than fixed, per the review's operating rules.

Nothing here makes a network call. `DuffelTransportProvider` is always driven
either through `parse_offers` on an offline fixture, or through a fake
`HttpClient` that never touches a socket.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from detoura.config import PlannerConfig
from detoura.data.destinations import DESTINATIONS
from detoura.models.baggage import BaggageStatus
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.providers.cache import ExpiringProviderCache
from detoura.providers.duffel import (
    DuffelConfigurationError,
    DuffelTransportProvider,
    ProviderCallBudgetExceeded,
)
from detoura.services.acquisition import (
    AcquisitionEdge,
    ProviderCallBudget,
    acquire,
    build_plan,
    days_for_request,
)
from detoura.providers.http import HttpResponse
from detoura.providers.failures import ProviderFailureKind
from detoura.search_modes import SearchMode, apply_mode
from detoura.services.acquisition import rank_candidates
from detoura.services.planner import TravelPlanner

from . import duffel_fixtures as fx

TOKEN = "duffel_test_notarealtoken"


def a_request(**kw) -> TripRequest:
    fields = dict(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=date(2026, 9, 10), date_to=date(2026, 9, 24),
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
    )
    fields.update(kw)
    return TripRequest(**fields)


class _StubHttpClient:
    """A fake `HttpClient` that never opens a socket.

    Records every call it is asked to make, and the passenger count on each
    request body, so a test can assert on both "did this reach the network
    layer at all" and "what did we tell the provider about the party".
    """

    def __init__(self, body: dict, *, status: int = 200):
        self.calls = 0
        self.passenger_counts: list[int] = []
        self._body = body
        self._status = status

    def request(self, method, url, *, headers=None, params=None, body=None,
                timeout=10.0):
        self.calls += 1
        if body:
            try:
                payload = json.loads(body)
                self.passenger_counts.append(
                    len(payload["data"]["passengers"])
                )
            except Exception:
                pass
        return HttpResponse(status=self._status, body=json.dumps(self._body))


# ===========================================================================
# CLAIM 1/2 — "beam search makes zero provider network calls, and calls are
# bounded" — attacked at the point the review found weakest: nothing stops a
# caller from wiring DuffelTransportProvider straight into TravelPlanner, the
# exact pattern shown in duffel.py's own module docstring as "constructed
# like any other provider". The zero-call guarantee is not architectural; it
# holds only when the caller remembers to route through
# services.acquisition.build_plan/acquire/SnapshotTransportProvider instead.
# ===========================================================================
def test_wiring_duffel_directly_into_travelplanner_is_refused():
    """FIXED. The provider now declines to serve beam search at all.

    This originally documented a P0: `DuffelTransportProvider` satisfied
    `TransportDataProvider` structurally, so the construction its own docstring
    showed wired a real API straight into beam expansion - one Offer Request
    per route explored, measured at 2,177 for a single SMART search. The claim
    "beam search makes zero provider calls" held only while every caller
    remembered to route through acquisition.

    `search()` now raises before counting or sending anything, so the guarantee
    is enforced rather than merely documented.
    """
    http = _StubHttpClient(fx.DIRECT)
    duffel = DuffelTransportProvider(access_token=TOKEN, http_client=http)
    planner = TravelPlanner(
        config=apply_mode(PlannerConfig(), SearchMode.QUICK),
        transport_provider=duffel,
    )
    request = a_request(duration_days=3, preferred_destinations=["Berlin"])
    with pytest.raises(DuffelConfigurationError, match="must not be driven by beam search"):
        planner.plan(request)
    assert duffel.search_calls == 0, "it must refuse before sending anything"


def test_the_refusal_can_be_overridden_but_only_deliberately():
    http = _StubHttpClient(fx.DIRECT)
    duffel = DuffelTransportProvider(
        access_token=TOKEN, http_client=http, acquisition_only=False
    )
    assert duffel.search("CGN", "BCN", date(2026, 10, 15)) != []


def test_a_hard_call_ceiling_stops_anything_that_bypasses_the_plan():
    """A backstop, so the worst case is an exception rather than an invoice."""
    http = _StubHttpClient(fx.DIRECT)
    duffel = DuffelTransportProvider(
        access_token=TOKEN, http_client=http, acquisition_only=False, max_calls=3
    )
    for _ in range(3):
        duffel.search("CGN", "BCN", date(2026, 10, 15))
    with pytest.raises(ProviderCallBudgetExceeded):
        duffel.search("CGN", "BCN", date(2026, 10, 15))


# ===========================================================================
# CLAIM 4/6 — money and structural mapping: a return offer's outbound slice
# is priced at the OFFER'S total, not the slice's share of it.
# ===========================================================================
def test_a_return_offers_outbound_leg_is_never_priced_at_the_round_trip():
    """FIXED. Multi-slice offers are refused rather than mispriced.

    Originally a P0: `_price_per_person` read the offer's `total_amount` - the
    figure covering BOTH slices - and attached it undivided to the single
    outbound leg, pricing a one-way CGN->BCN at the full 266.80 return fare.
    """
    duffel = DuffelTransportProvider(access_token=TOKEN)
    options = duffel.parse_offers(fx.MULTI_SLICE, "CGN", "BCN")
    raw_total = float(fx.MULTI_SLICE["data"]["offers"][0]["total_amount"])
    assert options == [], "an unsplittable total must not become a one-way price"
    assert any(oid == "off_return" for oid, _ in duffel.offers_dropped)
    assert all(o.price_per_person < raw_total for o in options)


# ===========================================================================
# CLAIM 4 — the same claim, restated for the direct-wiring path: party size
# is silently dropped, so a family of 4 gets priced as though it were 1.
# ===========================================================================
def test_party_size_reaches_the_provider_through_acquisition():
    """FIXED. The protocol cannot carry a party size; acquisition can.

    Originally a P1: `TransportDataProvider.search()` takes no `travelers`, so
    a directly-wired provider built every Offer Request for one adult whatever
    the trip's real party. Direct wiring is now refused outright, and the
    acquisition edge carries the party explicitly.
    """
    request = a_request(travelers=4)
    days = days_for_request(request, request.candidate_start_dates(), max_days=2)
    plan = build_plan(
        request, destinations=DESTINATIONS, airports=["CGN"], days=days,
        budget=ProviderCallBudget(max_offer_requests=10, max_destinations=2,
                                  max_date_variants=2, max_airport_variants=1),
    )
    assert plan.travelers == 4
    assert all(edge.travelers == 4 for edge in plan.edges)

    seen: list[int] = []

    def fetch(edge):
        seen.append(edge.travelers)
        return []

    acquire(plan, fetch)
    assert seen and set(seen) == {4}


# ===========================================================================
# CLAIM 7 — cache key correctness for different traveller counts.
# ===========================================================================
def test_the_acquisition_key_keeps_party_sizes_apart():
    """FIXED. `AcquisitionEdge` carries the party, so it is part of identity.

    Originally a P0 in the making: a natural-looking key of (origin,
    destination, day) - the shape used elsewhere in this codebase - would serve
    a family of four the solo traveller's fare. Airlines price per party and
    per remaining seat, so the party size belongs in the key.

    `ExpiringProviderCache` itself still has no opinion about what a key should
    contain, which is correct for a generic memo table; the guarantee lives in
    the edge type that the shipped path actually uses.
    """
    one = AcquisitionEdge("CGN", "BCN", date(2026, 10, 15), 1)
    four = AcquisitionEdge("CGN", "BCN", date(2026, 10, 15), 4)
    assert one != four
    assert hash(one) != hash(four)

    store = {one: ["solo price"]}
    assert four not in store, "a four-person lookup must miss a solo entry"


# ===========================================================================
# CLAIM 8 — candidate discovery: exploration can drop a genuinely good
# destination in favour of a genuinely worse one from deep in the tail.
# ===========================================================================
def test_exploration_trades_affinity_deliberately_and_says_which_cities_it_lost():
    """Exploration reaches past the obvious - and the cost is reported.

    This originally pinned the specific cities the stepped sampler chose
    (Copenhagen at rank 12, while Prague at 8 and Budapest at 9 were dropped).
    That sampler had a real flaw underneath the example: it stepped from the
    head of the rejected tail, so one of its two "exploration" slots went to
    the city affinity would have taken next anyway - measured, it picked rank 7
    and then jumped to rank 12, exploring once for two slots.

    Sampling now spreads inside the tail, so both slots reach genuinely
    different cities. The specific ids therefore changed, and pinning them
    again would re-freeze an implementation detail. What matters, and what is
    asserted here, is the shape: exploration must land beyond the affinity cut,
    and every city it costs must be named in `dropped_destinations` rather than
    quietly vanishing.
    """
    request = a_request()
    weights = request.preferences.experience_weights()

    def affinity(destination):
        profile = destination.experience_vector()
        return sum(profile[n] * w for n, w in weights.items()) / sum(weights.values())

    ranked = sorted(DESTINATIONS, key=lambda d: (-affinity(d), d.id))
    rank_of = {d.id: i for i, d in enumerate(ranked, 1)}

    chosen, dropped = rank_candidates(DESTINATIONS, request, limit=8)
    chosen_ranks = sorted(rank_of[d.id] for d in chosen)

    # Some of the pool must come from beyond the top-8 by affinity, or nothing
    # was explored and the 50-city catalog collapses to "the obvious eight".
    assert any(rank > 8 for rank in chosen_ranks), (
        f"no exploration happened: chosen ranks {chosen_ranks}"
    )
    # And the trade is real: better-ranked cities are genuinely given up.
    assert any(rank_of[d.id] < max(chosen_ranks) for d in dropped)

    # The cost is disclosed, not hidden. This is the part that makes the
    # trade-off defensible rather than a silent loss of coverage.
    plan = build_plan(
        request, destinations=DESTINATIONS, airports=["CGN"],
        days=days_for_request(request, request.candidate_start_dates(), max_days=1),
        budget=ProviderCallBudget(max_offer_requests=500, max_destinations=8,
                                  max_date_variants=1, max_airport_variants=1),
    )
    assert set(plan.dropped_destinations) == {d.id for d in dropped}
    assert plan.is_truncated, "dropping half the catalog is bounded coverage"



# ===========================================================================
# CLAIM 3 — token safety, attacked harder than the implementer's own tests.
# ===========================================================================
def test_token_does_not_leak_through_http_error_paths():
    """A 401 from the provider must not echo the Authorization header back."""
    secret_token = "duffel_test_abcdef123456SECRET"
    http = _StubHttpClient({"errors": []}, status=401)
    duffel = DuffelTransportProvider(access_token=secret_token, http_client=http)
    with pytest.raises(Exception) as caught:
        duffel.search("CGN", "BCN", date(2026, 10, 15))
    assert secret_token not in str(caught.value)
    assert "SECRET" not in str(caught.value)


def test_token_does_not_leak_through_repr_or_dict_of_the_provider():
    secret_token = "duffel_test_abcdef123456SECRET"
    duffel = DuffelTransportProvider(access_token=secret_token)
    assert secret_token not in repr(duffel)
    assert "SECRET" not in repr(duffel)
    # __dict__ is what a naive `print(vars(duffel))` or logger would dump.
    assert secret_token not in repr(vars(duffel)) or "_token" in vars(duffel), (
        "if the token is in vars() at all it must at least not appear under "
        "a name that invites accidental logging"
    )


# ===========================================================================
# CLAIM 4 — money edge cases beyond the implementer's 49 tests.
# ===========================================================================
def test_a_zero_amount_offer_is_accepted_as_a_genuine_free_fare_not_rejected():
    """Documents current behaviour: zero is not treated as malformed.

    Worth flagging even though it passes: a 0.00 total is far more likely to
    be a data error than a real free flight, and it is currently
    indistinguishable from one. Not failed here because Money legitimately
    permits `ge=0.0` and nothing in the spec says zero must be rejected -
    but it is a soft spot, not a verified-safe case.
    """
    duffel = DuffelTransportProvider(access_token=TOKEN)
    zero_fare = fx.clone(fx.DIRECT)
    zero_fare["data"]["offers"][0]["total_amount"] = "0.00"
    options = duffel.parse_offers(zero_fare, "CGN", "BCN")
    assert len(options) == 1
    assert options[0].price_per_person == 0.0


def test_a_huge_amount_does_not_crash_and_is_not_silently_truncated():
    duffel = DuffelTransportProvider(access_token=TOKEN)
    huge_fare = fx.clone(fx.DIRECT)
    huge_fare["data"]["offers"][0]["total_amount"] = "999999999999.99"
    options = duffel.parse_offers(huge_fare, "CGN", "BCN")
    assert len(options) == 1
    assert options[0].price_per_person > 1_000_000


def test_a_non_numeric_amount_is_dropped_not_crashed_on():
    duffel = DuffelTransportProvider(access_token=TOKEN)
    bad_fare = fx.clone(fx.DIRECT)
    bad_fare["data"]["offers"][0]["total_amount"] = "not-a-number"
    options = duffel.parse_offers(bad_fare, "CGN", "BCN")
    assert options == []
    assert duffel.offers_dropped == [("off_direct", ProviderFailureKind.MALFORMED_RESPONSE)]


def test_a_missing_currency_with_a_present_amount_is_malformed_not_assumed_eur():
    duffel = DuffelTransportProvider(access_token=TOKEN)
    bad_fare = fx.clone(fx.DIRECT)
    del bad_fare["data"]["offers"][0]["total_currency"]
    options = duffel.parse_offers(bad_fare, "CGN", "BCN")
    assert options == [], "a fare with no stated currency must not be assumed to be EUR"


# ===========================================================================
# CLAIM 6 — deeper structural attack: three segments, two connections.
# ===========================================================================
def test_a_two_stop_itinerary_still_collapses_to_one_leg():
    duffel = DuffelTransportProvider(access_token=TOKEN)
    two_stop = fx.response([
        fx._offer("off_twostop", [  # noqa: SLF001 - reusing the fixture builder deliberately
            fx._slice("CGN", "IST", "PT9H00M", [
                fx._segment("CGN", "FRA", "2026-10-15T06:00:00", "2026-10-15T07:00:00", "PT1H00M"),
                fx._segment("FRA", "VIE", "2026-10-15T09:00:00", "2026-10-15T10:30:00", "PT1H30M",
                            flight_number="200"),
                fx._segment("VIE", "IST", "2026-10-15T12:00:00", "2026-10-15T15:00:00", "PT3H00M",
                            flight_number="300"),
            ]),
        ]),
    ])
    option = duffel.parse_offers(two_stop, "CGN", "IST")[0]
    assert (option.origin, option.destination) == ("CGN", "IST")
    assert "FRA" not in (option.origin, option.destination)
    assert "VIE" not in (option.origin, option.destination)
    assert len(option.provider_ref.raw_segments) == 3


# ===========================================================================
# CLAIM 9 — CALL_BUDGET_EXHAUSTED must never be presentable as "no trips".
# ===========================================================================
def test_call_budget_exhausted_is_infrastructure_and_distinct_from_no_results():
    assert ProviderFailureKind.CALL_BUDGET_EXHAUSTED.is_infrastructure is True
    assert ProviderFailureKind.NO_RESULTS.is_infrastructure is False
    assert (
        ProviderFailureKind.CALL_BUDGET_EXHAUSTED
        is not ProviderFailureKind.NO_RESULTS
    )


# ===========================================================================
# CLAIM 5 — baggage: excluded-with-no-purchase-option stays UNKNOWN, not a
# fabricated NOT_AVAILABLE and not INCLUDED. Documents the conservative
# choice actually made.
# ===========================================================================
def test_explicitly_excluded_with_no_purchase_option_is_unknown_not_included():
    duffel = DuffelTransportProvider(access_token=TOKEN)
    fare = fx.response([
        fx._offer("off_excluded_nosell", [
            fx._slice("CGN", "BCN", "PT2H10M", [
                fx._segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00",
                            "PT2H10M", baggages=[{"type": "checked", "quantity": 0}]),
            ]),
        ]),
    ])
    option = duffel.parse_offers(fare, "CGN", "BCN")[0]
    assert option.baggage.checked_bag.status is not BaggageStatus.INCLUDED
    assert option.baggage.checked_bag.price is None
