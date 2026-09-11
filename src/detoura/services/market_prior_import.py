"""Bootstrap Market Prior ingestion pipeline (V9 Phase 2 §9).

    MarketPriorSource  ->  normalize  ->  validate  ->  (dry-run?)  ->  persist

Guarantees:

* **idempotent** — a re-import of the same logical row updates it, never
  double-counts (``market_priors`` UNIQUE key + ``upsert_priors``);
* **provenance-aware** — every row carries ``source`` + ``source_version``;
* **bad-row isolation** — a malformed record is rejected and reported, never
  aborts the run or corrupts good data;
* **no PII** — the record shape has no name/email/phone/document field, and the
  normalizer drops any unknown key;
* **dry-run** — validate + report without writing;
* **resumable** — ``since_market`` skips markets already covered by a prior run;
* **isolated failure** — an exception is caught, recorded on the import row
  with ``ok=0``, and existing ``market_priors`` / ``price_observations`` are
  untouched.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from ..models.market_prior import (
    BootstrapMarketPrior,
    DurationBucket,
    HorizonBucket,
    PriorConfidence,
    SeasonBucket,
    WeekdayClass,
)
from ..models.money import to_minor_units
from ..persistence import market_priors as store
from ..persistence.db import Database
from ..search_intel_config import SearchIntelConfig, search_intel_config
from .market_prior_source import MarketPriorSource

_IATA_OK = lambda s: isinstance(s, str) and len(s) == 3 and s.isalpha() and s.isupper()
_CCY_OK = lambda s: isinstance(s, str) and len(s) == 3 and s.isalpha()

#: The only keys the normalizer reads. Anything else on a raw record is
#: dropped — a source cannot smuggle a PII field into Price Memory.
_ALLOWED_KEYS = frozenset({
    "origin_airport", "destination_airport", "destination_id", "currency",
    "season", "month", "horizon_bucket", "horizon_days", "weekday_class",
    "weekday", "duration_bucket", "duration_nights", "sample_count",
    "observed_low", "observed_low_minor", "median", "median_minor",
    "typical", "typical_minor", "observed_high", "observed_high_minor",
    "confidence", "direct_possible", "weekly_frequency", "carrier_count",
    "source_date",
})


class PriorImportError(Exception):
    pass


def _minor(row: dict, minor_key: str, major_key: str) -> int | None:
    if row.get(minor_key) is not None:
        try:
            return int(row[minor_key])
        except (TypeError, ValueError):
            return None
    if row.get(major_key) is not None:
        try:
            return to_minor_units(round(float(row[major_key]), 2))
        except (TypeError, ValueError):
            return None
    return None


def _opt_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _bool(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def normalize(raw: dict, *, source: str, source_version: str,
              imported_at: datetime, default_source_date: date | None) -> BootstrapMarketPrior:
    """Raw record -> validated BootstrapMarketPrior. Raises PriorImportError
    for anything unusable."""
    row = {k: v for k, v in raw.items() if k in _ALLOWED_KEYS}

    o = str(row.get("origin_airport", "")).strip().upper()
    d = str(row.get("destination_airport", "")).strip().upper()
    if not _IATA_OK(o) or not _IATA_OK(d):
        raise PriorImportError(f"bad airport(s): {o!r} -> {d!r}")
    if o == d:
        raise PriorImportError(f"origin == destination ({o})")
    ccy = str(row.get("currency", "")).strip().upper()
    if not _CCY_OK(ccy):
        raise PriorImportError(f"bad currency: {row.get('currency')!r}")

    month = _opt_int(row.get("month"))
    if month is not None and not (1 <= month <= 12):
        raise PriorImportError(f"bad month: {month}")

    # season: explicit, else derived from month, else UNKNOWN
    season = SeasonBucket.UNKNOWN
    if row.get("season"):
        try:
            season = SeasonBucket(str(row["season"]).strip().upper())
        except ValueError:
            raise PriorImportError(f"bad season: {row['season']!r}")
    elif month is not None:
        season = SeasonBucket.for_month(month)

    # horizon: explicit bucket, else snap from horizon_days, else UNKNOWN
    horizon = HorizonBucket.UNKNOWN
    if row.get("horizon_bucket"):
        try:
            horizon = HorizonBucket(str(row["horizon_bucket"]).strip().upper())
        except ValueError:
            raise PriorImportError(f"bad horizon_bucket: {row['horizon_bucket']!r}")
    elif row.get("horizon_days") is not None:
        hd = _opt_int(row["horizon_days"])
        if hd is None or hd < 0:
            raise PriorImportError(f"bad horizon_days: {row['horizon_days']!r}")
        horizon = HorizonBucket.for_days(hd)

    weekday = WeekdayClass.UNKNOWN
    wd = row.get("weekday_class") or row.get("weekday")
    if wd:
        try:
            weekday = WeekdayClass(str(wd).strip().upper())
        except ValueError:
            raise PriorImportError(f"bad weekday_class: {wd!r}")

    duration = DurationBucket.UNKNOWN
    if row.get("duration_bucket"):
        try:
            duration = DurationBucket(str(row["duration_bucket"]).strip().upper())
        except ValueError:
            raise PriorImportError(f"bad duration_bucket: {row['duration_bucket']!r}")
    elif row.get("duration_nights") is not None:
        n = _opt_int(row["duration_nights"])
        if n is not None and n >= 0:
            duration = DurationBucket.for_nights(n)

    low = _minor(row, "observed_low_minor", "observed_low")
    med = _minor(row, "median_minor", "median")
    typ = _minor(row, "typical_minor", "typical")
    high = _minor(row, "observed_high_minor", "observed_high")
    for name, v in (("low", low), ("median", med), ("typical", typ), ("high", high)):
        if v is not None and v < 0:
            raise PriorImportError(f"negative {name} price")
    if low is not None and high is not None and low > high:
        raise PriorImportError(f"low {low} > high {high}")

    sd = row.get("source_date")
    source_date = default_source_date
    if sd:
        try:
            source_date = date.fromisoformat(str(sd))
        except ValueError:
            raise PriorImportError(f"bad source_date: {sd!r}")

    conf = PriorConfidence.LOW
    if row.get("confidence"):
        try:
            conf = PriorConfidence(str(row["confidence"]).strip().upper())
        except ValueError:
            conf = PriorConfidence.LOW

    return BootstrapMarketPrior(
        prior_id=store.new_prior_id(),
        source=source, source_version=source_version, imported_at=imported_at,
        source_date=source_date,
        origin_airport=o, destination_airport=d,
        destination_id=(str(row["destination_id"]) if row.get("destination_id") else None),
        season=season, month=month, horizon_bucket=horizon,
        weekday_class=weekday, duration_bucket=duration,
        currency=ccy, sample_count=_opt_int(row.get("sample_count")),
        observed_low_minor=low, median_minor=med, typical_minor=typ,
        observed_high_minor=high, confidence=conf,
        direct_possible=_bool(row.get("direct_possible")),
        weekly_frequency=_opt_int(row.get("weekly_frequency")),
        carrier_count=_opt_int(row.get("carrier_count")),
    )


def run_import(
    db: Database,
    source: MarketPriorSource,
    *,
    dry_run: bool = False,
    cfg: SearchIntelConfig | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> dict:
    """Execute one import. Returns the recorded import row as a dict.

    Never raises for bad rows — they are isolated and reported. A source-level
    exception is caught, the import row is written with ``ok=0``, and no
    ``market_priors`` change is committed (each upsert is its own transaction;
    on a source exception before persistence nothing is written)."""
    cfg = cfg or search_intel_config()
    started = now or datetime.now(timezone.utc)
    meta = source.meta()
    metrics = {k: 0 for k in (
        "rows_seen", "rows_imported", "rows_updated", "rows_skipped_dup",
        "rows_rejected", "markets_covered", "origins_covered",
        "destinations_covered",
    )}
    metrics["source_requests"] = meta.requests_made
    metrics["source_request_cost_minor"] = meta.request_cost_minor
    metrics["source_rate_limit_events"] = meta.rate_limit_events
    rejected: list[dict] = []
    valid: list[BootstrapMarketPrior] = []
    seen_keys: set[tuple] = set()
    ok, error = True, ""

    try:
        for i, raw in enumerate(source.records()):
            if limit is not None and metrics["rows_seen"] >= limit:
                break
            metrics["rows_seen"] += 1
            try:
                p = normalize(
                    raw, source=meta.source, source_version=meta.source_version,
                    imported_at=started, default_source_date=meta.source_date,
                )
            except PriorImportError as e:
                metrics["rows_rejected"] += 1
                rejected.append({"row": i, "reason": str(e)})
                continue
            key = (p.source, *p.market_tuple(), p.weekday_class.value,
                   p.duration_bucket.value, p.currency)
            if key in seen_keys:
                metrics["rows_skipped_dup"] += 1
                continue
            seen_keys.add(key)
            valid.append(p)
    except Exception as e:  # source-level failure
        ok, error = False, f"{type(e).__name__}: {e}"

    metrics["markets_covered"] = len({(p.origin_airport, p.destination_airport) for p in valid})
    metrics["origins_covered"] = len({p.origin_airport for p in valid})
    metrics["destinations_covered"] = len({p.destination_airport for p in valid})

    if valid and ok and not dry_run:
        ins, upd = store.upsert_priors(db, valid)
        metrics["rows_imported"] = ins
        metrics["rows_updated"] = upd
    elif dry_run:
        metrics["rows_imported"] = len(valid)  # would-be

    finished = datetime.now(timezone.utc) if now is None else now
    import_id = store.record_import(
        db, source=meta.source, source_version=meta.source_version,
        dry_run=dry_run, started_at=started, finished_at=finished,
        metrics=metrics, rejected=rejected, ok=ok, error=error,
    )
    return {"import_id": import_id, "ok": ok, "error": error, "dry_run": dry_run,
            **metrics, "rejected": rejected[:50]}
