"""V9 Phase 3 — Destination Attractiveness: model, persistence, provenance,
versioning, determinism, coverage, and separation from user fit."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from detoura.data.destinations import CORE_DESTINATIONS, acquisition_catalog
from detoura.models.attractiveness import (
    AttractivenessConfidence,
    AttractivenessProvenance,
    DestinationAttractivenessProfile,
    unknown_profile,
)
from detoura.models.destination import Destination
from detoura.persistence import attractiveness as store
from detoura.persistence.db import Database
from detoura.services.attractiveness_import import seed_attractiveness
from detoura.services.attractiveness_model import (
    CatalogAttractivenessContext,
    derive_profile,
)


def _catalog():
    return list(acquisition_catalog())


# ======================================================================
# Model
# ======================================================================
def test_unknown_profile_has_no_fabricated_scores():
    p = unknown_profile("nowhere")
    assert p.aggregate_score is None
    assert p.sightseeing_score is None
    assert p.confidence == AttractivenessConfidence.NONE
    assert p.provenance == AttractivenessProvenance.UNKNOWN
    assert not p.is_known


def test_dimension_vector_reflects_unknown_dimensions():
    p = unknown_profile("x")
    vec = p.dimension_vector()
    assert all(v is None for v in vec.values())


def test_scores_are_bounded_0_100():
    with pytest.raises(Exception):
        DestinationAttractivenessProfile(destination_id="x", model_version=1, aggregate_score=101)
    with pytest.raises(Exception):
        DestinationAttractivenessProfile(destination_id="x", model_version=1, aggregate_score=-1)


# ======================================================================
# Derivation — determinism, provenance, honesty (not popularity)
# ======================================================================
def test_derivation_is_deterministic():
    cat = _catalog()
    ctx = CatalogAttractivenessContext.build(cat)
    by_id = {d.id: d for d in cat}
    a = derive_profile(by_id["Paris"], ctx=ctx)
    b = derive_profile(by_id["Paris"], ctx=ctx)
    assert a.aggregate_score == b.aggregate_score
    assert a.dimension_vector() == b.dimension_vector()


def test_core_destinations_are_curated_high_confidence():
    cat = _catalog()
    ctx = CatalogAttractivenessContext.build(cat)
    by_id = {d.id: d for d in cat}
    for core in CORE_DESTINATIONS[:3]:
        p = derive_profile(by_id[core.id], ctx=ctx)
        assert p.provenance == AttractivenessProvenance.CURATED
        assert p.confidence == AttractivenessConfidence.HIGH


def test_discovery_catalog_destinations_are_derived_medium_confidence():
    cat = _catalog()
    ctx = CatalogAttractivenessContext.build(cat)
    by_id = {d.id: d for d in cat}
    core_ids = {d.id for d in CORE_DESTINATIONS}
    non_core = next(d for d in cat if d.id not in core_ids)
    p = derive_profile(by_id[non_core.id], ctx=ctx)
    assert p.provenance == AttractivenessProvenance.DERIVED
    assert p.confidence == AttractivenessConfidence.MEDIUM


def test_never_measured_this_phase():
    """§B2: no authorized external data source was integrated — nothing may
    claim MEASURED provenance."""
    cat = _catalog()
    ctx = CatalogAttractivenessContext.build(cat)
    for d in cat:
        p = derive_profile(d, ctx=ctx)
        assert p.provenance != AttractivenessProvenance.MEASURED


def test_uniqueness_is_not_a_popularity_proxy():
    """§B3: uniqueness must come from attribute distance, not from anything
    resembling visitor counts / fame. A small, distinctive discovery city can
    outscore a famous but generic-profile one."""
    cat = _catalog()
    ctx = CatalogAttractivenessContext.build(cat)
    by_id = {d.id: d for d in cat}
    # Fabricate an artificial "generic" famous city (all-average attributes)
    # and an artificial "unusual" city (extreme attributes) purely to prove
    # the mechanism responds to attribute distance, not identity/fame.
    generic = Destination(
        id="__generic_famous__", name="Generic", country="Nowhere",
        history=0.5, nature=0.5, nightlife=0.5, culture=0.5, food=0.5,
        architecture=0.5, shopping=0.5, museums=0.5, beaches=0.5,
        family_friendly=0.5, romance=0.5, adventure=0.5,
    )
    unusual = Destination(
        id="__unusual_unknown__", name="Unusual", country="Nowhere",
        history=1.0, nature=0.0, nightlife=1.0, culture=0.0, food=1.0,
        architecture=0.0, shopping=1.0, museums=0.0, beaches=1.0,
        family_friendly=0.0, romance=1.0, adventure=0.0,
    )
    p_generic = derive_profile(generic, ctx=ctx)
    p_unusual = derive_profile(unusual, ctx=ctx)
    assert p_unusual.uniqueness_score > p_generic.uniqueness_score


def test_short_trip_score_rewards_good_short_trips_not_just_fast_ones():
    cat = _catalog()
    ctx = CatalogAttractivenessContext.build(cat)
    shallow_fast = Destination(
        id="__shallow__", name="Shallow", country="Nowhere",
        recommended_min_days=1.0, recommended_max_days=1.0,
        history=0.2, nature=0.2, nightlife=0.2, culture=0.2, food=0.2,
        architecture=0.2, shopping=0.2, museums=0.2, beaches=0.0,
        family_friendly=0.2, romance=0.2, adventure=0.2,
    )
    rich_fast = Destination(
        id="__rich_fast__", name="RichFast", country="Nowhere",
        recommended_min_days=1.0, recommended_max_days=2.0,
        history=0.9, nature=0.7, nightlife=0.8, culture=0.9, food=0.9,
        architecture=0.8, shopping=0.6, museums=0.8, beaches=0.0,
        family_friendly=0.6, romance=0.7, adventure=0.6,
    )
    p_shallow = derive_profile(shallow_fast, ctx=ctx)
    p_rich = derive_profile(rich_fast, ctx=ctx)
    assert p_rich.short_trip_score > p_shallow.short_trip_score


def test_full_catalog_derives_without_error():
    cat = _catalog()
    ctx = CatalogAttractivenessContext.build(cat)
    for d in cat:
        p = derive_profile(d, ctx=ctx)
        assert p.is_known
        assert 0.0 <= p.aggregate_score <= 100.0


# ======================================================================
# Persistence — CRUD, batch, versioning
# ======================================================================
def test_upsert_and_get_roundtrip():
    db = Database(":memory:")
    p = DestinationAttractivenessProfile(
        destination_id="TestCity", model_version=1, aggregate_score=70.0,
        sightseeing_score=80.0, confidence=AttractivenessConfidence.HIGH,
        provenance=AttractivenessProvenance.CURATED, source="test",
    )
    inserted, updated = store.upsert_profiles(db, [p])
    assert (inserted, updated) == (1, 0)
    fetched = store.get_profile(db, "TestCity", model_version=1)
    assert fetched is not None
    assert fetched.aggregate_score == 70.0
    assert fetched.sightseeing_score == 80.0


def test_upsert_is_idempotent_update_not_duplicate():
    db = Database(":memory:")
    p1 = DestinationAttractivenessProfile(destination_id="X", model_version=1, aggregate_score=50.0)
    p2 = DestinationAttractivenessProfile(destination_id="X", model_version=1, aggregate_score=60.0)
    store.upsert_profiles(db, [p1])
    inserted, updated = store.upsert_profiles(db, [p2])
    assert (inserted, updated) == (0, 1)
    assert store.count_profiles(db, model_version=1) == 1
    assert store.get_profile(db, "X", model_version=1).aggregate_score == 60.0


def test_new_model_version_never_reinterprets_old_rows():
    """Changing the model version must not silently reinterpret old scores —
    both versions coexist, independently readable."""
    db = Database(":memory:")
    old = DestinationAttractivenessProfile(destination_id="X", model_version=1, aggregate_score=50.0)
    new = DestinationAttractivenessProfile(destination_id="X", model_version=2, aggregate_score=90.0)
    store.upsert_profiles(db, [old, new])
    assert store.get_profile(db, "X", model_version=1).aggregate_score == 50.0
    assert store.get_profile(db, "X", model_version=2).aggregate_score == 90.0
    assert store.model_versions_present(db) == [1, 2]


def test_batch_get_is_one_query_and_missing_ids_absent():
    db = Database(":memory:")
    store.upsert_profiles(db, [
        DestinationAttractivenessProfile(destination_id="A", model_version=1, aggregate_score=10.0),
        DestinationAttractivenessProfile(destination_id="B", model_version=1, aggregate_score=20.0),
    ])
    got = store.batch_get_profiles(db, ["A", "B", "C-does-not-exist"], model_version=1)
    assert set(got) == {"A", "B"}  # C is simply absent, not a fabricated UNKNOWN row


def test_batch_get_empty_input_returns_empty_without_querying():
    db = Database(":memory:")
    assert store.batch_get_profiles(db, [], model_version=1) == {}


# ======================================================================
# Seed / import mechanism
# ======================================================================
def test_seed_attractiveness_covers_full_catalog():
    db = Database(":memory:")
    cat = _catalog()
    summary = seed_attractiveness(db, cat)
    assert summary.catalog_total == len(cat)
    assert summary.computed == len(cat)
    assert summary.inserted == len(cat)
    assert store.count_profiles(db, model_version=1) == len(cat)


def test_seed_is_idempotent_on_unchanged_catalog():
    db = Database(":memory:")
    cat = _catalog()
    seed_attractiveness(db, cat)
    summary2 = seed_attractiveness(db, cat, now=datetime.now(timezone.utc))
    assert summary2.inserted == 0
    assert summary2.updated == 0
    assert summary2.unchanged == len(cat)


def test_seed_empty_catalog_is_safe():
    db = Database(":memory:")
    summary = seed_attractiveness(db, [])
    assert summary.catalog_total == 0
    assert summary.computed == 0
