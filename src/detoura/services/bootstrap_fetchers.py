"""Concrete :class:`~detoura.services.bootstrap_executor.SourceFetcher`
implementations (V9 Phase 2.5 §7, §8, §37, §38).

Two shapes, both satisfying the same protocol so the executor never branches
on source type:

* :class:`FixtureSourceFetcher` — a ``FILE_IMPORT``/``MANUAL_DATASET``-style
  fetcher: deterministic, offline, no network call ever. This is what the
  controlled E2E demo (§37) and every acquisition test run against, since no
  real authorized external source is configured for Phase 2.5 (§8).
* :class:`SimulatedAuthorizedWebFetcher` — an ``AUTHORIZED_WEB_SOURCE``-style
  fetcher: goes through the real
  :class:`~detoura.services.network_adapter.AuthorizedHttpFetcher` (domain
  allowlist, SSRF guard, size limit, redirect validation, rate limiting, the
  works) against an injected :class:`~detoura.providers.http.HttpClient`.
  Exercising it against a real third-party domain is exactly the "authorized
  web scraping" this project will not do without an explicit, reviewed
  ``APPROVED`` registration (§8, §9) — so every test and the demo point it at
  a local/stub HTTP client instead, which proves the safety controls work
  without ever contacting a real site.

Neither fetcher invents baggage, fare rules, cabin class or refundability —
a field the underlying data does not supply is simply absent (§21).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from .bootstrap_parser import SchemaChanged, TaskContext
from .market_prior_source import stable_hash
from .network_adapter import AuthorizedHttpFetcher


@dataclass(frozen=True, slots=True)
class FixtureSourceFetcher:
    """Deterministic, offline, one record per task — the same stable-hash
    generator :class:`~detoura.services.market_prior_source.FixtureMarketPriorSource`
    uses, reshaped to answer one ``(origin, destination, horizon)`` cell at a
    time instead of a whole market list up front."""

    parser_version: str = "fixture-v1"
    currency: str = "EUR"
    source_date: date | None = None
    #: cells to answer with NO_DATA (a real, honest "nothing here" - §21)
    no_data_cells: frozenset[tuple[str, str, int]] = frozenset()

    def fetch_one(self, task: TaskContext) -> dict | None:
        key = (task.origin, task.destination, task.horizon_days)
        if key in self.no_data_cells:
            return None
        h = stable_hash(task.origin, task.destination)
        base = 4000 + (h % 12000)
        factor = 1.0 + max(0, (60 - task.horizon_days)) / 300.0
        typ = int(base * factor)
        h2 = stable_hash(task.origin, task.destination, str(task.horizon_days))
        return {
            "origin_airport": task.origin,
            "destination_airport": task.destination,
            "horizon_days": task.horizon_days,
            "currency": self.currency,
            "sample_count": 40 + (h2 % 30),
            "observed_low_minor": int(typ * 0.72),
            "median_minor": typ,
            "observed_high_minor": int(typ * 1.55),
            "direct_possible": (h % 3 != 0),
            "weekly_frequency": 7 + (h % 30),
            "carrier_count": 1 + (h % 4),
            "source_date": self.source_date.isoformat() if self.source_date else None,
        }


@dataclass
class SimulatedAuthorizedWebFetcher:
    """An ``AUTHORIZED_WEB_SOURCE`` fetcher exercised only against an injected
    :class:`~detoura.providers.http.HttpClient` — never a real domain unless a
    caller explicitly configures one with a reviewed ``APPROVED``
    registration (§8). Expects each response body to be a small JSON object:
    ``{"origin": "...", "destination": "...", "median": 88.0,
    "currency": "EUR", ...}``. Anything else is a schema-change stop, not a
    best-effort partial parse (§22)."""

    http_fetcher: AuthorizedHttpFetcher
    url_template: str
    """e.g. ``"https://{base}/fares/{origin}/{destination}?days={horizon}"`` —
    the caller supplies the actual host via ``base``; this fetcher never
    hardcodes a target domain."""
    base: str
    parser_version: str = "authorized-web-v1"
    _REQUIRED_KEYS: tuple[str, ...] = field(
        default=("origin", "destination", "median", "currency"), repr=False,
    )

    def fetch_one(self, task: TaskContext) -> dict | None:
        url = self.url_template.format(
            base=self.base, origin=task.origin, destination=task.destination,
            horizon=task.horizon_days,
        )
        response = self.http_fetcher.fetch(url)
        if response.status == 204:
            return None
        try:
            body = json.loads(response.body)
        except (ValueError, TypeError) as exc:
            raise SchemaChanged(f"response body is not valid JSON: {exc}") from exc
        if not isinstance(body, dict) or not all(k in body for k in self._REQUIRED_KEYS):
            raise SchemaChanged(
                f"response is missing expected keys {self._REQUIRED_KEYS}; got {sorted(body) if isinstance(body, dict) else type(body).__name__}"
            )
        if body.get("median") is None:
            return None  # the source answered "no fare known" - honest NO_DATA
        return {
            "origin_airport": str(body["origin"]).upper(),
            "destination_airport": str(body["destination"]).upper(),
            "horizon_days": task.horizon_days,
            "currency": str(body["currency"]).upper(),
            "median": body.get("median"),
            "observed_low": body.get("low"),
            "observed_high": body.get("high"),
            "sample_count": body.get("sample_count"),
            "direct_possible": body.get("direct_possible"),
            "weekly_frequency": body.get("weekly_frequency"),
            "source_date": body.get("source_date"),
        }
