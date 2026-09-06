# Detoura V7 — Phase 0 Architecture Audit

**Baseline commit:** `c20fbf4` (V6.5, released)
**Audit type:** read-only. No source file was modified to produce this document.
**Verified checkout:** repository root, branch `claude/travel-planner-mvp-nvb267`, HEAD `c20fbf4`, working tree carrying only the frozen frontend and `CLAUDE.md`.

---

## 1. Current architecture

The system is a deterministic optimizer wrapped in a product API. The layering is strict and worth preserving:

```
api/contracts.py   product DTOs — no beam, no Pareto, no search state
api/assembler.py   one-way translation engine result -> product DTO
api/v1.py          endpoints (15 routes)
        |
services/planner.py     TravelPlanner.plan() — the domain entry point
        |
algorithms/beam_search.py   BeamSearchOptimizer (LOCKED unless coordinated)
algorithms/travel_value.py  the 9-component objective
algorithms/pareto.py        8-objective frontier
algorithms/diversity.py     Jaccard city-overlap dedupe
        |
providers/*   Protocols: transport, accommodation, ground_transfer, destinations
              wrapped Resilient(Caching(inner))
models/*      frozen Pydantic v2 + frozen slotted dataclasses for search state
```

**Request shape.** `TripRequest` (frozen) carries origin, budget, travelers, duration, a date window plus `date_flexible`, transport preferences, must/preferred/avoid destination lists, a 12-dimension `TravelPreferences` vector, `preferred_experiences`/`disliked_experiences`, `previously_visited`, `preferred_city_count`, accommodation preference, travel style and profile.

**Itinerary representation.** `SearchState` (frozen, slotted) is the search-time representation: origin airport, current location, route as a tuple of `TransportOption`, `cities` tuple, `stays` tuple of `CityStay`, three separate cost accumulators, travel/transfer minutes, and `visited_cities`. `Itinerary` is the result-time representation carrying the same facts plus `TravelValueBreakdown`, `ScoreBreakdown`, `explanation_factors`, `destination_insights` and `baseline_comparison`.

**Search modes.** `QUICK`/`SMART`/`DEEP` map to `PlannerConfig` via `apply_mode()`. Nothing in `beam_search.py` knows a mode exists — this is a product layer, not a second optimizer, and it must stay that way.

**Pareto objectives (8):** cost↓, travel_minutes↓, city_count↑, preference_score↑, usable_minutes↑, experience↑, accommodation↑, convenience↑ — quantized into human-noticeable boxes so the frontier does not degenerate into "everything".

**Beam state and admissibility.** `_estimate()` builds a hypothetical completion charging the cheapest return leg, the ride home *and* the nights still owed. An unknown return bound is passed as `None` and must never be read as unreachable — this is the V6 guarantee the roadmap restates in §45.

**LLM seams.** `llm/interfaces.py` defines exactly two Protocols: `PreferenceParser.parse(text) -> TripRequest` and `ItineraryExplainer.explain(itinerary) -> str`. Both current implementations are deterministic (keyword / template). No LLM touches price, route or feasibility today, which is precisely the invariant V7 §3 demands.

**Session state.** V6.5's `SessionStore` Protocol (`get`/`put`/`update`/`delete`/`cleanup_expired`) with in-memory and Redis implementations. `update()` takes a pure `mutate` closure so atomicity lives inside the store.

---

## 2. Reusable V6.5 components

These carry directly into V7 and must not be rewritten:

| Component | Why it carries |
|---|---|
| `BaselineResult` / `BaselineComparison` | Already the "user's original idea", already priced with hotel and transfers, already carrying `usable_destination_minutes` |
| `TravelValueScorer` | The single scoring path. V7 comparison must reuse it, not add a second one |
| `SearchState` | Already expresses locks implicitly: a partial state with fixed prefix cities is exactly what re-optimization needs |
| `pareto_filter` | Objective set already wide enough for V7's trade-offs |
| `SessionStore` | The persistence seam Phase 7 needs; no new abstraction required |
| `RelaxationSuggestion` + `relaxations()` | §43 constraint relaxation already has a typed, actionable shape in `assembler.py` |
| `Money` / `PriceNormalizer` | `PriceBasis` already distinguishes per-person / per-room-night / total — baggage pricing needs no new money model |
| `FailureLog` / `ProviderIssueDTO` | `UNKNOWN` is already a first-class answer; §20's "never invent a baggage fee" has a precedent to follow |
| `apply_mode` | Re-optimization modes can be expressed as config, not a new search path |

---

## 3. Architectural gaps

| # | Gap | Blocks | Severity |
|---|---|---|---|
| G1 | Baseline is *derived* (cheapest trip to `preferred_destinations[0]`), never *stated*. A user cannot say "my idea was Köln→Berlin, 5 days". | Phase 1 | Medium — additive |
| G2 | `BaselineComparison` has 4 fields; §9 needs 8 deltas. No experience/preference/accommodation metrics on the baseline at all. | Phase 1 | Medium — additive |
| G3 | No `TripPatch`, no lock/exclude semantics, no similarity objective. Nothing in the engine can be told "keep Hamburg". | Phase 2 | High — new subsystem |
| G4 | **No baggage concept anywhere.** `TransportOption` has no baggage field. Every advertised fare is implicitly whatever the provider quoted. | Phase 3 | High — new subsystem, touches price truth |
| G5 | Budget is a single hard number. No target/ceiling split, no above-target results. | Phase 4 | Medium |
| G6 | `_candidate_pool()` truncates `catalog[:limit]` — **catalog-order truncation**, exactly the antipattern §28 forbids. Inert today (`max_candidate_destinations=None`, 16 cities), live and harmful at 50+. | Phase 5 | **High** |
| G7 | `pareto_filter` is **O(n²)**. Measured: 18.3M vector comparisons over 10,566 completed itineraries, ~40% of DEEP wall time. | Phase 5 | **High** |
| G8 | Origin resolution is a hand-written distance table: 7 origin cities × 5 airports. No `LocationEntity`, no coordinates, no station codes, no autocomplete service. | Phase 6 | Medium |
| G9 | Deliberately stateless — "Detoura stores nothing: saved trips live in the browser" (`contracts.py`). Phase 7 reverses a documented design decision. | Phase 7 | Medium — intentional change, not a contradiction |
| G10 | `SearchMode.estimated_seconds` advertises SMART at 0.8–3.0s and DEEP at 8–20s. Measured below: SMART-flexible 6.8–8.5s, DEEP 20.3s on a realistic request. Pre-existing honesty gap, not introduced by V7. | Product copy | Low, but it is a truth claim |

---

## 4. Proposed V7 models

Additive only. No existing model is replaced.

```
OriginalTripBaseline    the user's stated idea, priced and scored through the
                        SAME TravelValueScorer as every recommendation
TripComparison          8 signed deltas + a verdict that can say "yours wins"
TripPatch               typed ops: lock/unlock/remove/replace/add/exclude city,
                        stay duration, budget, baggage, departure/return
ConstraintSet           HARD vs SOFT, with hard violations impossible by
                        construction rather than checked afterwards
TripSimilarity          editing-only diagnostic; NOT wired into discovery scoring
ReoptimizationResult    new trip + structured change diff + impact
BaggagePolicy /         PERSONAL_ITEM_ONLY | CABIN_BAG | CHECKED_BAG,
BaggageSelection        with UNKNOWN as a first-class, never-invented answer
BudgetEnvelope          target_budget + hard_ceiling
LocationEntity          CITY | AIRPORT | STATION | REGION + aliases + codes
```

**The single most important design decision:** the original trip must be scored by `TravelValueScorer` through a real `SearchState`, not by a parallel calculation. `BaselinePlanner` already selects concrete legs and a concrete room; building a `SearchState` from them costs nothing and makes §10's "never manipulate metrics to make Detoura appear superior" a structural property rather than a promise. Any second scoring path is a manipulation vector.

---

## 5. Dependency graph

```
Phase 1  OriginalTripBaseline + TripComparison
           |  (comparison exposes baggage_delta as UNKNOWN until Phase 3)
           v
Phase 2  TripPatch + locks + TripSimilarity + re-optimization
           |  (reuses SearchState prefix; needs beam_search coordination)
           v
Phase 3  Baggage ---> retro-fills Phase 1 baggage_delta
           |          and changes which option is actually cheaper
           v
Phase 4  Flexible budget (needs baggage totals to be meaningful)
           |
Phase 5  50+ cities  <-- BLOCKED BY G6 and G7; both must be fixed first
           |
Phase 6  Location intelligence (independent of 1-5; can run parallel)
           |
Phase 7  Accounts / workspace (needs SessionStore; independent of optimizer)
           |
Phase 8  Natural language -> TripPatch (needs Phase 2 stable and validated)
```

Phase 6 is the only phase genuinely parallelisable with the P0 line.

---

## 6. Expected files to modify

**Phase 1 (this checkpoint):**
- `models/itinerary.py` — extend `BaselineResult` with scored fields (additive, defaulted)
- `services/baseline.py` — emit a `SearchState` alongside the result
- `services/trip_comparison.py` — **new**
- `services/planner.py` — pass the scorer into the baseline path
- `api/contracts.py` — `TripComparisonDTO`
- `api/assembler.py` — translate it
- `tests/test_v7_comparison.py` — **new**

**Later phases:** `models/baggage.py`, `models/patch.py`, `services/reoptimizer.py`, `services/similarity.py`, `services/candidates.py`, `services/locations.py`, `data/destinations.py`, `algorithms/pareto.py` (G7), `algorithms/beam_search.py` (G6 — coordinated, currently locked).

---

## 7. Agent ownership map

| Agent | Owns | Active at |
|---|---|---|
| 1 Domain & Comparison | `services/trip_comparison.py`, `services/baseline.py`, `BaselineResult`, comparison tests | Checkpoint B |
| 2 Re-optimization | `models/patch.py`, `services/reoptimizer.py`, `services/similarity.py`, coordinated `beam_search.py` | Checkpoint C |
| 3 Pricing & Baggage | `models/baggage.py`, transport/accommodation price fields, budget envelope | Checkpoint D |
| 4 Destination Scale | `data/destinations.py`, `services/candidates.py`, `services/locations.py`, `origin_resolver.py`, `pareto.py` perf | Checkpoints E–F |
| 5 Independent QA | `tests/test_v7_*_adversarial.py`. **Implements no feature.** Rejection authority. | Every checkpoint |
| 6 Release custodian | Classification, selective staging, commits. Never `git add .` / `-A`. | Every checkpoint |

`beam_search.py` stays **locked** until Checkpoint C, when Agent 2 takes explicit ownership.

---

## 8. Risks

1. **Comparison dishonesty (highest product risk).** Any independent recomputation of the original trip's metrics can drift from how recommendations are scored, and the drift will flatter Detoura. Mitigated structurally by one scorer, one path.
2. **Baggage fabrication.** The pressure to show a total will push toward estimating an unknown fee. `UNKNOWN` must survive all the way to the DTO.
3. **Candidate retrieval silently killing exploration.** G6 makes this the default failure mode at 50 cities. Needs adversarial tests *before* the catalog grows.
4. **Pareto O(n²) at scale.** G7 turns a 50-city catalog into a latency cliff, not a gentle curve.
5. **Locks violated by re-optimization.** Must be structurally impossible, not validated after the fact.
6. **`beam_search.py` contention** between Agent 2 and Agent 4.
7. **Determinism loss.** Every V7 objective added to ranking risks introducing ties broken non-deterministically. `_rank_key` ends in `signature()` for this reason and must keep doing so.
8. **Frozen frontend.** 10 uncommitted frontend paths are unrelated pre-existing work. They must be neither staged nor reset.

---

## 9. Migration concerns

- `BaselineResult` gains fields — all defaulted, so V6 callers construct unchanged.
- `TripComparison` is a **new** DTO field on the response, not a replacement for `baseline_comparison`. Removing the old field would break the shipped frontend.
- Baggage changes displayed totals. Any saved trip re-checked after Phase 3 could appear to change price for a reason that is presentation, not market movement. `recheck` must distinguish these.
- Growing the catalog changes which trips win for existing requests. This is intended, but every published benchmark signature becomes invalid at that commit and must be re-baselined.
- No database migration exists or is needed before Phase 7.

---

## 10. Baseline performance

Measured on this host at `c20fbf4`. Each scenario ran in its own process; timings are uninstrumented (an earlier attempt with `tracemalloc` active inflated every number ~4x and was discarded), and peak RSS comes from `getrusage`.

Request: `Köln`, €900, 2 travelers, window `2026-09-10..2026-09-24`, history 0.9 / food 0.8 / culture 0.7, preferred destination `Berlin`.

| Scenario | Wall | Start dates | Beam | States | Completed | Pareto | Peak RSS | Best score |
|---|---|---|---|---|---|---|---|---|
| QUICK fixed 5d | 0.18s | 1 | 10 | 1,196 | 158 | 13 | 37.8 MB | 0.719869 |
| SMART fixed 5d | 1.02s | 1 | 20 | 9,730 | 1,076 | 68 | 40.8 MB | 0.734457 |
| SMART flexible 5d | 6.82s | 11 | 80 | 37,984 | 4,922 | 332 | 55.3 MB | 0.733288 |
| SMART fixed 7d | 1.18s | 1 | 20 | 10,066 | 1,124 | 110 | 41.4 MB | 0.736198 |
| SMART flexible 7d | 8.45s | 9 | 80 | 38,600 | 6,162 | 509 | 58.3 MB | 0.733115 |
| DEEP fixed 5d ×5 | 20.11 / 20.13 / 20.27 / 20.52 / 24.62s | 1 | 160 | 148,840 | 10,566 | 251 | 61.9 MB | 0.740693 |

**DEEP determinism: exact.** All five runs produced identical states generated, completed count, Pareto size, score and full ranked signature.

**DEEP adaptive ladder** (widths 20 → 40 → 80 → 160, cumulative seconds 0.85 / 2.35 / 5.71 / 11.75, stop reason `rounds_exhausted`):

| Width | Best score | Completed | Improvement | New city sets | Competitive |
|---|---|---|---|---|---|
| 20 | 0.734457 | 1,076 | — | — | — |
| 40 | 0.736652 | 2,192 | +0.002195 | 17 | 54 |
| 80 | 0.734457 | 5,424 | **−0.002195** | 20 | 98 |
| 160 | 0.740693 | 10,566 | +0.006236 | 11 | 241 |

Two things to record. The ladder stops on `max_rounds=4` before reaching its 320 ceiling or its 25s budget — so the time budget is *not* what bounds DEEP today. And beam search is **not monotonic in width**: width 80 scored strictly worse than width 40. That is expected for a heuristic, but it means "wider is better" cannot be assumed by any V7 performance work.

**Where DEEP's time goes** (cProfile, ~2x observer overhead, proportions valid):

| Stage | Cumulative | Share |
|---|---|---|
| `_adaptive_search` (4 rounds) | 25.0s | 60% |
| `_post_process` | 17.0s | **40%** |
| └ `pareto_filter` | 16.97s | 40% |
| └ └ `_dominates_vector` (18,345,319 calls) | 10.42s | 25% |

Pareto filtering is quadratic and already costs as much as two thirds of the search itself. **This is the single hardest constraint on Phase 5.**

### Current state of the V7 feature areas

| Area | Today |
|---|---|
| Supported cities | **16** (London, Brussels, Paris, Amsterdam, Prague, Vienna, Madrid, Barcelona, Milan, Rome, Dublin, Copenhagen, Budapest, Berlin, Munich, Zurich) + 5 origin airports (CGN, DUS, FRA, EIN, AMS) = 21 network nodes |
| Candidate generation | None. Whole catalog, in catalog order, filtered only by "not current" and "not visited". `max_candidate_destinations` truncates by list position. |
| Baggage assumptions | **None exist.** No field, no policy, no disclosure. Fares are whatever the provider quoted. |
| Original-trip baseline | Derived cheapest round trip to `preferred_destinations[0]`; `None` when the user named no preference. Priced with hotel + transfers. 4 comparison deltas. |
| Edit / re-optimization | **None.** Every change is a fresh search. |
| Budget | One hard number. No target/ceiling. |
| Persistence | None by design; saved trips live in the browser. |

### Baseline result signatures (regression anchors)

| Scenario | Top route | Price | Score |
|---|---|---|---|
| SMART fixed 5d | CGN → Berlin → Munich → CGN | see `v7_baseline.ndjson` | 0.734457 |
| SMART fixed 7d | CGN → Berlin → Prague → Vienna → CGN | — | 0.736198 |
| SMART flexible 5d | CGN → Copenhagen → Berlin → CGN | — | 0.733288 |
| DEEP fixed 5d | CGN → Berlin → Munich → CGN | — | 0.740693 |

Full ranked signatures for all ten runs are stored alongside this audit run and are the regression anchor for every later phase.

> These numbers describe **this** request on **this** host. They are not comparable to the V6.5 release measurements, which used a different request shape — that DEEP figure was 8.28s median. Nothing here indicates a regression; the workloads differ.

---

## 11. Verdict

**No architectural blocker to Phase 1.** The comparison work is additive, reuses `BaselineResult` and the existing scorer, and needs no change to `beam_search.py`.

Two findings are recorded as **hard prerequisites for Phase 5**, not for Phase 1: catalog-order truncation (G6) and quadratic Pareto filtering (G7). Both must be fixed before the catalog grows past 16 cities, or the 50-city expansion will be simultaneously slower and worse.

Proceeding automatically into Phase 1 per §7.
