"""V9 Frontend Reconnection Slice B — backend contract gap: expose the
server-issued ``selection_id`` on a search recommendation so a real,
provider-backed recommendation can create its corresponding real
``BookingIntent`` (§ "SEARCH SELECTION → BOOKING INTENT CONTRACT").

Before this patch, ``live_search()`` generated and persisted a
``SelectionStore`` id for every bookable LIVE recommendation, but the id was
never threaded into the ``/api/v1/search`` response - a pure integration
gap, not a design choice (confirmed by a full trace of
``live_search`` -> ``v1.search`` -> ``assembler.build_response`` ->
``TripRecommendation``). These tests prove:

* the id is threaded through correctly, by *recommendation index* -
  ``live_search`` and ``assembler.build_response`` iterate
  ``result.recommendations`` in the same order, so this is the fix for the
  actual pre-existing id-scheme mismatch between the two (a 0-based
  ``rank`` inside ``live_search`` versus a 1-based client-facing id built
  in ``assembler.py``), not merely a field rename;
* it is never fabricated for a recommendation this system cannot honestly
  book (a leg missing a bookable Duffel offer reference, or any synthetic
  recommendation);
* two recommendations never collide on one id;
* the id, once exposed, resolves through the *existing*, unmodified
  ``/api/v1/booking-intents`` selection lookup to the exact persisted
  selection - this patch adds no new validation there because none was
  needed (see ``tests/test_v8_revalidation.py`` for that machinery's own
  coverage: expiry, unknown-id 404, and the "client cannot supply a price"
  invariant already apply to any ``selection_id``, including one that
  reached the client through this new field).
"""

from __future__ import annotations

import types
from datetime import date, datetime

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from detoura.api import v1  # noqa: E402
from detoura.api.app import create_app  # noqa: E402
from detoura.api.assembler import build_response  # noqa: E402
from detoura.api.contracts import CreateBookingIntentRequest  # noqa: E402
from detoura.data.destinations import DESTINATIONS  # noqa: E402
from detoura.models.itinerary import Itinerary, PlanResult, PlannerMetadata  # noqa: E402
from detoura.models.provider_reference import ProviderOfferReference  # noqa: E402
from detoura.models.transport import TransportOption, TransportType  # noqa: E402
from detoura.models.trip import TravelPreferences, TripRequest  # noqa: E402
from detoura.persistence.db import Database  # noqa: E402
from detoura.providers.transport import SyntheticTransportDataProvider  # noqa: E402
from detoura.search_modes import SearchMode  # noqa: E402
from detoura.services.live_search import live_search  # noqa: E402
from detoura.services.planner import TravelPlanner  # noqa: E402
from detoura.services.selection_store import SelectionStore  # noqa: E402
from detoura.services import live_search as live_search_module  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
def _leg(offer_id: str, *, bookable: bool = True) -> TransportOption:
    return TransportOption(
        id=f"leg_{offer_id}", origin="CGN", destination="BCN",
        departure=datetime(2026, 10, 15, 8, 0), arrival=datetime(2026, 10, 15, 10, 0),
        price_per_person=95.0, transport_type=TransportType.FLIGHT, duration_minutes=120,
        provider_ref=(
            ProviderOfferReference(provider="duffel", offer_id=offer_id) if bookable else None
        ),
    )


def _itinerary(
    rank: int, *, legs, total_cost: float = 190.0, cities: list[str] | None = None,
) -> Itinerary:
    return Itinerary(
        rank=rank, score=0.5, total_cost=total_cost, currency="EUR",
        duration_days=4.0, origin_airport="CGN", return_airport="CGN",
        cities=cities or ["Barcelona"], legs=legs, total_travel_minutes=120,
        departure=datetime(2026, 10, 15, 8, 0), arrival=datetime(2026, 10, 19, 10, 0),
    )


def _live_search_request(**kw) -> TripRequest:
    fields = dict(
        origin="Köln", budget=4000.0, travelers=2, duration_days=4,
        date_from=date(2026, 10, 12), date_to=date(2026, 10, 26),
        preferences=TravelPreferences(history=0.9, culture=0.8),
    )
    fields.update(kw)
    return TripRequest(**fields)


def _run_live_search(monkeypatch, plan_result, *, request=None, portfolio_db=None):
    """Drive the real ``live_search()`` function - including the exact
    selection-recording loop this patch changed - against a canned
    ``PlanResult``, with ``acquire_real_supply`` and ``TravelPlanner``
    faked out. No network: this isolates the one thing being tested, the
    index-keyed recommendation -> selection id binding.

    ``portfolio_db`` (optional): passing a real in-memory ``Database`` lets
    the *real* Recommendation Portfolio reranker (``services.portfolio``)
    run against the canned itineraries - the one place inside
    ``live_search`` that can actually reorder ``result.recommendations``
    before the selection-recording loop below it runs. Omitted (the
    default), that branch is skipped entirely, same as before.
    """
    request = request or _live_search_request()

    def _fake_acquire(_request, **_kwargs):
        return types.SimpleNamespace(
            snapshot=types.SimpleNamespace(
                generated_at=datetime(2026, 10, 1, 12, 0), issues=[],
            ),
        )

    class _FakePlanner:
        def __init__(self, **_kwargs):
            pass

        def plan(self, _request):
            return plan_result

    monkeypatch.setattr(live_search_module, "acquire_real_supply", _fake_acquire)
    monkeypatch.setattr(live_search_module, "TravelPlanner", _FakePlanner)

    store = SelectionStore()
    result = live_search(
        request, duffel=object(), selection_store=store,
        destinations=DESTINATIONS, airports=[], days=[date(2026, 10, 15)],
        portfolio_db=portfolio_db,
    )
    return result, store


# ---------------------------------------------------------------------------
# `live_search()` — the actual generation/keying this patch fixed
# ---------------------------------------------------------------------------
def test_a_real_bookable_recommendation_gets_a_selection_id(monkeypatch):
    plan = PlanResult(
        metadata=PlannerMetadata(origin="Köln"),
        recommendations=[_itinerary(1, legs=[_leg("off_0")])],
    )
    result, store = _run_live_search(monkeypatch, plan)
    assert list(result.selection_ids.keys()) == [0]
    assert store.get(result.selection_ids[0]) is not None


def test_two_bookable_recommendations_never_share_a_selection_id(monkeypatch):
    plan = PlanResult(
        metadata=PlannerMetadata(origin="Köln"),
        recommendations=[
            _itinerary(1, legs=[_leg("off_0")]),
            _itinerary(2, legs=[_leg("off_1")]),
        ],
    )
    result, store = _run_live_search(monkeypatch, plan)
    assert set(result.selection_ids.keys()) == {0, 1}
    id0, id1 = result.selection_ids[0], result.selection_ids[1]
    assert id0 != id1


def test_a_selection_id_binds_to_the_exact_recommendation_not_a_neighbour(monkeypatch):
    """The regression test for the actual bug this patch fixes: the id
    scheme ``live_search`` used to build (``rank`` from its own 0-based
    ``enumerate``) never matched the client-facing id ``assembler.py``
    builds (1-based ``itinerary.rank``). Keying by list position, as this
    patch does on both sides, is what keeps recommendation 0's id from
    ever resolving to recommendation 1's offer."""
    plan = PlanResult(
        metadata=PlannerMetadata(origin="Köln"),
        recommendations=[
            _itinerary(1, legs=[_leg("off_AAA")], total_cost=100.0),
            _itinerary(2, legs=[_leg("off_BBB")], total_cost=200.0),
        ],
    )
    result, store = _run_live_search(monkeypatch, plan)

    selection_0 = store.get(result.selection_ids[0])
    selection_1 = store.get(result.selection_ids[1])
    assert selection_0.offers[0].offer_id == "off_AAA"
    assert selection_1.offers[0].offer_id == "off_BBB"
    assert selection_0.discovered_total == 100.0
    assert selection_1.discovered_total == 200.0


def test_selection_ids_still_bind_correctly_after_real_portfolio_reranking(monkeypatch):
    """Regression test for the exact failure mode an independent review of
    this patch was asked to hunt for: the Recommendation Portfolio reranker
    (``portfolio_db`` branch, ``live_search.py``) is the *only* place inside
    ``live_search`` that can reorder ``result.recommendations`` before the
    selection-recording loop runs. This drives the *real* reranker (not a
    fake) against two itineraries priced so it must actually swap their
    order, then proves the id keyed by post-rerank index still points at
    the right offer - not the itinerary that used to be at that position."""
    plan = PlanResult(
        metadata=PlannerMetadata(origin="Köln"),
        recommendations=[
            _itinerary(1, legs=[_leg("off_expensive")], total_cost=900.0, cities=["Prague"]),
            _itinerary(2, legs=[_leg("off_cheap")], total_cost=100.0, cities=["Barcelona"]),
        ],
    )
    db = Database(":memory:")
    result, store = _run_live_search(monkeypatch, plan, portfolio_db=db)

    final = result.plan_result.recommendations
    # The cheaper trip must actually have moved to the front - otherwise this
    # test is not exercising the reorder path it claims to.
    assert [it.cities[0] for it in final] == ["Barcelona", "Prague"]

    assert store.get(result.selection_ids[0]).offers[0].offer_id == "off_cheap"
    assert store.get(result.selection_ids[1]).offers[0].offer_id == "off_expensive"


def test_a_recommendation_with_an_unbookable_leg_gets_no_selection_id(monkeypatch):
    """A trip this system cannot honestly promise to revalidate/book (any
    leg without a Duffel offer reference) must not receive a selection id
    merely because a neighbouring recommendation did."""
    plan = PlanResult(
        metadata=PlannerMetadata(origin="Köln"),
        recommendations=[
            _itinerary(1, legs=[_leg("off_ok")]),
            _itinerary(2, legs=[_leg("off_missing", bookable=False)]),
        ],
    )
    result, _store = _run_live_search(monkeypatch, plan)
    assert list(result.selection_ids.keys()) == [0]


# ---------------------------------------------------------------------------
# `assembler.build_response()` — threading the map into the response DTO
# ---------------------------------------------------------------------------
def _synthetic_plan_result():
    request = TripRequest(
        origin="Köln", budget=900.0, travelers=2, duration_days=5,
        date_from=date(2026, 9, 10), date_to=date(2026, 9, 24),
        preferred_destinations=["Berlin"],
        preferences=TravelPreferences(history=0.9, food=0.8, culture=0.7),
    )
    planner = TravelPlanner(transport_provider=SyntheticTransportDataProvider())
    return request, planner.plan(request)


def test_build_response_threads_selection_id_by_index():
    request, result = _synthetic_plan_result()
    assert len(result.recommendations) >= 2  # the test needs >1 to prove index-correctness

    response = build_response(
        result, request, types.SimpleNamespace(budget=None), mode=SearchMode.SMART,
        selection_ids={0: "sel_first_only"},
    )
    assert response.recommendations[0].selection_id == "sel_first_only"
    assert all(r.selection_id is None for r in response.recommendations[1:])


def test_build_response_defaults_every_selection_id_to_null():
    """The synthetic path never calls ``live_search``/``selection_store``,
    so it never passes ``selection_ids`` - proving the omission (the
    default) truthfully leaves every recommendation unbookable rather than
    silently reusing whatever the caller last passed."""
    request, result = _synthetic_plan_result()
    response = build_response(
        result, request, types.SimpleNamespace(budget=None), mode=SearchMode.SMART,
    )
    assert all(r.selection_id is None for r in response.recommendations)


def test_the_real_synthetic_search_endpoint_never_exposes_a_selection_id():
    """End to end through the real, unmodified default (no live flag): the
    contract field exists (proves the API shape didn't silently break) but
    is null on every recommendation, since none of them can enter the real
    booking flow."""
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/search",
        json={
            "origin": "Köln", "budget": 900, "travelers": 2, "duration_days": 5,
            "date_from": "2026-09-10", "date_to": "2026-09-24", "date_flexible": True,
            "preferred_destinations": ["Berlin"], "interests": ["culture", "history"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostics"]["supply_source"] == "SYNTHETIC"
    assert body["recommendations"], "fixture must actually produce recommendations"
    assert all(r["selection_id"] is None for r in body["recommendations"])


# ---------------------------------------------------------------------------
# `/api/v1/booking-intents` — the existing, unmodified selection lookup this
# newly-exposed id now reaches. Nothing here changed the endpoint; these
# tests prove the seam this patch built actually lands on solid ground.
# ---------------------------------------------------------------------------
def _seed_selection(**kw):
    fields = dict(
        recommendation_id="rec_0", trip_label="CGN → BCN", currency="EUR",
        discovered_total=190.0, offers=(_selected_offer(),),
    )
    fields.update(kw)
    return v1.selection_store().record(**fields)


def _selected_offer():
    from detoura.services.selection_store import SelectedOffer

    return SelectedOffer(
        offer_id="off_0", provider="duffel", origin="CGN", destination="BCN",
        leg_label="CGN → BCN", travelers=2,
        discovered_amount=190.0, discovered_currency="EUR",
        discovered_raw_amount="190.00", discovered_raw_currency="EUR",
        discovered_departure=datetime(2026, 10, 15, 8, 0),
        discovered_arrival=datetime(2026, 10, 15, 10, 0),
    )


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_a_search_issued_selection_id_creates_the_matching_booking_intent(client):
    sid = _seed_selection()
    response = client.post("/api/v1/booking-intents", json={"selection_id": sid})
    assert response.status_code == 201
    body = response.json()
    assert body["trip_label"] == "CGN → BCN"
    assert body["currency"] == "EUR"
    assert body["discovered_total"] == 190.0


def test_an_unknown_selection_id_fails_safely_not_a_500(client):
    response = client.post("/api/v1/booking-intents", json={"selection_id": "sel_totally_unknown"})
    assert response.status_code == 404


def test_a_malformed_overlong_selection_id_is_rejected_before_lookup(client):
    response = client.post("/api/v1/booking-intents", json={"selection_id": "x" * 500})
    assert response.status_code == 422


def test_an_expired_selection_still_fails_even_though_it_once_existed(monkeypatch, client):
    clock = [0.0]
    store = SelectionStore(ttl_seconds=100, clock=lambda: clock[0])
    monkeypatch.setattr(v1, "selection_store", lambda: store)

    sid = store.record(
        recommendation_id="rec_0", trip_label="CGN → BCN", currency="EUR",
        discovered_total=190.0, offers=(_selected_offer(),),
    )
    clock[0] = 101.0  # past the 100s TTL
    response = client.post("/api/v1/booking-intents", json={"selection_id": sid})
    assert response.status_code == 404


def test_the_booking_intent_request_has_no_field_that_can_override_price_currency_or_itinerary():
    """The exact reason a client-supplied ``selection_id`` is safe to trust:
    there is no sibling field anywhere on this request the client could use
    to override what the server already recorded (V9 §7/§10)."""
    allowed = set(CreateBookingIntentRequest.model_fields)
    assert allowed == {
        "selection_id", "demo_trip_label", "demo_currency", "demo_travelers",
        "demo_legs", "demo_trip_estimate", "service_tier", "promo_code",
    }
    assert "price" not in allowed
    assert "total" not in allowed
    assert "currency" not in allowed  # demo_currency only applies to the demo (no-selection) path
    assert "offer_id" not in allowed
    assert "legs" not in allowed
