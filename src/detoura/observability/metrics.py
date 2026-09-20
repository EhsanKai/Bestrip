"""Minimal in-process metrics for the Limited Beta observability baseline.

A bespoke counter/histogram-sum registry, not a new dependency - this project
already prefers stdlib-only transport code (see ``providers/http.py``'s own
``HttpMetrics``), and pulling in ``prometheus_client`` for a handful of
counters is more than a Limited Beta needs. Exposed as Prometheus text format
by ``GET /metrics`` only when ``DETOURA_METRICS_ENABLED`` is set - see
``api/app.py`` and the observability report's deployment notes for the
network-exposure assumption that endpoint makes.

Label values are restricted to bounded, small-cardinality concepts (method,
status class, mode, provider, operation, outcome, phase, event_type) -
**never** an identifier (booking_id, payment_id, user_id, request_id, a
provider offer id, ...). Putting an identifier in a label is what turns a
metric into an unbounded, per-entity time series; identifiers belong in logs
(see ``logging.py``), not here.
"""

from __future__ import annotations

import threading
from collections import defaultdict

_LabelKey = tuple[tuple[str, str], ...]
_MetricKey = tuple[str, _LabelKey]

_lock = threading.Lock()
_counters: dict[_MetricKey, float] = defaultdict(float)
_histogram_sums: dict[_MetricKey, float] = defaultdict(float)
_histogram_counts: dict[_MetricKey, int] = defaultdict(int)


def _key(name: str, labels: dict[str, str]) -> _MetricKey:
    return name, tuple(sorted(labels.items()))


def incr(name: str, *, value: float = 1.0, **labels: str) -> None:
    try:
        with _lock:
            _counters[_key(name, labels)] += value
    except Exception:
        pass


def observe(name: str, value: float, **labels: str) -> None:
    try:
        with _lock:
            key = _key(name, labels)
            _histogram_sums[key] += value
            _histogram_counts[key] += 1
    except Exception:
        pass


def _status_class(status: int) -> str:
    return f"{status // 100}xx"


def observe_http_request(*, method: str, status: int, duration_ms: float) -> None:
    status_class = _status_class(status)
    incr("http_requests_total", method=method, status_class=status_class)
    if status >= 500:
        incr("http_errors_total", method=method, status_class=status_class)
    observe("http_request_duration_ms", duration_ms, method=method)


def observe_search(*, mode: str, outcome: str, duration_ms: float) -> None:
    incr("search_requests_total", mode=mode, outcome=outcome)
    if outcome == "failed":
        incr("search_failures_total", mode=mode)
    observe("search_duration_ms", duration_ms, mode=mode)


def observe_provider_call(*, provider: str, operation: str, outcome: str, duration_ms: float) -> None:
    incr("provider_requests_total", provider=provider, operation=operation, outcome=outcome)
    if outcome != "ok":
        incr("provider_failures_total", provider=provider, operation=operation, outcome=outcome)
    observe("provider_call_duration_ms", duration_ms, provider=provider, operation=operation)


def observe_payment_transition(*, event_type: str) -> None:
    incr("payment_transitions_total", event_type=event_type)
    if event_type == "RECONCILIATION_REQUIRED":
        incr("payment_reconciliation_required_total")


def observe_booking_outcome(*, phase: str) -> None:
    incr("booking_executions_total", phase=phase)
    if phase == "PARTIAL_FAILURE":
        incr("booking_partial_failure_total")
    if phase == "RECOVERY_REQUIRED":
        incr("booking_recovery_required_total")


def observe_communication_transition(*, provider: str, communication_type: str, outcome: str) -> None:
    """One communication send/resend/reconcile outcome (V9 Production
    Transactional Email §21). Labels are all bounded, small-cardinality
    concepts - ``provider`` ("sandbox"/"resend"), ``communication_type``
    ("BOOKING_CONFIRMATION", ...), ``outcome`` ("sent"/"failed"/"unknown") -
    never a communication_id, booking_id, recipient email, or
    provider_message_id, each of which is a per-entity identifier that would
    turn this into an unbounded time series (see the module docstring)."""
    incr(
        "communication_send_attempts_total",
        provider=provider, communication_type=communication_type, outcome=outcome,
    )
    if outcome == "unknown":
        incr("communication_unknown_total", provider=provider, communication_type=communication_type)
    if outcome == "failed":
        incr("communication_failed_total", provider=provider, communication_type=communication_type)


def _format_metric(name: str, labels: _LabelKey) -> str:
    if not labels:
        return name
    rendered = ",".join(f'{k}="{v}"' for k, v in labels)
    return f"{name}{{{rendered}}}"


def render_prometheus_text() -> str:
    """A minimal, hand-written Prometheus text exposition - no ``TYPE``/``HELP``
    metadata, since this is a Limited Beta baseline, not a full metrics
    library. Good enough for a scraper to parse counters and histogram
    sum/count pairs."""
    lines: list[str] = []
    with _lock:
        for (name, labels), value in sorted(_counters.items()):
            lines.append(f"{_format_metric(name, labels)} {value}")
        for (name, labels), total in sorted(_histogram_sums.items()):
            count = _histogram_counts.get((name, labels), 0)
            lines.append(f"{_format_metric(name + '_sum', labels)} {total}")
            lines.append(f"{_format_metric(name + '_count', labels)} {count}")
    return "\n".join(lines) + "\n"


def reset_for_tests() -> None:
    with _lock:
        _counters.clear()
        _histogram_sums.clear()
        _histogram_counts.clear()
