"""Provider-neutral Bootstrap Market Prior sources (V9 Phase 2 §8).

A :class:`MarketPriorSource` yields provider-neutral :class:`RawPriorRecord`
dicts; :mod:`detoura.services.market_prior_import` normalizes, validates and
persists them. Acquisition logic is never coupled to a concrete source.

Phase 2 ships:

* :class:`FixtureMarketPriorSource` — a deterministic in-memory generator for
  tests and the algorithm benchmarks;
* :class:`JsonMarketPriorSource` / :class:`CsvMarketPriorSource` — read an
  operator-provided, explicitly-approved dataset from disk.

A future authorized external-API adapter drops in behind the same protocol.
**No source here scrapes a consumer website, bypasses bot protection, or
reverse-engineers a private API** (V9 §7). If no legitimate live source is
configured the architecture still stands and validates on fixture data — see
``docs/V9_MARKET_PRIOR_SOURCE_OPTIONS.md``.
"""

from __future__ import annotations

import csv
import json
import zlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, Protocol


def _stable_hash(*parts: str) -> int:
    """A hash that is stable across processes and Python runs.

    Builtin ``hash()`` on ``str``/``tuple`` is salted per-process
    (``PYTHONHASHSEED`` randomization) — two runs of the same fixture would
    silently generate different synthetic prices, which contradicts the
    "deterministic" contract this module documents and undermines any
    benchmark or test that compares fixture output across process runs.
    ``zlib.crc32`` has no such salting."""
    return zlib.crc32("|".join(parts).encode("utf-8"))

#: A raw, provider-neutral prior record. Keys are all optional except the
#: market identity + currency; the import pipeline validates and buckets.
RawPriorRecord = dict


@dataclass(frozen=True, slots=True)
class SourceMeta:
    source: str
    source_version: str = ""
    source_date: date | None = None
    #: external-source cost/limits, all optional -> UNKNOWN downstream
    requests_made: int | None = None
    request_cost_minor: int | None = None
    rate_limit_events: int | None = None


class MarketPriorSource(Protocol):
    """Yields raw prior records and describes itself."""

    def meta(self) -> SourceMeta: ...

    def records(self) -> Iterable[RawPriorRecord]: ...


# ======================================================================
# Fixture source — deterministic, offline
# ======================================================================
@dataclass(frozen=True, slots=True)
class FixtureMarketPriorSource:
    """A deterministic synthetic prior. Given a set of (origin, destination)
    markets it emits one record per horizon bucket with a stable pseudo-price
    derived from a hash of the market — enough structure for the scoring and
    benchmark tests, honestly labelled as fixture data."""

    markets: tuple[tuple[str, str], ...]
    horizon_days: tuple[int, ...] = (14, 30, 45, 60, 90, 120)
    source_date: date | None = None
    version: str = "fixture-1"
    #: markets to deliberately give a *cheap but thin* prior (few samples) —
    #: for the "cheapest does not always win" test
    thin_cheap: frozenset[tuple[str, str]] = frozenset()
    #: markets to omit entirely (UNKNOWN)
    omit: frozenset[tuple[str, str]] = frozenset()
    currency: str = "EUR"

    def meta(self) -> SourceMeta:
        return SourceMeta(source="fixture", source_version=self.version,
                          source_date=self.source_date, requests_made=len(self.markets))

    def records(self) -> Iterator[RawPriorRecord]:
        for (o, d) in self.markets:
            if (o, d) in self.omit:
                continue
            h_od = _stable_hash(o, d)
            base = 4000 + (h_od % 12000)  # 40..160 EUR-ish, minor units
            thin = (o, d) in self.thin_cheap
            if thin:
                base = 2500 + (h_od % 1500)  # cheap
            for hd in self.horizon_days:
                # earlier booking a touch cheaper, deterministic
                factor = 1.0 + max(0, (60 - hd)) / 300.0
                typ = int(base * factor)
                h_odh = _stable_hash(o, d, str(hd))
                yield {
                    "origin_airport": o,
                    "destination_airport": d,
                    "horizon_days": hd,
                    "currency": self.currency,
                    "sample_count": 6 if thin else 40 + (h_odh % 30),
                    "observed_low_minor": int(typ * 0.72),
                    "median_minor": typ,
                    "observed_high_minor": int(typ * 1.55),
                    "direct_possible": (h_od % 3 != 0),
                    "weekly_frequency": 3 if thin else 7 + (h_od % 30),
                    "carrier_count": 1 if thin else 1 + (h_od % 4),
                    "source_date": self.source_date.isoformat() if self.source_date else None,
                }


# ======================================================================
# File sources — operator-provided approved datasets
# ======================================================================
@dataclass(frozen=True, slots=True)
class JsonMarketPriorSource:
    """Reads a JSON file: ``{"source": ..., "source_version": ...,
    "source_date": "YYYY-MM-DD", "records": [ {..}, ... ]}``. Each record is a
    :data:`RawPriorRecord`."""

    path: str

    def _doc(self) -> dict:
        return json.loads(Path(self.path).read_text())

    def meta(self) -> SourceMeta:
        d = self._doc()
        sd = d.get("source_date")
        return SourceMeta(
            source=str(d.get("source", "json")),
            source_version=str(d.get("source_version", "")),
            source_date=date.fromisoformat(sd) if sd else None,
            requests_made=d.get("source_requests"),
            request_cost_minor=d.get("source_request_cost_minor"),
            rate_limit_events=d.get("source_rate_limit_events"),
        )

    def records(self) -> Iterator[RawPriorRecord]:
        yield from self._doc().get("records", [])


@dataclass(frozen=True, slots=True)
class CsvMarketPriorSource:
    """Reads a CSV whose header names the :data:`RawPriorRecord` fields.
    ``source`` / ``source_version`` / ``source_date`` are passed to the
    constructor since a CSV has no envelope."""

    path: str
    source: str = "csv"
    source_version: str = ""
    source_date: date | None = None

    def meta(self) -> SourceMeta:
        return SourceMeta(source=self.source, source_version=self.source_version,
                          source_date=self.source_date)

    def records(self) -> Iterator[RawPriorRecord]:
        with open(self.path, newline="") as f:
            for row in csv.DictReader(f):
                yield {k: (v if v != "" else None) for k, v in row.items()}
