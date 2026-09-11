# V9 Phase 2 — European Catalog + Bootstrap Market Prior

Branch `claude/travel-planner-mvp-nvb267`. Backend only — V9 consumer UI/UX is
owned separately and untouched. Duffel **TEST MODE only**. Builds on V9 Phase
1 (Search Intelligence Foundation — `docs/V9_PHASE1_SEARCH_INTELLIGENCE.md`),
which is APPROVED and unmodified in spirit here: this phase adds a second,
architecturally distinct knowledge source (a bootstrap prior) and widens the
catalog Phase 1's funnel reasons over.

## Why the dense fare matrix was rejected

A naive approach stores one row per (origin, destination, exact date): for
~30 origins x ~200 destinations x 365 days that is tens of millions of rows of
data nobody has, for numbers that would be stale and false-precise even if
somebody had them. Two smaller catalogs (Phase 1's synthetic 16 destinations,
and the earlier design sketch) already show the actual usable signal is much
coarser: *is this route generally cheap, and does it have frequent direct
service, this time of year, booked with this much lead time?* — a handful of
bucketed answers, not 365 numbers.

Phase 2 instead builds a **sparse Bootstrap Market Prior**: a few
representative bucket rows per market (season x horizon x weekday x duration),
explicitly labelled UNKNOWN where the source does not say, never fabricated to
fill a grid cell.

## Two knowledge sources, never confused

| | `DETOURA_LIVE_OBSERVATION` (Phase 1) | `BOOTSTRAP_PRIOR` (Phase 2) |
|---|---|---|
| model | `PriceObservation` / `HistoricalPriceSignal` (`models/search_intel.py`) | `BootstrapMarketPrior` / `HistoricalMarketPriorSignal` (`models/market_prior.py`) |
| table | `price_observations` | `market_priors` |
| what it is | an actual normalized price Detoura itself observed via a real Duffel acquisition | external / pre-seeded *approximate* historical intelligence about a market |
| granularity | exact origin/destination/date/party | sparse buckets: season, horizon, weekday class, duration class |
| provenance | `search_id`, `acquisition_call_id`, edge kind | `source`, `source_version`, `source_date` |
| retention | `PRICE_MEMORY_RETENTION_DAYS` (180) | `MARKET_PRIOR_RETENTION_DAYS` (365), pruned independently (`persistence/market_priors.py::prune`) |
| answers | "what did Detoura itself see recently?" | "where is it probably worth spending a live provider request at all?" |
| is a quote | never (`not_a_quote=True`) | never (`not_a_quote=True`) |

`MarketDataSource` (`models/market_prior.py`) is the enum that keeps these
apart at the type level: `BOOTSTRAP_PRIOR` vs `DETOURA_LIVE_OBSERVATION`. There
is a third concept, unrelated to either: `TransportOption` — an actual
provider offer eligible for booking. A prior is never promoted into one.

## Hard invariant: the prior is never a quote

Enforced, not just documented (`tests/test_v9_market_prior.py`):

* No type in `models/market_prior.py` has an `.amount` / `.as_quote()` /
  `.current_price` / `.bookable_amount` accessor.
* Every `BootstrapMarketPrior` and `HistoricalMarketPriorSignal` carries
  `not_a_quote = True`.
* `services/revalidation.py`, `services/booking_commercial.py`,
  `services/booking_flow.py`, `services/recheck.py`, `services/guided_booking.py`
  do not import `market_prior`, `market_priors`, `BootstrapMarketPrior`,
  `opportunity` or `candidate_funnel` — checked by source inspection, not just
  behaviour, so the boundary cannot regress silently.
* `persistence/market_priors.py` and `persistence/price_memory.py` share no
  SQL against each other's tables.
* The Ops DTO (`GET /api/v1/ops/search-intel/market-prior`) is stamped
  `not_a_quote: true` and every amount field is named `*_estimate`, never
  `price` or `fare`.

## Sparse time buckets

`models/market_prior.py`:

* `HorizonBucket` — `H14 / H30 / H45 / H60 / H90 / H120 / UNKNOWN`, configurable
  via `MARKET_PRIOR_HORIZON_BUCKETS` (`search_intel_config.py`, default
  `14,30,45,60,90,120`). `HorizonBucket.for_days()` snaps an arbitrary lead
  time to the nearest bucket — never a new bucket per day.
* `SeasonBucket` — `LOW / SHOULDER / PEAK / UNKNOWN`, derived from month
  (`for_month`) when a source only gives a month, or taken directly when a
  source states its own season.
* `WeekdayClass` — `WEEKDAY / WEEKEND / UNKNOWN`.
* `DurationBucket` — `SHORT (<=3n) / MEDIUM (<=7n) / LONG / UNKNOWN`.

A source that gives no horizon, season, weekday or duration information gets
`UNKNOWN` in that dimension — never a guess, never averaged into a bucket it
was not observed in. `PriorConfidence` (`NONE/LOW/MEDIUM/HIGH`) is a plain
interpretable enum, not an ML score, matching the Phase 1 `MarketConfidence`
semantics exactly.

## Import pipeline (§8–§9)

```
MarketPriorSource.records()  ->  normalize()  ->  dedup  ->  (dry-run?)  ->  upsert_priors()
     (provider-neutral)          (validate)      (per key)                  (persistence/market_priors.py)
```

`services/market_prior_source.py` defines the `MarketPriorSource` protocol
(`meta()` + `records()`) and ships `FixtureMarketPriorSource` (deterministic,
offline, for tests/benchmarks) plus `JsonMarketPriorSource` /
`CsvMarketPriorSource` for an operator-approved dataset. See
`docs/V9_MARKET_PRIOR_SOURCE_OPTIONS.md` for which categories of real source
are acceptable — **no consumer-website scraping, no bot-protection bypass, no
private-API reverse engineering**, enforced by never building a source
adapter that does those things, not by a runtime check.

`services/market_prior_import.py::normalize()`:

* reads only an explicit allow-list of keys (`_ALLOWED_KEYS`) — anything else
  on a raw record, including an attempted PII field, is silently dropped;
* validates IATA codes (3-letter uppercase alpha), currency (3-letter alpha),
  month range, price ordering (`low <= high`), and `origin != destination`;
* derives season from month and horizon bucket from `horizon_days` when the
  source does not state the bucket directly;
* raises `PriorImportError` for anything unusable — the row is isolated
  (`rows_rejected`, `rejected` detail list capped at 200), the run continues.

`run_import()`:

* is **idempotent** — `upsert_priors()` keys on
  `(source, origin, destination, season, horizon, weekday, duration, currency)`
  and replaces, never duplicates, a re-imported row;
* **dry-run** validates and reports without writing;
* a **source-level exception** (the iterator itself raises) aborts before any
  row from that run is persisted — pre-existing data is untouched, the import
  is recorded with `ok=0` and the error message;
* every import is recorded in `market_prior_imports` with rows seen / imported
  / updated / rejected / skipped-dup, markets/origins/destinations covered,
  and the source's own reported request count / cost / rate-limit events
  (each `None` when the source does not say — never 0).

## Confidence + decay (documented, deterministic)

`services/market_prior_signal.py`:

```
row_component     = min(1, matching_row_count / 4)
source_conf       = mean of the rows' stated PriorConfidence  (NONE 0 / LOW 0.33 / MEDIUM 0.66 / HIGH 1)
freshness         = 0.5 ** (age_days / MARKET_PRIOR_DECAY_HALF_LIFE_DAYS)     # default half-life 120 days
                    age from the newest matching row's source_date (imported_at if absent)

score   = 0.30 * row_component + 0.35 * source_conf + 0.35 * freshness
verdict = NONE if no rows, else LOW (<0.34) / MEDIUM (<0.67) / HIGH otherwise
```

A cap: if the source rows themselves were mostly LOW/NONE confidence
(`source_conf < 0.5`), the verdict cannot read HIGH however fresh or numerous
— a bootstrap estimate the source doubted is not laundered into "HIGH" by
recency alone. A prior more than roughly one half-life stale decays toward LOW
and can no longer dominate acquisition scoring (§15, verified in
`tests/test_v9_market_prior.py::test_E_very_stale_prior_confidence_decays_below_fresh`).

Currency is never merged: `batch_prior_signals()` groups rows by destination
*then* currency, producing one `HistoricalMarketPriorSignal` per currency — a
EUR prior and a USD prior for the same market never blend, matching the Phase
1 rule for live observations.

## Signal precedence: live overrides prior

`services/opportunity.py::_price_component()`, in order:

1. **Recent, sufficient live Price Memory** (`live_n >= MARKET_PRIOR_LIVE_SUPERSEDES_MIN`,
   default 4 confident samples) — the prior's weight in the blended price
   component is multiplied by `0.05`; live effectively decides it.
2. **Thin live history** (`0 < live_n < live_supersedes_min_samples`) — blended,
   live weighted by `weight_live_when_confident` (default 1.0), prior weight
   shrinking linearly as live samples accumulate.
3. **No live, but a prior** — prior attractiveness scaled by the prior's own
   confidence and capped at `MARKET_PRIOR_WEIGHT_CEILING` (default 0.55) — a
   prior can meaningfully move the score, but never like confirmed live data.
4. **Neither** — neutral `0.5` (cold start).

This is exercised directly in
`tests/test_v9_market_prior.py::test_B_fresh_live_dominates_stale_prior`.

## Market Opportunity scoring (§14–§15)

`services/opportunity.py::score_opportunities()` — an interpretable weighted
sum, not opaque ML, every component in `[0, 1]`:

```
price          blended live/prior price attractiveness (precedence above)      weight 0.28
confidence     max(live confidence, prior confidence)                          weight 0.16
freshness      max(live recency, prior freshness)                              weight 0.12
preference     caller-supplied destination affinity (0.5 if unknown)           weight 0.16
feasibility    1 if the destination resolves to an airport, else 0             weight 0.10
supply         direct/frequency signal from the prior (0.5 if unknown)         weight 0.08
contribution   this market's historical useful-call / Top-K / winner rate      weight 0.10
               from live Price Memory (0.5 if untried — never a penalty)

base  = sum(weight_i * component_i) / sum(weight_i)
score = clamp[0,1]( base + SEARCH_INTEL_EXPLORE_BONUS  if stance == EXPLORE )
```

Knowledge (`Knowledge` enum — `services/opportunity.py`) is `LIVE` (useful
recent live history), `PRIOR` (no useful live history but a confident bootstrap
prior), or `UNKNOWN` (neither). `LIVE`/`PRIOR` map to stance `EXPLOIT`;
`UNKNOWN` maps to `EXPLORE`.

**Cheapest does not automatically win** (§15) — a slightly dearer market with
frequent direct supply, a strong preference match and a history of
contributing to Top-K/winner results outranks a cheap-but-rare/unproven one.
Verified directly:
`tests/test_v9_market_prior.py::test_A_cheap_but_rare_does_not_beat_dearer_but_useful`
(≈0.62 for the cheap/rare/weak-fit market vs ≈0.80 for the dearer/frequent/
well-fit one with contribution history) and reproduced end-to-end at catalog
scale in `scripts/bench_prior_value.py`.

## The candidate funnel (§20)

`services/candidate_funnel.py::run_funnel()`:

```
~200 catalog destinations
  -> hard eligibility        (enabled, acquisition_eligible, has airport, not avoided)
  -> cheap feasibility       (not the origin airport, plausible duration, must-visit forced in)
  -> Market Prior + Price Memory batch lookup   (one query each — no N+1)
  -> Market Opportunity scoring
  -> modest diversity adjustment (subregion soft-cap, §23)
  -> EXPLOIT / EXPLORE allocation (Phase 1's `acquisition_scoring.allocate` — EXPLORE floor preserved)
  -> bounded shortlist (<= slots)
  -> caller (real_supply.py) builds a bounded ProviderAcquisitionPlan against this shortlist only
```

Every stage count lands on `FunnelTrace` (`catalog_total`, `enabled_total`,
`eligible_total`, `feasible_total`, `prior_known_count`,
`live_history_known_count`, `fully_unknown_count`, `scored_total`,
`shortlisted_total`, `exploit_candidates`, `explore_candidates`,
`diversity_demoted`) and is carried into `SearchIntelligenceTrace.funnel`
(`models/search_trace.py`) — visible per-search via the Ops API.

### Three-level cold start (§21)

`Knowledge.LIVE / PRIOR / UNKNOWN` (above). Critically, `UNKNOWN` is not a
dead end: the funnel's exploit/explore allocation reserves a real slot share
for `UNKNOWN`/`EXPLORE` markets on every search (Phase 1's invariant — "no
destination with zero history should become permanently unreachable" —
carried forward unchanged), so a fully unknown market is always reachable, not
just after `LIVE` and `PRIOR` are both exhausted.

### No N+1 across ~200 destinations (§10)

`persistence/market_priors.py::priors_for_destinations()` and
`services/market_intel.py::batch_market_signals()` are each a single
`WHERE ... IN (...)` query for *every* candidate destination in one search —
never one query per destination. Verified at scale in
`scripts/bench_catalog_scale.py`: SQL query count is flat (3 queries) across
catalog sizes 16 / 50 / 100 / 203.

### Modest diversity, no hard quotas (§23)

`candidate_funnel._diversity_adjust()` — the Nth candidate from a subregion
beyond a soft cap (default 3) loses a small score penalty per extra, then the
list is re-sorted. A strong single outlier from an over-represented subregion
still wins its slot; ten near-identical cluster markets do not crowd out
comparable alternatives elsewhere. There is no hard "one per region" rule.

## The hard provider-call budget is unaffected by catalog size (§24–§26)

The catalog going from 16 to ~203 destinations, and every destination having a
Bootstrap Market Prior, changes **which** markets the funnel shortlists. It
never changes **how many** real provider calls a search can make:
`services/acquisition.py::ProviderCallBudget` (`max_offer_requests`,
`max_destinations`, `max_date_variants`, `max_airport_variants`) still governs
`build_plan()`, which only ever constructs inter-city / outbound / return
edges among the funnel's small shortlist (`<= max_destinations`), never across
the whole catalog. Adversarial proof:

* `tests/test_v9_phase2_budget.py::test_full_catalog_with_prior_everywhere_stays_within_budget`
  and `..._with_zero_prior_and_zero_live_history_stays_within_budget` — the
  full ~203-city catalog, prior seeded for every market (or none), multiple
  date variants, both directions, inter-city edges all enabled — real provider
  calls stay `<= max_offer_requests`.
* `test_build_plan_never_forms_all_pairs_over_the_full_catalog` — a naive
  all-pairs inter-city fan-out over 203 cities would be ~35,000+ edges; the
  actual plan is bounded by the funnel's shortlist (`<= 100` edges even with
  `max_offer_requests` set absurdly high).
* `test_flexible_wide_date_range_still_bounded_by_max_date_variants` and
  `test_bootstrap_horizon_buckets_never_become_query_dates` — a 5-month-wide
  flexible date range still yields `<= max_date_variants` actual query dates,
  and a prior's horizon buckets (14/30/45/... days out) never leak into the
  acquisition plan's `days` — they are intelligence only (§26).

## Catalog architecture (§17–§19)

`models/destination.py::Destination` gained geography/governance fields:
`country_code` (ISO alpha-2), `region`, `subregion`, `timezone`,
`latitude`/`longitude`, `secondary_airports` (reference only — acquisition
never fans out to them, only the single `primary_airport`, §18),
`tags`, `enabled`, `acquisition_eligible`, `metadata_source`. A model
validator (`_acq_needs_airport`) refuses to construct an
`acquisition_eligible and enabled` destination with no `primary_airport` — the
governance rule is enforced at the type level, not by convention.

The 12 experience attributes (`history` … `adventure`) default to `0.5`
("average") so a ~200-city catalog built from broad, truthful tags
(`data/european_catalog.py::profile_from_tags()`) never fabricates precise
personality scores it does not actually have; the original 16 core
destinations still set every attribute explicitly.

`data/destinations.py`:

* `CORE_DESTINATIONS` (16) — unchanged, the synthetic beam-searchable network
  with hand-authored `synthetic_transport` links. `StaticDestinationProvider()`
  still defaults to exactly this set, so the synthetic optimizer path is
  byte-for-byte unaffected by Phase 2.
* `EUROPEAN_CATALOG` (`data/european_catalog.py`, 187 curated cities) — broad
  coverage across Iberia, France, Italy, Central Europe, the Balkans, Greece,
  the Nordics, the Baltics, the UK & Ireland, Benelux and Anatolia; capitals
  and city-breaks; no single country holds more than ~10% of the catalog.
* `DESTINATIONS = CORE_DESTINATIONS + EUROPEAN_CATALOG` (203 total) — the
  discovery catalog the candidate funnel reasons over.
* `acquisition_catalog()` — every `enabled and acquisition_eligible` entry
  with a usable `primary_airport`; this is what a real search may ever spend a
  provider call discovering, and it is `>150`, not `16`.

Origin is independent of the catalog (§19): `services/real_supply.py::resolve_airport()`
treats any well-formed 3-letter code as a valid origin even if it names no
catalog city — a traveller may depart from an airport Detoura has no
destination profile for at all.

## What Phase 2 deliberately does *not* do

* **No unauthorized scraping.** See `docs/V9_MARKET_PRIOR_SOURCE_OPTIONS.md`.
  No source shipped in this phase touches a consumer-facing website.
* **No scheduled background crawling.** §27's refresh strategy (below) is a
  design, not a running job — nothing in Phase 2 schedules an external fetch.
* **No opaque ML.** Confidence, decay and opportunity scoring are documented
  closed-form formulas with named, tunable weights (`search_intel_config.py`),
  not a trained model.
* **No UI redesign.** The Ops additions are read-only JSON endpoints behind
  `require_ops` (`api/ops_search_intel.py`); no frontend change.

## Bootstrap refresh strategy (§27 — design only)

A future scheduled job (not built or scheduled in Phase 2) would run
`services/market_prior_import.run_import()` against a real, approved source on
a bounded cadence, prioritized as:

1. **High-traffic origins** — the `bootstrap_origin_airports` configured in
   `search_intel_config.py` (`MARKET_PRIOR_BOOTSTRAP_ORIGINS`, 30 major
   European airports by default) refresh first.
2. **High-value / high-uncertainty markets** — destinations the candidate
   funnel has recently classified `UNKNOWN` or low-confidence `PRIOR` at
   non-trivial `preference_affinity`, from `Ops /knowledge` and
   `/market-prior` observability.
3. **Stale rows** — anything past `stale_row_count()`
   (`persistence/market_priors.py`), refreshed before it decays further.
4. **Under-covered markets** — destination/origin pairs with zero prior rows
   at all, lowest priority (they already fall back correctly to cold-start
   EXPLORE — refreshing them is an optimization, not a correctness fix).

The job would call `run_import(..., dry_run=True)` first, inspect
`rows_rejected`, and only then commit — exactly the pattern
`tests/test_v9_market_prior.py` exercises. It is explicitly **not**
Detoura crawling anything itself; it is a scheduled pull from whichever
APPROVED source (per the source-options doc) is under contract at the time.

## Economics — prior updates vs Duffel search economics (§28–§29)

`market_prior_imports` records, per import: rows seen/imported/updated/
rejected/skipped, markets/origins/destinations covered, and the *source's own*
reported request count / cost / rate-limit events — each `None` (never `0`)
when the source does not report it. This is deliberately separate from
`services/provider_economics.py`'s Duffel search-to-book economics (Phase 1,
unmodified): a prior import can be free (a licensed flat-file drop) or
metered (a paid API), and conflating the two ledgers would make either one
unauditable. Provider calls *avoided* by having a prior (a market scored
`PRIOR`/`EXPLOIT` instead of spending an `EXPLORE` call on it) are visible via
`FunnelTrace` counts, never asserted as a specific money figure — no cost
model is configured in this phase, so no currency amount is claimed for it
(`SearchEconomicsSnapshot.economics_configured` stays the source of truth for
whether Duffel-side money figures may be shown at all).

## Ops observability (§41)

All behind `require_ops` (`api/ops_search_intel.py`):

* `GET /api/v1/ops/search-intel/catalog` — catalog totals, core-network size,
  enabled/eligible counts, countries/subregions covered, per-subregion counts.
* `GET /api/v1/ops/search-intel/market-prior` — coverage summary (rows,
  markets, origins, destinations, by-source, by-confidence, oldest/newest
  source date, last import), stale-row count; stamped `not_a_quote: true`.
* `GET /api/v1/ops/search-intel/market-prior/imports` — recent import runs.
* `POST /api/v1/ops/search-intel/market-prior/prune` — retention cleanup,
  touching only `market_priors` (never `price_observations`,
  `booking_economics`, `audit_events`, `bookings`, `ticket_operations`).
* `GET /api/v1/ops/search-intel/knowledge` — LIVE/PRIOR/UNKNOWN candidate
  counts aggregated from the funnel trace of recent searches; a pre-Phase-2
  trace with no `funnel` field is skipped, never reported as a false zero.

## Benchmarks (§37–§39)

* `scripts/bench_catalog_scale.py` — funnel latency and SQL-query count at
  catalog sizes 16/50/100/~203. Query count is flat (3) at every scale;
  latency grows sub-linearly with candidate count and stays under ~35ms even
  at the full catalog.
* `scripts/bench_prior_value.py` — a synthetic world with three "hidden gem"
  destinations placed deep in a 60-city slice, weak preference fit, strong
  underlying market. Identical budget/slots across three runs: cold (no
  prior, no live) discovers 0/3; a Bootstrap Market Prior discovers 2/3;
  Detoura's own live Price Memory (with a contribution-history bonus)
  discovers 3/3 — a monotonically improving, reproducible story (bit-for-bit
  identical across repeated process runs — see the fixture-determinism note
  below), not a rigged one.
* `scripts/bench_legacy_vs_200.py` — an opportunity destination that exists
  only outside the legacy 16. The legacy catalog structurally cannot discover
  it (it is not a candidate); the expanded catalog does, given a prior — with
  identical `ProviderCallBudget` edge counts in both runs.

## Known limitations (honest, not hidden)

* No real external bootstrap source is contracted yet — see
  `docs/V9_MARKET_PRIOR_SOURCE_OPTIONS.md`. Every prior in this codebase today
  is fixture/test data or an operator-approved manual import.
* The catalog's 12 experience-attribute values for the 187 curated cities are
  broad tag-derived defaults (`profile_from_tags`), not individually
  researched personality scores — deliberately, per §17, rather than invented
  precision.
* The diversity adjustment (§23) is a soft, interpretable penalty, not a
  guarantee of exact geographic spread every search.
* `bench_prior_value.py`'s "prior" scenario recovers 2 of 3 hidden gems, not
  3 of 3 — this is the real output of the scoring formula (a single,
  moderately-confident bootstrap prior competes against a richer
  live-history-plus-contribution signal in the "live" run, as designed) and
  is reported as such rather than tuned to look better.

## Release-gate fix: fixture determinism (found during the Phase 2 gate)

`FixtureMarketPriorSource` (`services/market_prior_source.py`) originally
derived its synthetic prices from Python's builtin `hash()` on
`(origin, destination)` tuples. CPython salts `str`/`tuple` hashing per
process (`PYTHONHASHSEED` randomization) unless explicitly disabled, so two
runs of the same fixture in two different processes produced different
synthetic prices — contradicting the "deterministic" claim this module and
`docs/V9_MARKET_PRIOR_SOURCE_OPTIONS.md` make, and undermining exactly the
kind of run-to-run benchmark comparison this document relies on. This was
caught by re-running `scripts/bench_prior_value.py` twice during the release
gate and seeing the "prior" scenario's hidden-gem recovery rate move between
runs with no code change. Fixed by hashing with `zlib.crc32` over an explicit
string encoding instead of builtin `hash()` — stable across processes and
Python versions. All three benchmark scripts now produce byte-identical
output across repeated runs; this was re-verified (`diff` on repeated JSON
output) as part of the gate before benchmark numbers were reported.
