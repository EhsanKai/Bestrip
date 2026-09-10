# V9 Phase 1 — Search Intelligence Foundation

Branch `claude/travel-planner-mvp-nvb267`. Backend only — the V9 consumer UI/UX
redesign is out of scope and owned separately. Duffel **TEST MODE only**.

Phase 1 builds a **persistent, interpretable, statistical** market-intelligence
layer. It is **not** a machine-learning milestone and does **not** claim
adaptive-acquisition superiority — that benchmark comes in a later V9 phase.

---

## Architecture

```
real-supply search
  │
  ├─ SearchIntelRecorder (services/search_intel_recorder.py)
  │     ├─ scores candidate markets vs Price Memory       (acquisition_scoring.py)
  │     ├─ classifies each EXPLOIT / EXPLORE + allocates  (acquisition_scoring.allocate)
  │     ├─ mints acquisition_call_id per provider call
  │     ├─ tags every TransportOption.acquisition_call_id
  │     ├─ writes 1 PriceObservation per answered edge
  │     ├─ attributes optimizer / Top-K / winner back to calls
  │     └─ persists observations + SearchIntelligenceTrace, prunes retention
  │
  ├─ acquire_real_supply(..., recorder=…)   (services/real_supply.py)
  ├─ live_search(..., recorder=…)           (services/live_search.py)
  │
  ▼
persistence/price_memory.py  ──►  price_observations, search_traces   (SQLite, schema v5)
services/market_intel.py     ──►  HistoricalPriceSignal per market & currency
services/provider_economics.py ─►  search-to-book economics (config-driven, UNKNOWN-safe)
api/ops_search_intel.py      ──►  Ops-authenticated read access
```

The recorder is **inert** when `SEARCH_INTEL_ENABLED=0`; search is unaffected.

## Price Memory vs Cache — a hard boundary

| | Cache (`providers/cache.py`) | Price Memory (`persistence/price_memory.py`) |
|---|---|---|
| purpose | avoid repeating an equivalent live provider request | historical market observation for acquisition + analytics |
| lifetime | seconds–minutes (offer expiry) | days (`PRICE_MEMORY_RETENTION_DAYS`, default 180) |
| survives restart | no | **yes** |
| can satisfy a request without a provider call | yes | **never** |
| is a current quote | the cached value is | **never** — see below |
| used at checkout / revalidation | (indirectly, via the acquisition it feeds) | **never** |

Enforced boundaries:

* `PriceObservation` has **no** `.amount` / `.as_quote` / `.current_price` /
  `.bookable_amount` accessor. `HistoricalPriceSignal` likewise, and every one
  carries `not_a_quote = True`. (`test_price_observation_has_no_bookable_amount_accessor`)
* `services/revalidation.py`, `services/booking_commercial.py`,
  `services/booking_flow.py`, `services/recheck.py` do **not** import
  `price_memory`, `market_intel` or `search_intel`.
  (`test_checkout_and_revalidation_never_import_price_memory`)
* `providers/cache.py` and `persistence/price_memory.py` share no symbols.
  (`test_cache_and_price_memory_are_independent_modules`)
* The Ops API returns market fields named `*_observed` and a `not_a_quote` flag —
  never `current_price` or `fare`. (`test_market_signal_is_flagged_not_a_quote`)

## PriceObservation

Fields (`models/search_intel.py`): identity (`observation_id`, `observed_at`,
`provider`); market (`origin`, `destination`, `departure_date`, `trip_shape`,
`return_date`, `travelers`, `travelers_bucket`); offer characteristics as
observed (`total_amount_minor`, `per_person_minor`, `currency`, `direct`,
`stops`, `marketing_carrier`, `operating_carrier`, `cabin`, baggage,
`offer_count_for_edge`); acquisition context (`search_id`,
`acquisition_call_id`, `search_mode`, `candidate_reason`, `exploration`,
`candidate_rank`, `provider_call_ordinal`, `provider_call_budget`); quality /
outcome (`normalized_ok`, `retained_after_limits`, `entered_candidate_set`,
`contributed_to_top_k`, `contributed_to_winner`).

**No provider raw payloads.** One observation per answered edge — the cheapest
retained offer, keyed by the **resolved IATA airport** (not the catalog city
id), with `offer_count_for_edge` for representativeness. Money is integer minor
units with an explicit currency.

## SearchIntelligenceTrace

`models/search_trace.py`. One per real-supply search: `search_id`, timings,
origin / dates / mode / travelers, `provider_call_budget` /
`provider_calls_used` / `_failed`, `cache_hits` / `_misses`, `calls_explore` /
`calls_exploit`, a `CandidateDecision` per market (pre-rank, stance, selected,
reason, confidence, baseline score + components), a `ProviderCallOutcome` per
call (offers received/normalized/retained, error/timeout/rate-limit, elapsed,
contribution flags), optimizer outcome counts, and a `SearchEconomicsSnapshot`.
Derived rates: `explore_fraction`, `useful_call_rate`,
`top_k_contribution_rate`, `winner_contribution_rate`.

**No traveller PII** — no name, email, phone, DOB or document anywhere. The
`search_id` is an operational attribution handle, not a person.
(`test_no_pii_columns_in_price_observations_or_traces`,
`test_trace_json_contains_no_pii`, `test_recorder_source_never_reads_traveler_pii`)

## Provenance chain (contribution attribution)

```
acquisition_call_id  (search_id : ordinal)
  → offers                        recorder.wrap_fetch tags each TransportOption
  → normalized offers             retained_after_limits
  → candidate edges / Itinerary.legs   the optimizer passes TransportOption through untouched
  → optimizer results             recorder.attribute() walks recommendations
```

`recorder.attribute(recommendations)`:

* every call whose id appears on any recommendation leg →
  `entered_candidate_set` (class C)
* … on a Top-K (default 5, `SEARCH_INTEL_TOP_K`) recommendation → class D
* … on the winner (`rank == 0`) → class E

Classes A (no usable offer) / B (usable but unused) fall out of the retained /
candidate flags. IDs are **propagated, not inferred** — `TransportOption`
carries `acquisition_call_id` and the optimizer never rewrites it.
(`test_attribution_marks_optimizer_top_k_and_winner`,
`test_contribution_classes_are_written_back_to_observations`)

## Aggregation (`services/market_intel.py`)

`HistoricalPriceSignal` per `MarketKey` **and per currency** — EUR and USD
observations are never merged and **no FX is invented**; a market observed in
two currencies produces two signals. `batch_market_signals` answers many
markets in **one SQL query** (no N+1). Percentiles (`cheap` = P25, `median`,
`expensive` = P75) are **suppressed** below `PRICE_MEMORY_MIN_SAMPLES`
(default 3).

## Freshness / confidence formula (deterministic — not ML)

Per market + currency, each component in `[0, 1]`:

```
sample_component      = min(1, sample_count / 12)
recency_component     = max(0, 1 - age_days(freshest) / PRICE_MEMORY_FRESHNESS_HORIZON_DAYS)   # default 45
consistency_component = 1 - min(1, price_cv)          # price_cv = stdev/mean of per-person prices; 0.5 when < 2 samples

score   = 0.45 * sample_component + 0.35 * recency_component + 0.20 * consistency_component

verdict = NONE   if sample_count == 0
          LOW    if score < 0.34            (also: LOW whenever sample_count < PRICE_MEMORY_MIN_SAMPLES)
          MEDIUM if score < 0.67
          HIGH   otherwise
```

Every component is returned alongside the verdict, so the score is always
explainable. One recent observation caps at LOW; dozens of consistent recent
observations reach HIGH. (`test_stale_history_is_not_as_trustworthy_as_recent`,
`test_sparse_history_suppresses_percentiles_and_is_low_confidence`)

## Baseline acquisition score (`services/acquisition_scoring.py`)

**The first** interpretable historical score — not the final V9 adaptive
engine. Weighted sum, each component in `[0, 1]`:

```
price_attractiveness  cheaper historical median vs the candidate-field median,
                      credited only at confidence >= MEDIUM, clamped [0,1];
                      0.5 (neutral, never a penalty) when there is no usable history
confidence            NONE:0 LOW:0.25 MEDIUM:0.6 HIGH:1
freshness             the recency component
preference            destination affinity in [0,1]  (0.5 when unknown)
feasibility           1 if the destination resolves to an airport, else 0

base  = (w_price*price + w_conf*conf + w_fresh*fresh + w_pref*pref + w_feas*feas) / Σw
score = clamp[0,1]( base + (exploration_bonus if stance == EXPLORE else 0) )
```

Defaults: `w_price 0.30`, `w_conf 0.20`, `w_fresh 0.15`, `w_pref 0.20`,
`w_feas 0.15`, `exploration_bonus 0.10` — all env-tunable
(`SEARCH_INTEL_W_*`, `SEARCH_INTEL_EXPLORE_BONUS`).

**Historical cheapness cannot dominate**: `w_price` is 0.30, and an unseen
market scores a *neutral* 0.5 on price rather than 0, so Detoura does not
repeatedly re-query the historically-cheapest markets and starve the rest.
(`test_historical_cheapness_alone_does_not_dominate`)

## Explore / exploit (`allocate()`)

A candidate is **EXPLOIT** iff it has usable history at MEDIUM or HIGH
confidence; otherwise **EXPLORE**. `allocate(scored, slots)` then reserves

```
explore_target = clamp( max(SEARCH_INTEL_EXPLORE_MIN_SLOTS,               # default 1
                            round(slots * SEARCH_INTEL_EXPLORE_FRACTION)), # default 0.20
                        0, slots )
```

EXPLORE slots, filled with the best-scoring EXPLORE candidates ahead of
marginal EXPLOIT ones, backfilling either pool if the other is short.

**Invariant — no destination with zero history is permanently unreachable.**
Two layers guarantee this: (1) `rank_candidates` already reserves ~25 % of the
destination *pool* for cities affinity rejected; (2) at any realistic budget
(`max_destinations` = 8 → `explore_target` ≥ 2) at least one EXPLORE slot
survives allocation every search. (`test_unseen_destination_stays_reachable_under_a_budget`,
benchmark cold-start = 100 % explore)

*Degenerate case:* with a 1-destination budget, `explore_target` is 0 and the
single slot goes to the top score; raise the budget for guaranteed
per-search exploration. The pool reservation still applies.

## Cold start

Empty DB / unseen origin / unseen destination / no route-date history / stale
or tiny samples → every candidate scores as EXPLORE and selection falls back to
the incoming deterministic rank order. **Historical intelligence improves
search; search correctness never depends on it.**
(`test_empty_price_memory_scores_everything_as_explore`,
`test_cold_start_is_all_explore_then_warm_markets_become_exploit`,
`test_disabled_recorder_is_a_noop`)

## Retention (`persistence/price_memory.prune`)

`price_observations` (and `search_traces`) older than
`PRICE_MEMORY_RETENTION_DAYS` (default 180, **configuration** not policy) are
deleted. The prune query is a single `DELETE FROM price_observations WHERE
observed_at < ?` — it references **no other table**. `booking_economics` and
`audit_events` are immutable and out of its reach by construction.
(`test_retention_prune_only_touches_observations`) Run automatically at the end
of each recorded search, and on demand via `POST /api/v1/ops/search-intel/prune`.

## Provider search economics (`services/provider_economics.py`)

Duffel's search-to-book excess terms are `ProviderEconomicsConfig` (env:
`DUFFEL_INCLUDED_SEARCHES_PER_BOOKING`, `DUFFEL_INCLUDED_SEARCHES_FLAT`,
`DUFFEL_EXCESS_SEARCH_FEE`, `DUFFEL_EXCESS_SEARCH_CURRENCY`,
`DUFFEL_EXCESS_RATIO_THRESHOLD`) — **never hardcoded**. Tracked:
`provider_searches` (Σ `provider_calls_used`), `bookings_attributable`
(distinct bookings with a Duffel item), rolling `search_to_book_ratio`,
`included_search_allowance`, `estimated_excess_searches`,
`estimated_excess_search_cost`.

**UNKNOWN stays UNKNOWN.** With no terms configured, every derived figure is
`None`; with no bookings yet, the ratio and a per-booking allowance are `None`.
Nothing reports `€0` / `$0` for an unknown cost.
(`test_economics_unknown_when_not_configured`,
`test_search_to_book_ratio_is_none_without_bookings`)

## Provider call budget & zero-network invariant

The V8.5 hard `ProviderCallBudget` is **untouched**. The recorder changes
*which* calls are made (via `preselected` into `build_plan`), **never how many**
— `max_offer_requests` still binds and `DuffelTransportProvider.max_calls` is
still the backstop. `SnapshotTransportProvider` still has no client / host /
token, so the beam still makes **zero** provider network calls by construction.
(`test_provider_budget_is_never_exceeded_by_the_recorder`,
`test_zero_network_optimizer_invariant_holds`) The trace records `budget`,
`calls_used`, `calls_failed`, `cache_hits/misses`, `calls_explore`,
`calls_exploit`.

## Privacy

No `given_name` / `family_name` / `email` / `phone` / `born_on` / passport /
payment field is persisted or exposed anywhere in this layer — not in
`price_observations`, `search_traces`, the trace JSON blob, or any Ops DTO. The
recorder source never touches `run.party`. `search_id` / `acquisition_call_id`
are random operational handles. No protected/sensitive personal attribute is a
scoring input (the scorer sees market stats, preference affinity and
feasibility only).

## Ops API (`api/ops_search_intel.py`, all `require_ops`)

| route | returns |
|---|---|
| `GET /api/v1/ops/search-intel/overview` | coverage, contribution rollup, exploration rate, economics |
| `GET …/markets?order=samples\|weak` | markets by sample count (most / weakest) with candidate/Top-K/winner rates |
| `GET …/market?origin=&destination=&departure_date=&travelers=` | the `HistoricalPriceSignal` per currency, `not_a_quote: true`, fields named `*_observed` |
| `GET …/traces` / `…/traces/{search_id}` | recent traces / one full trace |
| `POST …/prune` | run retention now, returns counts |

The Search Intelligence Ops **UI/UX is designed separately**; this is the
backend it will read.

## Benchmark (`scripts/bench_search_intel.py`)

`python3 scripts/bench_search_intel.py --runs 5` — deterministic synthetic
fake-Duffel run. Representative output:

```
cold_start : 8 candidates, 4 selected, budget 48, 20 calls, 0 exploit / 20 explore  (explore_fraction 1.0)
warm       : 8 candidates, 4 selected, budget 48, 20 calls, 12 exploit / 8 explore  (explore_fraction 0.4)
attribution_demo : 20 calls → 6 candidate, 6 top-K, 2 winner
lookup_overhead  : 8 candidate markets, one query, ~0.9 ms  (baseline ~0.0 ms)
warm_search_latency : ~10 ms median
price_memory : 120 observations, 28 markets, 6 traces
```

Cold start is 100 % exploration; once history accumulates the same budget
shifts toward exploitation while never dropping exploration below its floor.
Price Memory lookup adds ~1 ms for 8 candidate markets (single batched query).

**This is the baseline for the real adaptive-acquisition benchmark in a later
V9 phase.**

## What Phase 1 does NOT claim / does NOT do

* **No claim** that adaptive acquisition beats fixed acquisition — the benchmark
  measures instrumentation, not outcome quality.
* **Not ML.** Every score is a documented deterministic formula.
* Price Memory is **never** a bookable or current price, and no endpoint exposes
  it as one.
* The production `/api/v1/search` endpoint is still synthetic-only; the recorder
  is wired into the **real-supply path** (`live_search` / `acquire_real_supply`),
  which no consumer endpoint yet calls. Wiring a real-supply consumer endpoint is
  future work.
* Cross-currency aggregation is **refused** (per-currency signals only) — no FX.
* The benchmark's synthetic fixture does not always yield a bookable multi-city
  itinerary, so its `useful_call_rate` can be 0; contribution attribution
  correctness is covered by unit tests and the `attribution_demo`.
* No Search Intelligence consumer or Ops **UI** — API only.
* `execute_change`-style adaptive re-planning inside a single search is not done;
  the recorder classifies and records, and steers destination selection within
  the existing budget.

## Config reference

| env var | default | meaning |
|---|---|---|
| `SEARCH_INTEL_ENABLED` | `1` | master switch; `0` → recorder inert |
| `PRICE_MEMORY_RETENTION_DAYS` | `180` | observation/trace prune age |
| `SEARCH_INTEL_TOP_K` | `5` | Top-K cut for attribution + metrics |
| `SEARCH_INTEL_W_PRICE` / `_CONFIDENCE` / `_FRESHNESS` / `_PREFERENCE` / `_FEASIBILITY` | `0.30 / 0.20 / 0.15 / 0.20 / 0.15` | scoring weights |
| `SEARCH_INTEL_EXPLORE_BONUS` | `0.10` | added to EXPLORE candidate scores |
| `SEARCH_INTEL_EXPLORE_FRACTION` | `0.20` | target EXPLORE share of the budget |
| `SEARCH_INTEL_EXPLORE_MIN_SLOTS` | `1` | minimum EXPLORE slots when budget > 1 |
| `PRICE_MEMORY_MIN_SAMPLES` | `3` | below this: percentiles suppressed, confidence capped LOW |
| `PRICE_MEMORY_CHEAP_PCT` / `_EXPENSIVE_PCT` | `0.25 / 0.75` | reference percentiles |
| `PRICE_MEMORY_FRESHNESS_HORIZON_DAYS` | `45` | age at which recency contribution → 0 |
| `DUFFEL_INCLUDED_SEARCHES_PER_BOOKING` | — | per-booking search allowance (unset → UNKNOWN) |
| `DUFFEL_INCLUDED_SEARCHES_FLAT` | — | flat search allowance |
| `DUFFEL_EXCESS_SEARCH_FEE` | — | fee per excess search (unset → UNKNOWN) |
| `DUFFEL_EXCESS_SEARCH_CURRENCY` | `EUR` | currency of the fee |
| `DUFFEL_EXCESS_RATIO_THRESHOLD` | — | search-to-book ratio triggering excess billing |
