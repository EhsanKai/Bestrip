"""V8 Phase 3: offer revalidation, price tolerance, booking safety.

Search results are discovery data. Booking must run on revalidated data. These
tests defend that boundary: the domain, the comparator, the Duffel read path,
and the endpoint, driven entirely offline through a fake Duffel client.

Nothing here makes a network call.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from detoura.models.baggage import BaggageStatus
from detoura.models.booking import PriceTolerance
from detoura.models.revalidation import (
    NO_INCREASE,
    OfferRevalidationStatus,
    RevalidationStatus,
    absolute_eur,
    percentage,
)
from detoura.providers import duffel as duffel_module
from detoura.providers.duffel import (
    DuffelConfigurationError,
    DuffelOfferGone,
    DuffelTransportProvider,
    OFFER_ID_RE,
)
from detoura.providers.http import HttpResponse, ProviderHttpError, RateLimitExceeded
from detoura.services.revalidation import (
    MAX_OFFERS_PER_REVALIDATION,
    RevalidationLimitExceeded,
    revalidate_selection,
)
from detoura.services.selection_store import SelectedOffer, Selection, SelectionStore

from . import duffel_fixtures as fx

TOKEN = "duffel_test_notarealtoken"
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# A fake Duffel client: answers GET /air/offers/{id} from a per-id script.
# ---------------------------------------------------------------------------
class _FakeDuffelHttp:
    def __init__(self, script: dict):
        """script: offer_id -> ("offer", offer_dict) | ("status", int) | ("raise", exc)."""
        self.script = script
        self.gets: list[str] = []

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        assert method == "GET" and "/air/offers/" in url
        offer_id = url.rsplit("/air/offers/", 1)[1]
        self.gets.append(offer_id)
        action = self.script.get(offer_id, ("status", 404))
        kind, payload = action
        if kind == "raise":
            raise payload
        if kind == "status":
            return HttpResponse(status=payload, body=json.dumps({"errors": [{"code": "x"}]}))
        # a full single-offer envelope
        return HttpResponse(status=200, body=json.dumps({"data": payload}))


def _offer_body(
    offer_id="off_LEG1",
    *,
    total="142.51",
    currency="EUR",
    minutes_to_expiry=20,
    baggages=None,
    requires_instant_payment=True,
    live_mode=False,
):
    o = fx._offer(
        offer_id,
        [fx._slice("CGN", "BCN", "PT2H10M", [
            fx._segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00",
                        "PT2H10M", baggages=baggages),
        ])],
        total_amount=total, total_currency=currency,
        requires_instant_payment=requires_instant_payment,
        live_mode=live_mode,
    )
    if minutes_to_expiry is None:
        o.pop("expires_at", None)
    else:
        o["expires_at"] = (NOW + timedelta(minutes=minutes_to_expiry)).isoformat().replace("+00:00", "Z")
    return o


def _provider(script: dict) -> DuffelTransportProvider:
    return DuffelTransportProvider(access_token=TOKEN, http_client=_FakeDuffelHttp(script),
                                   max_calls=16)


_INCLUDED_BAGS = [{"type": "carry_on", "quantity": 1}, {"type": "checked", "quantity": 1}]


def _selected(
    offer_id="off_LEG1",
    *,
    amount=142.51,
    currency="EUR",
    cabin=BaggageStatus.INCLUDED,
    checked=BaggageStatus.INCLUDED,
    hold=False,
    travelers=1,
    required=True,
):
    return SelectedOffer(
        offer_id=offer_id, provider="duffel", origin="CGN", destination="BCN",
        leg_label="CGN → BCN", travelers=travelers,
        discovered_amount=amount, discovered_currency=currency,
        discovered_raw_amount=str(amount), discovered_raw_currency=currency,
        discovered_baggage_cabin=cabin, discovered_baggage_checked=checked,
        discovered_hold_supported=hold, required=required,
        discovered_departure=datetime(2026, 10, 15, 8, 0),
        discovered_arrival=datetime(2026, 10, 15, 10, 10),
    )


def _selection(offers, *, total=None, currency="EUR"):
    total = sum(o.discovered_amount for o in offers) if total is None else total
    return Selection(
        selection_id="sel_test", recommendation_id="rec_1", trip_label="CGN → BCN",
        currency=currency, discovered_total=total, offers=tuple(offers),
    )


def _revalidate(offers, script, *, tolerance=None):
    return revalidate_selection(
        _selection(offers), duffel=_provider(script), tolerance=tolerance, now=NOW,
    )


# ===========================================================================
# Unchanged
# ===========================================================================
def test_an_unchanged_offer_is_ready():
    r = _revalidate([_selected(checked=BaggageStatus.INCLUDED)],
                    {"off_LEG1": ("offer", _offer_body(baggages=_INCLUDED_BAGS))})
    assert r.status is RevalidationStatus.READY
    assert r.bookable and r.status.may_proceed_to_confirmation
    assert r.offers[0].status is OfferRevalidationStatus.UNCHANGED
    assert r.current_total == pytest.approx(142.51)
    assert r.total_delta == 0.0


# ===========================================================================
# Price up / down / tolerance
# ===========================================================================
def test_a_tiny_price_rise_within_tolerance_is_minor_change():
    r = _revalidate([_selected(amount=403.02, checked=BaggageStatus.INCLUDED)],
                    {"off_LEG1": ("offer", _offer_body(total="404.00", baggages=_INCLUDED_BAGS))},
                    tolerance=absolute_eur(5.0))
    assert r.status is RevalidationStatus.READY_WITH_MINOR_CHANGE
    assert r.status.may_proceed_to_confirmation
    assert r.offers[0].status is OfferRevalidationStatus.PRICE_CHANGED
    assert r.total_delta == pytest.approx(0.98)


def test_a_large_price_rise_beyond_tolerance_requires_reconfirmation():
    r = _revalidate([_selected(amount=403.02, checked=BaggageStatus.INCLUDED)],
                    {"off_LEG1": ("offer", _offer_body(total="469.00", baggages=_INCLUDED_BAGS))},
                    tolerance=absolute_eur(5.0))
    assert r.status is RevalidationStatus.USER_RECONFIRMATION_REQUIRED
    assert not r.status.may_proceed_to_confirmation
    assert r.bookable  # still bookable, just needs consent
    assert r.total_delta == pytest.approx(65.98)


def test_a_price_decrease_is_disclosed_but_still_ready():
    r = _revalidate([_selected(amount=403.02, checked=BaggageStatus.INCLUDED)],
                    {"off_LEG1": ("offer", _offer_body(total="390.00", baggages=_INCLUDED_BAGS))},
                    tolerance=NO_INCREASE)
    assert r.status is RevalidationStatus.READY
    assert r.total_delta == pytest.approx(-13.02)
    change = r.offers[0].changes[0]
    assert change.field == "total_price" and change.severity.value == "INFO"


def test_no_increase_tolerance_blocks_any_rise():
    r = _revalidate([_selected(amount=100.0, checked=BaggageStatus.INCLUDED)],
                    {"off_LEG1": ("offer", _offer_body(total="100.50", baggages=_INCLUDED_BAGS))},
                    tolerance=NO_INCREASE)
    assert r.status is RevalidationStatus.USER_RECONFIRMATION_REQUIRED


def test_percentage_tolerance_scales_with_the_fare():
    # 2% of 1000 = 20 allowed; a 15 rise passes.
    r = _revalidate([_selected(amount=1000.0, checked=BaggageStatus.INCLUDED)],
                    {"off_LEG1": ("offer", _offer_body(total="1015.00", baggages=_INCLUDED_BAGS))},
                    tolerance=percentage(2.0))
    assert r.status is RevalidationStatus.READY_WITH_MINOR_CHANGE


# ===========================================================================
# Expired / unavailable / provider error — kept distinct
# ===========================================================================
def test_a_deleted_offer_is_unavailable_not_zero_or_unknown():
    r = _revalidate([_selected()], {"off_LEG1": ("status", 404)})
    assert r.offers[0].status is OfferRevalidationStatus.UNAVAILABLE
    assert r.offers[0].current_amount is None
    assert r.status is RevalidationStatus.NOT_BOOKABLE
    assert not r.bookable
    assert r.current_total is None


def test_an_expired_offer_reports_expired_distinctly():
    r = _revalidate([_selected()],
                    {"off_LEG1": ("offer", _offer_body(minutes_to_expiry=-5, baggages=_INCLUDED_BAGS))})
    assert r.offers[0].status is OfferRevalidationStatus.EXPIRED
    assert r.status is RevalidationStatus.NOT_BOOKABLE


def test_a_quote_with_no_stated_expiry_is_not_treated_as_fresh():
    r = _revalidate([_selected()],
                    {"off_LEG1": ("offer", _offer_body(minutes_to_expiry=None, baggages=_INCLUDED_BAGS))})
    assert r.status is RevalidationStatus.USER_RECONFIRMATION_REQUIRED
    assert any(c.field == "expiry" for c in r.offers[0].changes)


@pytest.mark.parametrize("action", [
    ("status", 500),
    ("status", 429),
    ("raise", ProviderHttpError("boom")),
    ("raise", RateLimitExceeded("429", status=429)),
    ("raise", TimeoutError("slow")),
])
def test_a_provider_failure_never_falls_back_to_the_snapshot_price(action):
    r = _revalidate([_selected(amount=403.02)], {"off_LEG1": action})
    offer = r.offers[0]
    assert offer.status is OfferRevalidationStatus.PROVIDER_ERROR
    assert offer.current_amount is None, "the discovered price must not be reused"
    assert r.status is RevalidationStatus.NOT_BOOKABLE
    assert not r.bookable


def test_a_malformed_provider_body_fails_safely():
    class _Bad:
        def request(self, *a, **k):
            return HttpResponse(status=200, body="{not json")
    duffel = DuffelTransportProvider(access_token=TOKEN, http_client=_Bad(), max_calls=8)
    r = revalidate_selection(_selection([_selected()]), duffel=duffel, now=NOW)
    assert r.offers[0].status is OfferRevalidationStatus.PROVIDER_ERROR
    assert r.status is RevalidationStatus.NOT_BOOKABLE


# ===========================================================================
# Currency
# ===========================================================================
def test_a_currency_change_is_not_bookable_and_not_a_price_delta():
    r = _revalidate([_selected(amount=403.02, currency="EUR")],
                    {"off_LEG1": ("offer", _offer_body(total="403.02", currency="USD",
                                                       baggages=_INCLUDED_BAGS))})
    assert r.status is RevalidationStatus.NOT_BOOKABLE
    changes = {c.field for c in r.offers[0].changes}
    assert "currency" in changes
    assert "total_price" not in changes, "a currency change must not read as a price move"


# ===========================================================================
# Baggage / terms
# ===========================================================================
def test_baggage_included_to_unknown_is_blocking_even_with_no_price_move():
    r = _revalidate([_selected(checked=BaggageStatus.INCLUDED)],
                    {"off_LEG1": ("offer", _offer_body(baggages=[{"type": "carry_on", "quantity": 1}]))})
    assert r.status is RevalidationStatus.USER_RECONFIRMATION_REQUIRED
    assert r.offers[0].status is OfferRevalidationStatus.TERMS_CHANGED
    assert r.total_delta == 0.0
    downgrade = next(c for c in r.offers[0].changes if c.field == "checked_bag")
    assert downgrade.severity.value == "BLOCKING"


def test_a_price_drop_caused_by_a_baggage_downgrade_is_never_a_saving():
    r = _revalidate([_selected(amount=200.0, checked=BaggageStatus.INCLUDED)],
                    {"off_LEG1": ("offer", _offer_body(total="150.00",
                                                       baggages=[{"type": "carry_on", "quantity": 1}]))},
                    tolerance=NO_INCREASE)
    assert r.status is RevalidationStatus.USER_RECONFIRMATION_REQUIRED
    fields = {c.field for c in r.offers[0].changes}
    assert "checked_bag" in fields


def test_hold_withdrawn_requires_reconfirmation():
    r = _revalidate([_selected(hold=True, checked=BaggageStatus.INCLUDED)],
                    {"off_LEG1": ("offer", _offer_body(requires_instant_payment=True,
                                                       baggages=_INCLUDED_BAGS))})
    assert r.status is RevalidationStatus.USER_RECONFIRMATION_REQUIRED
    assert any(c.field == "hold" and c.severity.value == "BLOCKING" for c in r.offers[0].changes)


def test_hold_becoming_available_is_disclosed_not_blocking():
    r = _revalidate([_selected(hold=False, checked=BaggageStatus.INCLUDED)],
                    {"off_LEG1": ("offer", _offer_body(requires_instant_payment=False,
                                                       baggages=_INCLUDED_BAGS))})
    assert r.status is RevalidationStatus.READY
    assert any(c.field == "hold" and c.severity.value == "INFO" for c in r.offers[0].changes)


# ===========================================================================
# Multi-leg — the invariant
# ===========================================================================
def test_two_valid_and_one_unavailable_is_not_bookable():
    offers = [_selected("off_A", checked=BaggageStatus.INCLUDED),
              _selected("off_B", checked=BaggageStatus.INCLUDED),
              _selected("off_C", checked=BaggageStatus.INCLUDED)]
    script = {
        "off_A": ("offer", _offer_body("off_A", baggages=_INCLUDED_BAGS)),
        "off_B": ("offer", _offer_body("off_B", baggages=_INCLUDED_BAGS)),
        "off_C": ("status", 404),
    }
    r = _revalidate(offers, script)
    assert r.status is RevalidationStatus.NOT_BOOKABLE
    assert not r.bookable
    assert [o.status for o in r.offers] == [
        OfferRevalidationStatus.UNCHANGED,
        OfferRevalidationStatus.UNCHANGED,
        OfferRevalidationStatus.UNAVAILABLE,
    ]
    assert r.current_total is None


def test_partial_multi_leg_price_changes_aggregate_to_the_strictest():
    offers = [_selected("off_A", amount=100.0, checked=BaggageStatus.INCLUDED),
              _selected("off_B", amount=100.0, checked=BaggageStatus.INCLUDED)]
    script = {
        "off_A": ("offer", _offer_body("off_A", total="101.00", baggages=_INCLUDED_BAGS)),
        "off_B": ("offer", _offer_body("off_B", total="180.00", baggages=_INCLUDED_BAGS)),
    }
    r = _revalidate(offers, script, tolerance=absolute_eur(5.0))
    assert r.status is RevalidationStatus.USER_RECONFIRMATION_REQUIRED  # leg B blew tolerance


# ===========================================================================
# Bounded / safety
# ===========================================================================
def test_more_offers_than_the_ceiling_is_refused():
    offers = [_selected(f"off_{i}") for i in range(MAX_OFFERS_PER_REVALIDATION + 1)]
    with pytest.raises(RevalidationLimitExceeded):
        revalidate_selection(_selection(offers), duffel=_provider({}), now=NOW)


def test_the_offer_id_shape_is_validated_before_a_url_is_built():
    assert OFFER_ID_RE.match("off_0000BADSEwWhjmCpAdMZTm")
    for bad in ["../etc/passwd", "off_x/../../y", "http://evil", "off_x?a=b", ""]:
        assert not OFFER_ID_RE.match(bad)
    duffel = _provider({})
    with pytest.raises(DuffelConfigurationError):
        duffel.get_offer("../air/offer_requests")


def test_a_fake_live_mode_revalidation_response_is_refused():
    r = _revalidate([_selected()],
                    {"off_LEG1": ("offer", _offer_body(live_mode=True, baggages=_INCLUDED_BAGS))})
    assert r.offers[0].status is OfferRevalidationStatus.PROVIDER_ERROR
    assert r.status is RevalidationStatus.NOT_BOOKABLE


def test_the_token_never_appears_in_a_revalidation_error():
    secret = "duffel_test_SECRETzzz"
    duffel = DuffelTransportProvider(
        access_token=secret,
        http_client=_FakeDuffelHttp({"off_LEG1": ("status", 500)}), max_calls=8,
    )
    r = revalidate_selection(_selection([_selected()]), duffel=duffel, now=NOW)
    assert "SECRET" not in r.offers[0].detail
    assert "SECRET" not in json.dumps(r.model_dump(), default=str)


def test_get_offer_counts_against_the_hard_call_ceiling():
    duffel = DuffelTransportProvider(
        access_token=TOKEN,
        http_client=_FakeDuffelHttp({f"off_{i}": ("offer", _offer_body(f"off_{i}"))
                                     for i in range(10)}),
        max_calls=3,
    )
    for i in range(3):
        duffel.get_offer(f"off_{i}")
    with pytest.raises(Exception):
        duffel.get_offer("off_3")


# ===========================================================================
# Serialization honesty
# ===========================================================================
def test_the_result_keeps_discovered_and_current_apart_through_serialization():
    r = _revalidate([_selected(amount=403.02, checked=BaggageStatus.INCLUDED)],
                    {"off_LEG1": ("offer", _offer_body(total="417.50", baggages=_INCLUDED_BAGS))},
                    tolerance=absolute_eur(50.0))
    body = json.loads(r.model_dump_json())
    assert body["discovered_total"] == 403.02
    assert body["current_total"] == 417.5
    assert body["offers"][0]["discovered_amount"] == 403.02
    assert body["offers"][0]["current_amount"] == 417.5


def test_an_unpriced_total_serializes_as_null_not_zero():
    r = _revalidate([_selected()], {"off_LEG1": ("status", 404)})
    body = json.loads(r.model_dump_json())
    assert body["current_total"] is None
    assert body["offers"][0]["current_amount"] is None


# ===========================================================================
# Selection store
# ===========================================================================
def test_a_selection_expires_and_then_reads_as_unknown():
    clock = [0.0]
    store = SelectionStore(ttl_seconds=100, clock=lambda: clock[0])
    sid = store.record(recommendation_id="r", trip_label="t", currency="EUR",
                       discovered_total=1.0, offers=(_selected(),))
    assert store.get(sid) is not None
    clock[0] = 101.0
    assert store.get(sid) is None


def test_the_selection_store_is_size_bounded():
    store = SelectionStore(max_selections=3, clock=lambda: 0.0)
    ids = [store.record(recommendation_id=str(i), trip_label="t", currency="EUR",
                        discovered_total=1.0, offers=(_selected(),)) for i in range(6)]
    assert len(store) == 3
    assert store.get(ids[0]) is None and store.get(ids[-1]) is not None


# ===========================================================================
# Endpoint
# ===========================================================================
def test_the_endpoint_rejects_an_unknown_selection_id():
    from fastapi.testclient import TestClient
    from detoura.api.app import create_app

    client = TestClient(create_app())
    r = client.post("/api/v1/trips/revalidate", json={"selection_id": "sel_nope"})
    assert r.status_code == 404


def test_the_endpoint_503s_without_a_sandbox_token(monkeypatch):
    from fastapi.testclient import TestClient
    from detoura.api.app import create_app
    from detoura.services.selection_store import selection_store

    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    sid = selection_store().record(
        recommendation_id="r", trip_label="t", currency="EUR",
        discovered_total=1.0, offers=(_selected(),),
    )
    client = TestClient(create_app())
    r = client.post("/api/v1/trips/revalidate", json={"selection_id": sid})
    assert r.status_code == 503


def test_the_endpoint_never_accepts_a_client_supplied_price_or_offer_id():
    from detoura.api.contracts import RevalidateRequest

    allowed = set(RevalidateRequest.model_fields)
    assert allowed == {"selection_id", "tolerance_absolute", "tolerance_percentage"}
    assert "offer_id" not in allowed and "discovered_total" not in allowed
