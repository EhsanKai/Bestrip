"""Market Prior Coverage Audit (V9 Search Intelligence Slice 1.5 §A/§21).

A factual measurement of how much of the Bootstrap Market Prior dataset
actually exists, not an estimate. Every number here is derived either from
the live catalog/config (deterministic) or from a real query against the
``market_priors`` table via the existing
:func:`~detoura.persistence.market_priors.coverage_summary` (reused, not
reimplemented) plus a handful of additional read-only queries for the
distribution breakdowns that function does not already provide.

**The denominator, precisely**: this audit measures coverage against
``(origin airport, destination airport)`` pairs - the same granularity
:class:`~detoura.models.market_prior.BootstrapMarketPrior.market_tuple`
itself keys on before the finer season/horizon/weekday/duration buckets.
It deliberately does **not** attempt to report per-bucket coverage (season x
horizon x weekday x duration multiplies the space ~24x further, and the
system is sparse-by-bucket by design - see
``services/bootstrap_planner.py``'s own docstring) - reporting a
per-(origin, destination)-pair figure is the correct, honest granularity for
"is this route known at all", which is what this audit exists to answer.

A pair counts as "has usable prior" the moment at least one
``market_priors`` row exists for it, regardless of that row's own
``confidence`` - the confidence breakdown is reported separately
(``by_confidence``) precisely so a reader can judge quality themselves
rather than have this module silently apply an unstated threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from ..data.destinations import DESTINATIONS, acquisition_catalog
from ..persistence import market_priors as mp
from ..persistence.db import Database
from ..search_intel_config import SearchIntelConfig, search_intel_config

#: The prior data model, stated precisely once, reused verbatim in both the
#: engineering report and the generated JSON artifact so the two can never
#: silently drift apart.
PRIOR_DATA_MODEL_DESCRIPTION = (
    "A sparse (origin_airport, destination_airport) matrix, further bucketed "
    "by season / horizon_bucket / weekday_class / duration_bucket - not a "
    "dense per-calendar-date matrix, and not geo-cell based. Each stored row "
    "(table `market_priors`) is one historical statistics bucket for one "
    "route, from one import source; nothing here is ever a bookable quote "
    "(`not_a_quote` is stamped on every row and signal, and no accessor "
    "yields a bookable amount - see models/market_prior.py)."
)


@dataclass(slots=True)
class OriginCoverage:
    origin_airport: str
    destinations_covered: int
    rows: int


@dataclass(slots=True)
class CountryCoverage:
    country: str
    destinations_covered: int
    rows: int


@dataclass(slots=True)
class MarketPriorCoverageAudit:
    generated_at: datetime
    prior_data_model: str
    catalog_destinations: int
    eligible_destinations: int
    origins: int
    possible_pairs: int
    usable_prior_pairs: int
    unknown_pairs: int
    coverage_pct: float
    by_origin: list[OriginCoverage]
    by_country: list[CountryCoverage]
    destinations_with_no_prior: list[str]
    origins_with_no_prior: list[str]
    total_rows: int
    by_source: dict
    by_confidence: dict
    sample_count_distribution: dict
    oldest_source_date: str | None
    newest_source_date: str | None
    last_import_at: str | None
    rows_missing_source_date: int
    note: str

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at.isoformat(),
            "prior_data_model": self.prior_data_model,
            "catalog_destinations": self.catalog_destinations,
            "eligible_destinations": self.eligible_destinations,
            "origins": self.origins,
            "possible_pairs": self.possible_pairs,
            "usable_prior_pairs": self.usable_prior_pairs,
            "unknown_pairs": self.unknown_pairs,
            "coverage_pct": self.coverage_pct,
            "by_origin": [
                {"origin_airport": o.origin_airport,
                 "destinations_covered": o.destinations_covered, "rows": o.rows}
                for o in self.by_origin
            ],
            "by_country": [
                {"country": c.country, "destinations_covered": c.destinations_covered,
                 "rows": c.rows}
                for c in self.by_country
            ],
            "destinations_with_no_prior": self.destinations_with_no_prior,
            "origins_with_no_prior": self.origins_with_no_prior,
            "freshness": {
                "oldest_source_date": self.oldest_source_date,
                "newest_source_date": self.newest_source_date,
                "last_import_at": self.last_import_at,
                "rows_missing_source_date": self.rows_missing_source_date,
            },
            "provenance": {
                "total_rows": self.total_rows,
                "by_source": self.by_source,
                "by_confidence": self.by_confidence,
                "sample_count_distribution": self.sample_count_distribution,
            },
            "note": self.note,
        }


def compute_coverage_audit(
    db: Database,
    *,
    catalog: list | None = None,
    bootstrap_origins: tuple[str, ...] | None = None,
    cfg: SearchIntelConfig | None = None,
    now: datetime | None = None,
) -> MarketPriorCoverageAudit:
    """Compute the audit fresh against ``db``. Read-only; writes nothing.

    ``catalog``/``bootstrap_origins`` are injectable purely for deterministic
    testing against a small fixture set - production/report generation
    should omit both and let this use the real catalog and the real
    configured bootstrap origin list.
    """
    cat = catalog if catalog is not None else list(acquisition_catalog())
    origins = tuple(bootstrap_origins) if bootstrap_origins is not None else (
        (cfg or search_intel_config()).bootstrap_origin_airports
    )
    moment = now or datetime.now(timezone.utc)

    destination_airports = sorted({d.primary_airport for d in cat})
    country_by_airport = {d.primary_airport: d.country for d in cat}
    possible_pairs = len(origins) * len(destination_airports)

    summary = mp.coverage_summary(db)

    origin_set = set(origins)
    dest_set = set(destination_airports)
    placeholders_o = ",".join("?" for _ in origin_set) or "''"
    placeholders_d = ",".join("?" for _ in dest_set) or "''"

    # Distinct (origin, destination) pairs actually within this audit's own
    # denominator - deliberately not the same as coverage_summary()'s own
    # "markets" count, which is unbounded by any origin/destination set and
    # would silently over/undercount relative to the denominator this audit
    # defines (e.g. a manually-imported test row for an origin outside the
    # configured bootstrap list must not inflate "coverage" of that list).
    if origin_set and dest_set:
        pair_row = db.query_one(
            "SELECT COUNT(DISTINCT origin_airport || '|' || destination_airport) AS n, "
            "COUNT(*) AS rows "
            f"FROM market_priors WHERE origin_airport IN ({placeholders_o}) "
            f"AND destination_airport IN ({placeholders_d})",
            (*origin_set, *dest_set),
        )
        usable_prior_pairs = int(pair_row["n"]) if pair_row and pair_row["n"] else 0
        rows_in_denominator = int(pair_row["rows"]) if pair_row and pair_row["rows"] else 0
    else:
        usable_prior_pairs = 0
        rows_in_denominator = 0

    unknown_pairs = max(0, possible_pairs - usable_prior_pairs)
    coverage_pct = round(100.0 * usable_prior_pairs / possible_pairs, 4) if possible_pairs else 0.0

    by_origin: list[OriginCoverage] = []
    origins_with_no_prior: list[str] = []
    if origin_set and dest_set:
        origin_rows = db.query(
            "SELECT origin_airport, "
            "COUNT(DISTINCT destination_airport) AS destinations_covered, "
            "COUNT(*) AS rows "
            f"FROM market_priors WHERE origin_airport IN ({placeholders_o}) "
            f"AND destination_airport IN ({placeholders_d}) "
            "GROUP BY origin_airport",
            (*origin_set, *dest_set),
        )
        covered_origins = {r["origin_airport"]: (int(r["destinations_covered"]), int(r["rows"])) for r in origin_rows}
        for code in sorted(origin_set):
            covered, rows = covered_origins.get(code, (0, 0))
            by_origin.append(OriginCoverage(origin_airport=code, destinations_covered=covered, rows=rows))
            if covered == 0:
                origins_with_no_prior.append(code)

    by_country: list[CountryCoverage] = []
    covered_destination_airports: set[str] = set()
    if origin_set and dest_set:
        dest_rows = db.query(
            "SELECT destination_airport, COUNT(*) AS rows "
            f"FROM market_priors WHERE origin_airport IN ({placeholders_o}) "
            f"AND destination_airport IN ({placeholders_d}) "
            "GROUP BY destination_airport",
            (*origin_set, *dest_set),
        )
        country_agg: dict[str, list[int]] = {}
        for r in dest_rows:
            code = r["destination_airport"]
            covered_destination_airports.add(code)
            country = country_by_airport.get(code, "UNKNOWN")
            bucket = country_agg.setdefault(country, [0, 0])
            bucket[0] += 1
            bucket[1] += int(r["rows"])
        for country, (dest_count, rows) in sorted(country_agg.items()):
            by_country.append(CountryCoverage(country=country, destinations_covered=dest_count, rows=rows))

    destinations_with_no_prior = sorted(dest_set - covered_destination_airports)

    # Sample-count distribution - deliberately bucketed, not raw per-row, to
    # keep the artifact small; every row's sample_count is UNKNOWN-safe
    # (None -> its own bucket, never coerced to 0).
    sample_rows = db.query(
        "SELECT sample_count FROM market_priors "
        f"WHERE origin_airport IN ({placeholders_o}) AND destination_airport IN ({placeholders_d})"
        if origin_set and dest_set else "SELECT sample_count FROM market_priors WHERE 0",
        (*origin_set, *dest_set) if origin_set and dest_set else (),
    )
    sample_dist = {"unknown": 0, "1-2": 0, "3-9": 0, "10+": 0}
    for r in sample_rows:
        n = r["sample_count"]
        if n is None:
            sample_dist["unknown"] += 1
        elif n < 3:
            sample_dist["1-2"] += 1
        elif n < 10:
            sample_dist["3-9"] += 1
        else:
            sample_dist["10+"] += 1

    missing_source_date_row = db.query_one(
        "SELECT COUNT(*) AS n FROM market_priors "
        f"WHERE source_date IS NULL AND origin_airport IN ({placeholders_o}) "
        f"AND destination_airport IN ({placeholders_d})"
        if origin_set and dest_set else "SELECT 0 AS n",
        (*origin_set, *dest_set) if origin_set and dest_set else (),
    )
    rows_missing_source_date = int(missing_source_date_row["n"]) if missing_source_date_row else 0

    note = (
        "Coverage is measured against this audit's own denominator "
        f"({len(origins)} configured bootstrap origins x "
        f"{len(destination_airports)} acquisition-eligible catalog "
        "destinations); a market_priors row outside that set (e.g. from a "
        "manual/ad-hoc import) is excluded from every count above, not "
        "silently folded in. Zero rows means exactly what it says: no prior "
        "has ever been imported for that route, not that one was imported "
        "and found empty."
    )

    return MarketPriorCoverageAudit(
        generated_at=moment,
        prior_data_model=PRIOR_DATA_MODEL_DESCRIPTION,
        catalog_destinations=len(DESTINATIONS),
        eligible_destinations=len(cat),
        origins=len(origins),
        possible_pairs=possible_pairs,
        usable_prior_pairs=usable_prior_pairs,
        unknown_pairs=unknown_pairs,
        coverage_pct=coverage_pct,
        by_origin=by_origin,
        by_country=by_country,
        destinations_with_no_prior=destinations_with_no_prior,
        origins_with_no_prior=origins_with_no_prior,
        total_rows=rows_in_denominator,
        by_source=dict(summary.get("by_source", {})),
        by_confidence=dict(summary.get("by_confidence", {})),
        sample_count_distribution=sample_dist,
        oldest_source_date=_iso(summary.get("oldest_source_date")),
        newest_source_date=_iso(summary.get("newest_source_date")),
        last_import_at=_iso(summary.get("last_import_at")),
        rows_missing_source_date=rows_missing_source_date,
        note=note,
    )


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)
