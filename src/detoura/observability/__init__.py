"""Limited Beta observability baseline: structured logging, request
correlation, and minimal in-process metrics - stdlib only, no new
dependency (matches ``providers/http.py``'s "stdlib only" posture).

See docs/V9_PRODUCTION_OBSERVABILITY_BASELINE_REPORT.md for the full
contract, event schema, and privacy/redaction policy this package implements.
"""

from __future__ import annotations

from .logging import bind_request_id, configure_logging, log_event, new_request_id, sanitize_request_id
from .metrics import render_prometheus_text

__all__ = [
    "bind_request_id",
    "configure_logging",
    "log_event",
    "new_request_id",
    "sanitize_request_id",
    "render_prometheus_text",
]
