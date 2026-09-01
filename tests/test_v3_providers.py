"""Provider contracts, caching, metrics and money normalization."""

from __future__ import annotations

from datetime import date

import pytest

from travel_planner.config import PlannerConfig
from travel_planner.models.money import (
    BASE_CURRENCY,
    FixedExchangeRates,
    Money,
    PriceBasis,
    PriceNormalizer,
)
from travel_planner.providers.accommodation import (
    AccommodationDataProvider,
    SyntheticAccommodationDataProvider,
)
from travel_planner.providers.cache import (
    CacheStats,
    CachingAccommodationProvider,
    CachingGroundTransferProvider,
    CachingTransportProvider,
    ProviderCache,
    ProviderMetrics,
)
from travel_planner.providers.ground_transfer import (
    GroundTransferProvider,
    SyntheticGroundTransferProvider,
)
from travel_planner.providers.transport import (
    SyntheticTransportDataProvider,
    TransportDataProvider,
)
from travel_planner.services.planner import TravelPlanner

from .conftest import trip_request

DAY = date(2026, 9, 10)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def test_money_addition_requires_one_currency():
    assert (Money(amount=10) + Money(amount=5)).amount == 15.0
    with pytest.raises(ValueError, match="convert first"):
        Money(amount=10, currency="EUR") + Money(amount=5, currency="USD")


def test_money_addition_requires_matching_tax_treatment():
    with pytest.raises(ValueError, match="tax"):
        Money(amount=10) + Money(amount=5, tax_included=False)


def test_conversion_to_the_base_currency():
    normalizer = PriceNormalizer()
    converted = normalizer.to_base(Money(amount=100, currency="USD"))
    assert converted.currency == BASE_CURRENCY
    assert converted.amount == pytest.approx(92.0)


def test_tax_exclusive_quotes_are_grossed_up():
    normalizer = PriceNormalizer(tax_uplift=0.20)
    net = Money(amount=100, currency="EUR", tax_included=False)
    assert normalizer.to_base(net).amount == pytest.approx(120.0)
    assert normalizer.to_base(net).tax_included


def test_price_basis_drives_the_party_total():
    normalizer = PriceNormalizer()
    quote = Money(amount=50)
    assert normalizer.party_total(quote, PriceBasis.PER_PERSON, 4) == 200.0
    assert normalizer.party_total(quote, PriceBasis.TOTAL, 4) == 50.0
    assert normalizer.per_person(quote, PriceBasis.TOTAL, 4) == 12.5


def test_a_room_night_basis_is_refused_as_a_party_total():
    """Rooms depend on nights and occupancy; guessing would be wrong."""
    normalizer = PriceNormalizer()
    with pytest.raises(ValueError, match="nights and room count"):
        normalizer.party_total(Money(amount=60), PriceBasis.PER_ROOM_NIGHT, 2)


def test_unknown_currencies_fail_loudly():
    with pytest.raises(ValueError, match="no exchange rate"):
        FixedExchangeRates().rate("XYZ", "EUR")


def test_identical_currencies_need_no_rate():
    assert FixedExchangeRates().rate("EUR", "EUR") == 1.0


def test_rounding_is_half_up_to_cents():
    """Money rounds the way money rounds, not the way floats do."""
    assert Money(amount=1.005).scaled(1.0).amount == 1.01
    assert PriceNormalizer().to_base(Money(amount=10.125)).amount == 10.13
    assert round(1.005, 2) == 1.0  # what banker's rounding would have given


def test_synthetic_data_declares_its_currency(accommodation):
    option = accommodation.search("Prague", DAY, date(2026, 9, 12), 2)[0]
    assert option.currency == BASE_CURRENCY


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------
def test_every_synthetic_provider_satisfies_its_protocol():
    assert isinstance(SyntheticTransportDataProvider(), TransportDataProvider)
    assert isinstance(SyntheticAccommodationDataProvider(), AccommodationDataProvider)
    assert isinstance(SyntheticGroundTransferProvider(), GroundTransferProvider)


def test_caching_wrappers_also_satisfy_the_protocols():
    assert isinstance(
        CachingTransportProvider(SyntheticTransportDataProvider()),
        TransportDataProvider,
    )
    assert isinstance(
        CachingAccommodationProvider(SyntheticAccommodationDataProvider()),
        AccommodationDataProvider,
    )
    assert isinstance(
        CachingGroundTransferProvider(SyntheticGroundTransferProvider()),
        GroundTransferProvider,
    )


def test_wrapping_never_removes_capability():
    """Anything the inner provider offers stays reachable."""
    inner = SyntheticTransportDataProvider()
    wrapped = CachingTransportProvider(inner)
    assert wrapped.destinations_from("DUS") == inner.destinations_from("DUS")
    assert wrapped.coverage == inner.coverage


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
def test_the_cache_counts_hits_and_misses():
    cache: ProviderCache[str, int] = ProviderCache()
    calls = []
    cache.get_or_compute("a", lambda: calls.append(1) or 1)
    cache.get_or_compute("a", lambda: calls.append(1) or 1)
    assert len(calls) == 1
    assert cache.stats.hits == 1
    assert cache.stats.misses == 1
    assert cache.stats.hit_rate == 0.5
    assert cache.entries == 1


def test_a_repeated_transport_search_reaches_the_inner_provider_once():
    inner = SyntheticTransportDataProvider()
    wrapped = CachingTransportProvider(inner)
    for _ in range(5):
        wrapped.search("DUS", "Prague", DAY)
    assert inner.search_calls == 1
    assert wrapped.stats.hits == 4
    assert wrapped.stats.misses == 1


def test_a_repeated_accommodation_search_reaches_the_inner_provider_once():
    inner = SyntheticAccommodationDataProvider()
    wrapped = CachingAccommodationProvider(inner)
    for _ in range(5):
        wrapped.search("Prague", DAY, date(2026, 9, 12), 2)
        wrapped.min_price_per_night("Prague", 2)
    assert inner.search_calls == 1
    assert wrapped.stats.hits == 8


def test_caching_does_not_change_results():
    inner = SyntheticTransportDataProvider()
    wrapped = CachingTransportProvider(SyntheticTransportDataProvider())
    assert wrapped.search("DUS", "Prague", DAY) == inner.search("DUS", "Prague", DAY)


def test_cache_stats_merge():
    merged = CacheStats(hits=3, misses=1).merged(CacheStats(hits=1, misses=5))
    assert merged.hits == 4 and merged.misses == 6
    assert merged.lookups == 10


def test_empty_stats_have_no_hit_rate():
    assert CacheStats().hit_rate == 0.0


# ---------------------------------------------------------------------------
# Metrics through the planner
# ---------------------------------------------------------------------------
def test_the_planner_reports_provider_activity():
    """The number that decides whether a real API integration is affordable."""
    planner = TravelPlanner()
    result = planner.plan(trip_request())
    metrics = result.metadata.provider_metrics

    assert metrics["lookups"] > 1000, "the beam really does ask a lot of questions"
    assert metrics["misses"] < metrics["lookups"]
    assert metrics["hit_rate"] > 0.5
    assert metrics["transport_upstream"] < metrics["transport_lookups"]
    assert metrics["accommodation_upstream"] < metrics["accommodation_lookups"]


def test_caching_collapses_the_upstream_call_count():
    """Without the cache every lookup would be an API request."""
    planner = TravelPlanner()
    metrics = planner.plan(trip_request()).metadata.provider_metrics
    # An order of magnitude is the point; the exact ratio will drift.
    assert metrics["misses"] * 4 < metrics["lookups"]


def test_a_second_identical_plan_is_almost_entirely_cached():
    planner = TravelPlanner()
    request = trip_request()
    planner.plan(request)
    second = planner.plan(request).metadata.provider_metrics
    assert second["misses"] == 0
    assert second["hit_rate"] == 1.0


def test_metrics_can_be_switched_off():
    planner = TravelPlanner(config=PlannerConfig(collect_provider_metrics=False))
    assert planner.plan(trip_request()).metadata.provider_metrics == {}


def test_provider_metrics_aggregate():
    metrics = ProviderMetrics(
        transport=CacheStats(hits=10, misses=2),
        accommodation=CacheStats(hits=5, misses=1),
        ground_transfer=CacheStats(hits=1, misses=1),
    )
    assert metrics.total.lookups == 20
    assert metrics.upstream_calls == 4
    assert metrics.as_dict()["transport_upstream"] == 2


# ---------------------------------------------------------------------------
# Candidate generation limits (spec section 21)
# ---------------------------------------------------------------------------
def test_transport_options_per_leg_are_capped():
    """Real APIs return more departures than a beam can afford to branch on."""
    from .conftest import completed_states

    tight = TravelPlanner(config=PlannerConfig(max_transport_options_per_leg=1))
    loose = TravelPlanner(config=PlannerConfig(max_transport_options_per_leg=4))
    request = trip_request()
    assert len(completed_states(tight, request)) < len(
        completed_states(loose, request)
    )


def test_candidate_destinations_can_be_capped():
    from .conftest import completed_states

    limited = TravelPlanner(config=PlannerConfig(max_candidate_destinations=3))
    request = trip_request(budget=800, preferred_destinations=[])
    cities = {city for state in completed_states(limited, request) for city in state.cities}
    assert 0 < len(cities) <= 3


def test_stay_lengths_can_be_capped():
    from .conftest import completed_states

    limited = TravelPlanner(config=PlannerConfig(max_stay_lengths=1))
    request = trip_request(budget=800)
    lengths = {
        nights
        for state in completed_states(limited, request)
        for nights in state.stay_days
    }
    assert lengths <= {PlannerConfig().min_city_stay_days}


def test_the_search_stays_bounded_and_interactive():
    planner = TravelPlanner()
    result = planner.plan(trip_request())
    assert result.metadata.elapsed_seconds < 5.0
    assert result.metadata.states_generated < 60_000
