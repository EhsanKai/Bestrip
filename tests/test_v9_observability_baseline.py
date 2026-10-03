"""V9 Limited Beta observability baseline.

Targets the new instrumentation only - structured logging, request
correlation, minimal metrics, health/readiness, and the privacy/redaction
policy around all of it. Does not re-derive what the Phase 6 PII/logging
adversarial slice already pins (test_v9_phase6_pii_security.py's caplog
tests on the 5 pre-existing logger call sites) - this file's own privacy
tests attack the NEW call sites this baseline adds.
"""

from __future__ import annotations

import logging
from datetime import timezone, datetime

import pytest

from detoura.models.commercial import CommercialQuote, PriceBreakdown, PricingPolicyRef, ServiceTier
from detoura.models.travel_pass import PassMode
from detoura.observability import logging as obs_logging
from detoura.observability import metrics as obs_metrics
from detoura.observability.logging import log_event, sanitize_request_id
from detoura.persistence.db import Database
from detoura.providers.http import ProviderHttpError, RetryingHttpClient
from detoura.providers.payment_provider import ProviderResult
from detoura.services import payment_service as ps
from detoura.services.booking_orchestrator import BookingPhase, BookingRun, ItemProgress, _log_booking_outcome


def _client(tmp_path, monkeypatch, db_name="obs.db"):
    from fastapi.testclient import TestClient

    import detoura.persistence.db as _db
    from detoura.api.app import create_app

    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / db_name))
    monkeypatch.setattr(_db, "_DB", None)
    return TestClient(create_app())


def _quote(total: float = 100.0) -> CommercialQuote:
    return CommercialQuote(
        service_tier=ServiceTier.BASIC,
        breakdown=PriceBreakdown(currency="EUR", supplier_transport=total),
        markup_policy=PricingPolicyRef(policy_id="default", version=1),
    )


def _authorized_payment(db, provider, *, booking_id="bk_obs", idem="idem_obs_1"):
    snap = ps.freeze_checkout_snapshot(
        db, booking_id=booking_id, journey_reference=f"jr_{booking_id}", user_id=None,
        service_tier="BASIC", quote=_quote(),
    )
    payment, _ = ps.create_payment(db, snapshot=snap, provider_name=provider.name, idempotency_key=idem)
    return payment


# ======================================================================
# 1-4: request id generation, propagation, sanitization, and echo
# ======================================================================
def test_request_id_generated_when_absent(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/readyz")
    assert r.status_code == 200
    request_id = r.headers.get("x-request-id")
    assert request_id and obs_logging._REQUEST_ID_RE.match(request_id)


def test_valid_request_id_propagates_unchanged(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/readyz", headers={"X-Request-Id": "caller-chosen-id-123"})
    assert r.headers.get("x-request-id") == "caller-chosen-id-123"


def test_oversized_or_unsafe_request_id_is_replaced_not_echoed(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    hostile = "x" * 5000
    r = client.get("/readyz", headers={"X-Request-Id": hostile})
    got = r.headers.get("x-request-id")
    assert got is not None
    assert got != hostile
    assert len(got) <= 128

    # CRLF-shaped input specifically - header/log injection, not just length.
    assert sanitize_request_id("evil\r\nSet-Cookie: x=y") != "evil\r\nSet-Cookie: x=y"


# ======================================================================
# 5: structured request event emitted
# ======================================================================
def test_request_completed_event_is_structured(tmp_path, monkeypatch, caplog):
    client = _client(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO, logger="detoura.request"):
        client.get("/readyz")
    records = [r for r in caplog.records if getattr(r, "event", None) == "request_completed"]
    assert records
    record = records[0]
    assert record.fields["method"] == "GET"
    assert record.fields["route"] == "/readyz"
    assert record.fields["status_code"] == 200
    assert "duration_ms" in record.fields


# ======================================================================
# 6-7: unexpected exception is correlated, client gets no stack trace
# ======================================================================
def test_unhandled_exception_is_correlated_and_hides_stack_trace(tmp_path, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    import detoura.persistence.db as _db
    from detoura.api.app import create_app

    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "obs_exc.db"))
    monkeypatch.setattr(_db, "_DB", None)
    # raise_server_exceptions=False: a real ASGI server (uvicorn) does not
    # care that Starlette's ServerErrorMiddleware re-raises after sending
    # the handler's response - it already went out on the wire. TestClient's
    # default DOES re-raise into the test, which would test TestClient's
    # plumbing instead of what an actual client receives.
    client = TestClient(create_app(), raise_server_exceptions=False)

    def _boom():
        raise RuntimeError("boom - internal detail that must not reach the client")

    monkeypatch.setattr("detoura.api.payments.get_db", _boom)
    with caplog.at_level(logging.ERROR, logger="detoura.api"):
        r = client.get("/api/v1/payments/pay_does_not_matter")
    assert r.status_code == 500
    body = r.json()
    assert "boom" not in r.text
    assert "Traceback" not in r.text
    assert "request_id" in body["detail"]
    assert body["detail"]["request_id"] == r.headers.get("x-request-id")

    events = [rec for rec in caplog.records if getattr(rec, "event", None) == "unhandled_exception"]
    assert events


# ======================================================================
# 8-10: sensitive headers/cookies/CSRF never logged
# ======================================================================
def test_authorization_and_cookie_headers_never_appear_in_request_logs(tmp_path, monkeypatch, caplog):
    client = _client(tmp_path, monkeypatch)
    secret_auth = "Bearer sk_super_secret_token"
    secret_cookie = "detoura_session=super-secret-session-value"
    with caplog.at_level(logging.INFO):
        client.get(
            "/readyz",
            headers={"Authorization": secret_auth, "Cookie": secret_cookie},
        )
    log_text = "\n".join(r.getMessage() for r in caplog.records) + "\n".join(
        str(getattr(r, "fields", {})) for r in caplog.records
    )
    assert "sk_super_secret_token" not in log_text
    assert "super-secret-session-value" not in log_text


def test_csrf_token_never_appears_in_logs(tmp_path, monkeypatch, caplog):
    client = _client(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO):
        client.post(
            "/api/v1/payments/pay_x/confirm",
            headers={"X-CSRF-Token": "top-secret-csrf-value"},
        )
    log_text = "\n".join(str(getattr(r, "fields", {})) for r in caplog.records)
    assert "top-secret-csrf-value" not in log_text


# ======================================================================
# 11: payment secrets are never logged - forbidden-field filtering
# ======================================================================
def test_log_event_drops_forbidden_fields_defensively(caplog):
    logger = logging.getLogger("detoura.test.redaction")
    with caplog.at_level(logging.INFO, logger="detoura.test.redaction"):
        log_event(
            logger, "test_event",
            payment_id="pay_123",  # safe identifier - kept
            client_secret="sk_live_should_never_appear",
            password="hunter2",
        )
    record = next(r for r in caplog.records if getattr(r, "event", None) == "test_event")
    assert record.fields["payment_id"] == "pay_123"
    assert "client_secret" not in record.fields
    assert "password" not in record.fields


def test_stripe_provider_call_logging_never_includes_secret_or_body(caplog):
    from detoura.providers.stripe_payment import StripePaymentProvider

    class _FakeHttp:
        def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
            from detoura.providers.http import HttpResponse
            assert "sk_test_should_not_be_logged" in headers.get("Authorization", "")
            return HttpResponse(status=200, body='{"id": "pi_123", "status": "requires_action"}')

    provider = StripePaymentProvider(secret_key="sk_test_should_not_be_logged", http=_FakeHttp())
    with caplog.at_level(logging.INFO, logger="detoura.providers.stripe_payment"):
        provider._post("/payment_intents", idempotency_key="idem1", data={"amount": "100"})
    log_text = "\n".join(str(getattr(r, "fields", {})) for r in caplog.records)
    assert "sk_test_should_not_be_logged" not in log_text
    assert "amount" not in log_text


# ======================================================================
# 12: TravelerParty / traveler fields never in booking observability logs
# ======================================================================
def test_booking_intent_created_log_carries_no_traveler_data(caplog):
    from detoura.services.booking_flow import create_run_demo

    with caplog.at_level(logging.INFO, logger="detoura.services.booking_flow"):
        create_run_demo(
            trip_label="T", currency="EUR",
            legs=[{
                "origin": "CGN", "destination": "PRG",
                "departure": "2026-01-01T10:00:00", "arrival": "2026-01-01T11:00:00",
                "price_per_person": 50.0,
            }],
        )
    record = next(r for r in caplog.records if getattr(r, "event", None) == "booking_intent_created")
    assert set(record.fields.keys()) == {"booking_id", "mode", "item_count"}


# ======================================================================
# 13-14: health endpoint
# ======================================================================
def test_health_endpoint_works(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_endpoint_never_touches_the_database(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    def _boom():
        raise RuntimeError("the database must never be touched by /health")

    monkeypatch.setattr("detoura.persistence.get_db", _boom)
    r = client.get("/health")
    assert r.status_code == 200


# ======================================================================
# 15: readiness semantics
# ======================================================================
def test_readyz_ready_when_db_reachable(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ready"}


def test_readyz_not_ready_when_db_unreachable(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("detoura.api.app.get_db", _boom)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready"}


# ======================================================================
# 16: payment UNKNOWN / reconciliation-required event observable
# ======================================================================
def test_payment_unknown_authorize_emits_reconciliation_required_event(caplog):
    from detoura.providers.sandbox_payment import SandboxPaymentProvider

    db = Database(":memory:")
    provider = SandboxPaymentProvider()
    payment = _authorized_payment(db, provider, booking_id="bk_unknown")
    result = ProviderResult(ok=False, unknown=True, provider_reference=None, status="unknown", detail="timed out")
    with caplog.at_level(logging.INFO, logger="detoura.services.payment_service"):
        ps._apply_authorize_result(db, payment=payment, result=result, now=datetime.now(timezone.utc))
    events = {getattr(r, "event", None) for r in caplog.records}
    assert "payment_reconciliation_required" in events


# ======================================================================
# 17: booking recovery / partial-failure event observable
# ======================================================================
def _item(item_id: str, *, required: bool = True) -> ItemProgress:
    now = datetime.now(timezone.utc)
    return ItemProgress(
        item_id=item_id, origin_city="BER", origin_airport="BER",
        destination_city="LHR", destination_airport="LHR",
        departure=now, arrival=now, carrier="XY", flight_number="123",
        offer_id=f"off_{item_id}", provider="duffel", travelers=1,
        quoted_price=100.0, currency="EUR", required=required,
    )


def test_booking_partial_failure_outcome_is_observable(caplog):
    from detoura.models.booking import PriceTolerance

    run = BookingRun(
        booking_id="bk_partial", journey_reference="jr_bk_partial", mode=PassMode.SANDBOX_BOOKED,
        trip_label="test", route_cities=("BER", "LHR"), currency="EUR", discovered_total=200.0,
        tolerance=PriceTolerance(), items=[_item("leg-1")], phase=BookingPhase.PARTIAL_FAILURE,
    )
    with caplog.at_level(logging.INFO, logger="detoura.services.booking_orchestrator"):
        _log_booking_outcome(run)
    events = {getattr(r, "event", None) for r in caplog.records}
    assert "booking_partial_failure" in events


# ======================================================================
# 18: provider failure event observable, with outcome classification
# ======================================================================
class _AlwaysTimesOut:
    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        raise ProviderHttpError(f"{url} timed out after {timeout}s")


def test_provider_timeout_is_observable_and_classified(caplog):
    client = RetryingHttpClient(_AlwaysTimesOut(), max_retries=0, provider="duffel", sleep=lambda s: None)
    with caplog.at_level(logging.INFO, logger="detoura.providers.http"):
        with pytest.raises(ProviderHttpError):
            client.request("GET", "https://duffel.example/air/offers")
    record = next(r for r in caplog.records if getattr(r, "event", None) == "provider_request_completed")
    assert record.fields["provider"] == "duffel"
    assert record.fields["outcome"] == "timeout"
    # Never the full URL/body - only the host, and never headers.
    assert record.fields["host"] == "duffel.example"


# ======================================================================
# 19: metric labels avoid high-cardinality identifiers
# ======================================================================
def test_metrics_never_carry_identifier_shaped_labels():
    obs_metrics.reset_for_tests()
    obs_metrics.observe_http_request(method="GET", status=200, duration_ms=12.5)
    obs_metrics.observe_payment_transition(event_type="PAYMENT_CREATED")
    obs_metrics.observe_booking_outcome(phase="COMPLETE")
    obs_metrics.observe_provider_call(provider="duffel", operation="GET", outcome="ok", duration_ms=5.0)
    text = obs_metrics.render_prometheus_text()
    for forbidden in ("payment_id", "booking_id", "user_id", "request_id", "selection_id"):
        assert forbidden not in text
    obs_metrics.reset_for_tests()


# ======================================================================
# 20: observability failure never changes transactional outcome
# ======================================================================
def test_broken_logger_does_not_break_payment_creation(monkeypatch):
    from detoura.providers.sandbox_payment import SandboxPaymentProvider

    def _broken_log(*args, **kwargs):
        raise RuntimeError("logging backend is on fire")

    monkeypatch.setattr(logging.Logger, "log", _broken_log)

    db = Database(":memory:")
    provider = SandboxPaymentProvider()
    snap = ps.freeze_checkout_snapshot(
        db, booking_id="bk_resilient", journey_reference="jr_resilient", user_id=None,
        service_tier="BASIC", quote=_quote(),
    )
    payment, created = ps.create_payment(
        db, snapshot=snap, provider_name=provider.name, idempotency_key="idem_resilient_1",
    )
    assert created is True
    assert payment.payment_id


def test_broken_metrics_do_not_raise(monkeypatch):
    obs_metrics.reset_for_tests()
    monkeypatch.setattr(obs_metrics, "_counters", None)  # any counter write now raises
    obs_metrics.incr("whatever_total")  # must swallow the error, not propagate
    obs_metrics.observe("whatever_ms", 1.0)
    # monkeypatch restores the real `_counters` dict on teardown automatically.


# ======================================================================
# 21: V9 staging/production ops readiness - non-production deployments
# fail closed to noindex (docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md)
# ======================================================================
def test_non_production_deployment_is_noindexed_by_default(tmp_path, monkeypatch):
    from detoura import auth_config as auth_config_mod

    auth_config_mod.reset_auth_config()
    monkeypatch.delenv("DETOURA_ENV", raising=False)
    monkeypatch.delenv("DETOURA_ENV_PRODUCTION", raising=False)
    auth_config_mod.reset_auth_config()
    try:
        client = _client(tmp_path, monkeypatch, db_name="noindex_default.db")
        r = client.get("/readyz")
        assert r.headers.get("x-robots-tag") == "noindex, nofollow"
    finally:
        auth_config_mod.reset_auth_config()


def test_production_deployment_is_not_noindexed(tmp_path, monkeypatch):
    from detoura import auth_config as auth_config_mod

    auth_config_mod.reset_auth_config()
    monkeypatch.setenv("DETOURA_ENV", "production")
    auth_config_mod.reset_auth_config()
    try:
        client = _client(tmp_path, monkeypatch, db_name="noindex_prod.db")
        r = client.get("/readyz")
        assert r.headers.get("x-robots-tag") is None
    finally:
        auth_config_mod.reset_auth_config()
