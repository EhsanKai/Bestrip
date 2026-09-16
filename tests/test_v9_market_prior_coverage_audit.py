"""V9 Search Intelligence Slice 1.5 §21/§26 — the Market Prior Coverage
Audit generator (`services.market_prior_coverage_audit`).

Covers denominator correctness, usable-vs-UNKNOWN counting, deterministic
aggregation, origin/country grouping, no duplicate-pair inflation, empty
dataset, and malformed/edge-case row behavior.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from detoura.models.market_prior import BootstrapMarketPrior, HorizonBucket, PriorConfidence
from detoura.persistence import market_priors as mp
from detoura.persistence.db import Database
from detoura.services.market_prior_coverage_audit import compute_coverage_audit


def _prior(origin, dest, *, source="test", confidence=PriorConfidence.MEDIUM,
           sample_count=5, source_date=date(2026, 1, 1), suffix="",
           horizon_bucket=HorizonBucket.UNKNOWN) -> BootstrapMarketPrior:
    # upsert_priors() is keyed on (source, origin, destination, season,
    # horizon_bucket, weekday_class, duration_bucket, currency) - two rows
    # that differ only by prior_id/suffix collapse into one via UPSERT, not
    # two distinct rows. A caller that wants genuinely distinct rows for the
    # same (origin, destination) pair must vary horizon_bucket (or another
    # bucket dimension), matching the real dedup key.
    return BootstrapMarketPrior(
        prior_id=f"prior-{origin}-{dest}-{suffix or 'x'}", source=source, source_version="1",
        imported_at=datetime.now(timezone.utc), source_date=source_date,
        origin_airport=origin, destination_airport=dest, currency="EUR",
        sample_count=sample_count, median_minor=10000, confidence=confidence,
        horizon_bucket=horizon_bucket,
    )


def test_empty_database_reports_zero_coverage_not_an_error():
    db = Database(":memory:")
    audit = compute_coverage_audit(db, catalog=[], bootstrap_origins=())
    d = audit.as_dict()
    assert d["possible_pairs"] == 0
    assert d["usable_prior_pairs"] == 0
    assert d["unknown_pairs"] == 0
    assert d["coverage_pct"] == 0.0


def test_denominator_is_origins_times_destinations():
    db = Database(":memory:")
    audit = compute_coverage_audit(
        db, bootstrap_origins=("AAA", "BBB", "CCC"),
        catalog=_fake_catalog(["XXX", "YYY"]),
    )
    assert audit.possible_pairs == 3 * 2
    assert audit.origins == 3
    assert audit.eligible_destinations == 2


def _fake_catalog(airport_codes: list[str]):
    from detoura.models.destination import Destination

    return [
        Destination(
            id=code, name=code, country=f"Country-{code}", country_code="XX",
            primary_airport=code, latitude=0.0, longitude=float(i),
            enabled=True, acquisition_eligible=True, metadata_source="test",
        )
        for i, code in enumerate(airport_codes)
    ]


def test_usable_vs_unknown_counting():
    db = Database(":memory:")
    mp.upsert_priors(db, [_prior("AAA", "XXX"), _prior("AAA", "YYY"), _prior("BBB", "XXX")])
    audit = compute_coverage_audit(
        db, bootstrap_origins=("AAA", "BBB", "CCC"), catalog=_fake_catalog(["XXX", "YYY"]),
    )
    assert audit.possible_pairs == 6
    assert audit.usable_prior_pairs == 3
    assert audit.unknown_pairs == 3
    assert audit.coverage_pct == pytest.approx(50.0)


def test_multiple_rows_for_the_same_pair_do_not_inflate_the_pair_count():
    """Several season/horizon buckets for the SAME (origin, destination) must
    still count as one covered pair, not several."""
    db = Database(":memory:")
    mp.upsert_priors(db, [
        _prior("AAA", "XXX", suffix="a", horizon_bucket=HorizonBucket.H14),
        _prior("AAA", "XXX", suffix="b", horizon_bucket=HorizonBucket.H30, source_date=date(2026, 2, 1)),
        _prior("AAA", "XXX", suffix="c", horizon_bucket=HorizonBucket.H60, source_date=date(2026, 3, 1)),
    ])
    audit = compute_coverage_audit(db, bootstrap_origins=("AAA",), catalog=_fake_catalog(["XXX"]))
    assert audit.usable_prior_pairs == 1
    assert audit.total_rows == 3  # rows are still counted, just not as extra pairs


def test_rows_outside_the_denominator_are_excluded_not_folded_in():
    """A row for an origin/destination outside this audit's own configured
    sets must not silently inflate coverage against a denominator it was
    never part of."""
    db = Database(":memory:")
    mp.upsert_priors(db, [_prior("ZZZ", "XXX")])  # ZZZ is not a configured origin
    audit = compute_coverage_audit(db, bootstrap_origins=("AAA",), catalog=_fake_catalog(["XXX"]))
    assert audit.usable_prior_pairs == 0
    assert audit.possible_pairs == 1


def test_by_origin_and_by_country_distribution():
    db = Database(":memory:")
    mp.upsert_priors(db, [_prior("AAA", "XXX"), _prior("AAA", "YYY"), _prior("BBB", "XXX")])
    audit = compute_coverage_audit(
        db, bootstrap_origins=("AAA", "BBB", "CCC"), catalog=_fake_catalog(["XXX", "YYY"]),
    )
    by_origin = {o.origin_airport: o.destinations_covered for o in audit.by_origin}
    assert by_origin == {"AAA": 2, "BBB": 1, "CCC": 0}
    countries = {c.country: c.destinations_covered for c in audit.by_country}
    # "Country-XXX" is one destination airport (XXX) covered by two origins
    # (AAA and BBB) - destinations_covered counts distinct destinations
    # reached, not origin-destination rows, so it's 1, not 2 (rows=2 is
    # reported separately, in "rows").
    assert countries == {"Country-XXX": 1, "Country-YYY": 1}
    rows_by_country = {c.country: c.rows for c in audit.by_country}
    assert rows_by_country == {"Country-XXX": 2, "Country-YYY": 1}


def test_destinations_and_origins_with_no_prior_are_listed():
    db = Database(":memory:")
    mp.upsert_priors(db, [_prior("AAA", "XXX")])
    audit = compute_coverage_audit(
        db, bootstrap_origins=("AAA", "BBB"), catalog=_fake_catalog(["XXX", "YYY"]),
    )
    assert audit.destinations_with_no_prior == ["YYY"]
    assert audit.origins_with_no_prior == ["BBB"]


def test_confidence_and_source_breakdown_is_reported():
    db = Database(":memory:")
    mp.upsert_priors(db, [
        _prior("AAA", "XXX", confidence=PriorConfidence.HIGH, source="src-a"),
        _prior("AAA", "YYY", confidence=PriorConfidence.LOW, source="src-b"),
    ])
    audit = compute_coverage_audit(db, bootstrap_origins=("AAA",), catalog=_fake_catalog(["XXX", "YYY"]))
    assert audit.by_confidence.get("HIGH") == 1
    assert audit.by_confidence.get("LOW") == 1
    assert "src-a" in audit.by_source
    assert "src-b" in audit.by_source


def test_sample_count_distribution_buckets_and_handles_unknown():
    db = Database(":memory:")
    mp.upsert_priors(db, [
        _prior("AAA", "XXX", sample_count=None, suffix="none", horizon_bucket=HorizonBucket.H14),
        _prior("AAA", "XXX", sample_count=1, suffix="low", horizon_bucket=HorizonBucket.H30,
               source_date=date(2026, 2, 1)),
        _prior("AAA", "XXX", sample_count=20, suffix="hi", horizon_bucket=HorizonBucket.H60,
               source_date=date(2026, 3, 1)),
    ])
    audit = compute_coverage_audit(db, bootstrap_origins=("AAA",), catalog=_fake_catalog(["XXX"]))
    assert audit.sample_count_distribution["unknown"] == 1
    assert audit.sample_count_distribution["1-2"] == 1
    assert audit.sample_count_distribution["10+"] == 1


def test_audit_is_deterministic_across_repeated_calls():
    db = Database(":memory:")
    mp.upsert_priors(db, [_prior("AAA", "XXX"), _prior("BBB", "YYY")])
    catalog = _fake_catalog(["XXX", "YYY"])
    first = compute_coverage_audit(db, bootstrap_origins=("AAA", "BBB"), catalog=catalog)
    second = compute_coverage_audit(db, bootstrap_origins=("AAA", "BBB"), catalog=catalog)
    # Exclude generated_at (the one deliberately non-deterministic field).
    d1, d2 = first.as_dict(), second.as_dict()
    d1.pop("generated_at"); d2.pop("generated_at")
    assert d1 == d2


def test_real_catalog_and_config_produce_a_sane_result():
    """Sanity check against the real catalog/config (not a fixture) - the
    numbers themselves are not asserted (they legitimately vary with the
    database given), only that the computation runs cleanly and produces an
    internally consistent result."""
    db = Database(":memory:")
    audit = compute_coverage_audit(db)
    assert audit.possible_pairs == audit.origins * audit.eligible_destinations
    assert audit.usable_prior_pairs + audit.unknown_pairs == audit.possible_pairs
    assert 0.0 <= audit.coverage_pct <= 100.0
    assert audit.catalog_destinations >= audit.eligible_destinations
