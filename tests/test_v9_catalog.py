"""V9 Phase 2 — expanded European catalog (§18, §19, §35).

The discovery catalog grows from the legacy 16 to ~200 destinations with broad
coverage. The synthetic beam-search network stays on the 16 CORE cities; a real
acquisition may reach every enabled, acquisition-eligible catalog entry that has
a usable primary airport. Origin is independent of the catalog.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

import pytest

from detoura.data.destinations import (
    ALL_NODES,
    CORE_DESTINATION_IDS,
    CORE_DESTINATIONS,
    DESTINATIONS,
    ORIGIN_AIRPORTS,
    acquisition_catalog,
)
from detoura.models.destination import Destination
from detoura.models.trip import TravelPreferences, TripRequest
from detoura.providers.destinations import StaticDestinationProvider
from detoura.services.acquisition import ProviderCallBudget, build_plan, rank_candidates
from detoura.services.real_supply import resolve_airport

_IATA = re.compile(r"^[A-Z]{3}$")


# ======================================================================
# Scale + centralisation
# ======================================================================
def test_catalog_is_roughly_200_useful_destinations():
    assert 180 <= len(DESTINATIONS) <= 240


def test_no_sixteen_destination_assumption_in_acquisition():
    acq = acquisition_catalog()
    assert len(acq) > 150  # far beyond the legacy 16
    assert len({d.id for d in acq}) == len(acq)


def test_catalog_loading_is_deterministic():
    a = [d.id for d in DESTINATIONS]
    # re-import from a fresh module dict → identical order and membership
    import importlib

    import detoura.data.destinations as mod

    b = [d.id for d in importlib.reload(mod).DESTINATIONS]
    assert a == b
    # provider ordering is stable too
    p1 = StaticDestinationProvider(DESTINATIONS).ids()
    p2 = StaticDestinationProvider(DESTINATIONS).ids()
    assert p1 == p2 == sorted(p1)


def test_catalog_is_centralised_typed_models():
    assert all(isinstance(d, Destination) for d in DESTINATIONS)


# ======================================================================
# Airport metadata / governance (§18)
# ======================================================================
def test_every_acquisition_eligible_destination_has_a_valid_primary_airport():
    for d in DESTINATIONS:
        if d.enabled and d.acquisition_eligible:
            assert d.primary_airport is not None, d.id
            assert _IATA.match(d.primary_airport), (d.id, d.primary_airport)


def test_no_duplicate_destination_ids():
    ids = [d.id for d in DESTINATIONS]
    dupes = [k for k, v in Counter(ids).items() if v > 1]
    assert dupes == []


def test_no_two_destinations_share_a_primary_airport():
    aps = [d.primary_airport for d in DESTINATIONS if d.primary_airport]
    dupes = [k for k, v in Counter(aps).items() if v > 1]
    assert dupes == []


def test_country_codes_are_iso_alpha2_upper():
    for d in DESTINATIONS:
        if d.country_code is not None:
            assert re.match(r"^[A-Z]{2}$", d.country_code), (d.id, d.country_code)


def test_coordinates_are_within_range_when_present():
    for d in DESTINATIONS:
        if d.latitude is not None:
            assert -90 <= d.latitude <= 90 and d.longitude is not None
            assert -180 <= d.longitude <= 180
            # Europe-ish sanity bound (Canaries ~28N/-16E, Reykjavík ~64N/-22E,
            # Cyprus ~35N/33E, Nordkapp ~71N)
            assert -30 <= d.longitude <= 45 and 27 <= d.latitude <= 72, d.id


def test_acquisition_eligible_without_airport_is_rejected_by_model():
    with pytest.raises(ValueError):
        Destination(id="Nowhere", name="Nowhere", country="X",
                    acquisition_eligible=True, enabled=True, primary_airport=None)


def test_secondary_airports_are_reference_only_never_queried():
    # §18: acquisition uses the single primary airport, never fans out.
    import detoura.services.real_supply as rs
    src = open(rs.__file__).read()
    assert "secondary_airport" not in src


# ======================================================================
# Coverage breadth (§35 — not concentrated in one country/region)
# ======================================================================
def test_broad_country_and_subregion_coverage():
    countries = Counter(d.country_code for d in DESTINATIONS if d.country_code)
    assert len(countries) >= 20
    # no single country dominates
    assert max(countries.values()) / len(DESTINATIONS) <= 0.16
    subregions = {d.subregion for d in DESTINATIONS if d.subregion}
    assert len(subregions) >= 8


def test_expected_regions_all_represented():
    subs = {d.subregion for d in DESTINATIONS if d.subregion}
    for expected in ("Iberia", "Italy", "France", "Central Europe", "Balkans",
                     "Nordics", "Baltics", "Greece", "UK & Ireland", "Benelux"):
        assert expected in subs, expected


# ======================================================================
# Core network unchanged (synthetic beam search)
# ======================================================================
def test_core_catalog_is_still_the_legacy_sixteen():
    assert len(CORE_DESTINATIONS) == 16
    assert len(CORE_DESTINATION_IDS) == 16
    assert CORE_DESTINATION_IDS == frozenset(d.id for d in CORE_DESTINATIONS)


def test_default_provider_serves_only_the_core_network():
    prov = StaticDestinationProvider()
    assert {d.id for d in prov.all()} == set(CORE_DESTINATION_IDS)


def test_synthetic_graph_nodes_are_origins_plus_core_only():
    node_set = set(ALL_NODES)
    assert node_set == {a.code for a in ORIGIN_AIRPORTS} | set(CORE_DESTINATION_IDS)


# ======================================================================
# Origin independence (§19)
# ======================================================================
def test_origin_airport_need_not_be_in_the_catalog():
    catalog_airports = {d.primary_airport for d in DESTINATIONS if d.primary_airport}
    # JFK is not a European catalog city, but is a valid origin token
    assert "JFK" not in catalog_airports
    assert resolve_airport("JFK", {}) == "JFK"
    # a lowercase / non-code token that is not a known city stays unresolved
    assert resolve_airport("Atlantis", {}) is None


def test_core_destinations_all_have_phase2_geography():
    for d in CORE_DESTINATIONS:
        assert d.country_code and re.match(r"^[A-Z]{2}$", d.country_code)
        assert d.subregion
        assert d.region == "Europe"


# ======================================================================
# Acquisition eligibility is opt-in, not opt-out (Phase 2 gate finding)
# ======================================================================
def _req(**kw) -> TripRequest:
    f = dict(origin="Köln", budget=1000.0, travelers=1, duration_days=3,
             date_from=date(2026, 10, 1), date_to=date(2026, 10, 20),
             preferences=TravelPreferences())
    f.update(kw)
    return TripRequest(**f)


def test_destination_defaults_to_not_acquisition_eligible():
    # A generic/ad-hoc Destination (no governance fields set) never
    # accidentally opts itself into real provider acquisition.
    bare = Destination(id="Bare", name="Bare", country="Nowhere")
    assert bare.acquisition_eligible is False
    assert bare.enabled is True  # visible/usable, just not acquirable by default


def test_every_real_catalog_destination_explicitly_opts_in():
    for d in DESTINATIONS:
        if d.enabled and d.primary_airport:
            assert d.acquisition_eligible is True, d.id
            assert d.metadata_source in ("curated", "synthetic"), d.id


def test_acquisition_catalog_size_unaffected_by_the_default_flip():
    # The default flip (True -> False) must not silently shrink the real
    # catalog - every real entry sets acquisition_eligible=True explicitly.
    assert len(acquisition_catalog()) > 150
    assert len(acquisition_catalog()) == sum(
        1 for d in DESTINATIONS if d.enabled and d.acquisition_eligible and d.primary_airport
    )


def test_disabled_and_ineligible_destinations_never_enter_rank_candidates():
    eligible = Destination(id="OkCity", name="OkCity", country="X", country_code="XX",
                           primary_airport="AAA", acquisition_eligible=True, enabled=True)
    disabled = Destination(id="DisabledCity", name="DisabledCity", country="X", country_code="XX",
                           primary_airport="BBB", acquisition_eligible=True, enabled=False)
    ineligible = Destination(id="IneligibleCity", name="IneligibleCity", country="X",
                             country_code="XX", primary_airport="CCC",
                             acquisition_eligible=False, enabled=True)
    no_airport = Destination(id="NoAirportCity", name="NoAirportCity", country="X",
                             country_code="XX")  # acquisition_eligible defaults False

    chosen, dropped = rank_candidates(
        [eligible, disabled, ineligible, no_airport], _req(), limit=10,
    )
    chosen_ids = {d.id for d in chosen}
    assert chosen_ids == {"OkCity"}
    assert {"DisabledCity", "IneligibleCity", "NoAirportCity"} & chosen_ids == set()


def test_must_visit_does_not_override_acquisition_governance():
    # An explicit must-visit request cannot force a disabled/ineligible
    # destination into a real provider acquisition plan.
    disabled = Destination(id="DisabledCity", name="DisabledCity", country="X",
                           country_code="XX", primary_airport="BBB",
                           acquisition_eligible=True, enabled=False)
    chosen, _ = rank_candidates([disabled], _req(must_visit=["DisabledCity"]), limit=10)
    assert chosen == []


def test_build_plan_never_constructs_edges_for_ineligible_destinations():
    eligible = Destination(id="OkCity", name="OkCity", country="X", country_code="XX",
                           primary_airport="AAA", acquisition_eligible=True, enabled=True)
    ineligible = Destination(id="IneligibleCity", name="IneligibleCity", country="X",
                             country_code="XX", primary_airport="CCC",
                             acquisition_eligible=False, enabled=True)
    budget = ProviderCallBudget(max_offer_requests=100, max_destinations=8,
                                max_date_variants=1, max_airport_variants=1)
    plan = build_plan(_req(), destinations=[eligible, ineligible], airports=["LHR"],
                      days=[date(2026, 10, 5)], budget=budget)
    endpoints = {e.origin for e in plan.edges} | {e.destination for e in plan.edges}
    assert "IneligibleCity" not in endpoints
    assert "OkCity" in plan.destinations
