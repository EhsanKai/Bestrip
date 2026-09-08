# Detoura V7 — Final Release Report

**Baseline:** `c20fbf4` (V6.5) · **Final V7 HEAD:** `b9ce87e` · **Remote parity:** `0 0`

| Phase | Commit | Subject |
|---|---|---|
| V7 Phase 1 | `7e34507` | compare the traveler's own idea against ours, honestly |
| V7 Phase 2 | `4686371` | add intent-preserving trip reoptimization |
| V7 Phase 3 | `b9ce87e` | enforce honest baggage-aware pricing |

**Suite at `b9ce87e`:** 1093 collected · **1070 passed** · **0 failed** · 0 errors · 23 skipped · 0 xfailed · 0 xpassed · exit 0.

Every skip is enumerated: **22** in `tests/test_v65_session_store.py` ("no Redis reachable") because the Docker daemon is down on this machine, and **1** pre-existing in `tests/test_v6_recheck.py` ("this search returned no transfer-free trip"). None is in V7 code.

---

## 1. Release gate — how Phase 3 was approved

**Agent 5 verdict: never issued.** This is recorded rather than papered over.

Agent 5 reviewed Phase 3 and returned **REJECTED** with four defects (three P0, one P1). All four were fixed. It then attempted re-verification **three times**, and all three attempts died on infrastructure — one account session limit and two watchdog stalls, the last while idle-waiting on a regression suite slowed by contention on this machine. **No attempt failed because it found an unresolved defect.**

Before failing, Agent 5 did independently confirm: all four code fixes correct by direct reading, the additional `BAGGAGE_NOT_AVAILABLE` factor a good addition, and its own baggage suite passing 93/93 — including the seven tests that had encoded the defects.

**Agent 6 verdict: `APPROVED_WITH_INFRASTRUCTURE_WAIVER`.** This is explicitly **not** a claim that Agent 5 approved. It is Agent 6's own fallback verification, run under an authorized waiver, covering:

- Phase 1 + Phase 2 regression suite: exit 0
- Phase 3 baggage suites uncontended: 95 tests, no XPASS/XFAIL
- All four rejected defect groups re-proven fixed (12 direct assertions)
- All named invariants re-proven (11 direct assertions)
- Full suite uncontended: exit 0, 0 failures
- Fail-closed secret scan over staged content: clean

**No known correctness defect remains.**

### The four defects, and what fixed them

| Sev | Defect | Fix |
|---|---|---|
| P0 | `FITS_BUDGET` / `GOOD_BUDGET_USAGE` asserted while fully-known baggage put the trip €55 over budget — a false claim made from complete information | Budget factors reason about `total_with_known_baggage` and `is_priceable`; emit exactly one of `FITS_BUDGET`, `BAGGAGE_EXCEEDS_BUDGET`, `BAGGAGE_COST_UNKNOWN`, `BAGGAGE_NOT_AVAILABLE` |
| P0 | One trip carried three disagreeing totals: `total_price` 546.76, `costs.total` 666.76, components summing to neither | `CostBreakdown.total` excludes baggage and equals `total_cost`; components sum to exactly it; `costs.baggage` added; all-in exposed as nullable `total_with_known_baggage` |
| P0 | A bag that cannot be carried at any price reported identically to a free one (`COMPLETE`, `total_for_display: 0.0`) | `is_priceable = is_complete and satisfiable`; unsatisfiable forces `PARTIAL_UNKNOWN`; comparison gates on it |
| P1 | A `$20` fee summed as `€20` — inventing a number by relabelling it | Foreign-currency fees count as unquoted; conversion belongs at the provider boundary where `PriceNormalizer` has rates |

A fifth issue was found by Agent 6 in the P0-1 fix itself: an unavailable bag was reporting *"Baggage cost unknown"*, conflating "we don't know the price" with "you can't bring this". `BAGGAGE_NOT_AVAILABLE` now separates them.

---

## 2. What V7 does

### Original vs Detoura (Phase 1)

The traveler's own idea is scored by walking it through the **same `TravelValueScorer` over a real `SearchState`**, built from its own legs, room and transfers. There is no parallel "baseline maths": two code paths would drift, and drift in a comparison always flatters the author's side.

Honesty guarantees, each enforced by test:

- The verdict can come out `ORIGINAL_BETTER`, and does.
- Every material metric lands in exactly one of `advantages` / `tradeoffs`.
- Losses are stated **first** in the summary, before any advantage is claimed.
- Materiality is judged on raw deltas, then rounded — never the reverse, so a rounding artefact cannot become a sales pitch.
- Trip-length difference is disclosed up front, because "17.7h more in destinations" reads very differently on a trip 1.8 days longer.
- A substitution is never described as an addition.
- An unscored baseline or itinerary produces **no comparison at all**, rather than a landslide manufactured from placeholder zeros.

### Editing (Phase 2)

`TripPatch` is a typed, ordered, discriminated union — never an unvalidated dict. An unknown operation is a 422; a declared-but-unimplemented one is named in `unsupported_operations` rather than silently dropped.

**Locks and exclusions are hard constraints, not preferences.** `lock_city` → `must_visit` (enforced at completion); `remove_city` / `exclude_city` → `avoid_destinations` (rejected on *every* state, partial and complete, so an excluded city cannot reappear at any beam depth). The consequence is that the brief's adversarial case — an unrelated, globally higher-scoring trip winning an edit — is **infeasible**, not merely out-ranked. Asserted at `similarity_weight=0.0`, so preservation demonstrably does not depend on a tuned weight.

`TripSimilarity` (9 weighted dimensions) decides only between candidates that already satisfy every hard constraint — route order, hotel, airport. It is absent from discovery ranking by test.

Re-optimization uses its **own** selection path, skipping Pareto and diversity: after a lock, every valid candidate shares the locked cities, so a filter built to collapse near-duplicates would discard exactly the alternatives the edit is choosing between.

The structured diff reports improvements and costs as separate lists, so a client cannot render one without deciding to omit the other.

### Baggage truth (Phase 3)

```
BaggageStatus:  INCLUDED | EXTRA | NOT_AVAILABLE | UNKNOWN
PriceCompleteness:  COMPLETE | PARTIAL_UNKNOWN
```

**Unknown is a type, not a sentinel float.** The model refuses to construct dishonest states: `UNKNOWN` with any price, `NOT_AVAILABLE` with a price, `INCLUDED` with a non-zero price, a policy slot holding the wrong `BaggageKind`. `BaggageQuote.total_for_display` returns `None` whenever anything is unquoted or unbuyable — a caller wanting the partial figure must ask for `known_total` explicitly and thereby decide how to caveat it.

| State | `known_total` | `total_for_display` | Distinguishable? |
|---|---|---|---|
| `INCLUDED` | 0.0 | **0.0** | yes — a known zero |
| `UNKNOWN` | 0.0 | `null` | yes |
| `EXTRA`, unpriced | 0.0 | `null` | yes |
| `NOT_AVAILABLE` | 0.0 | `null` | yes |

Verified in the raw HTTP body, not just Python objects: `"total_for_display": null`, `"price_per_person": null` — never `0`.

**The admissible-bound asymmetry.** An unquoted fee contributes **zero to a lower bound** (zero is a true statement about a fee nobody quoted; guessing higher would make the bound overestimate and silently prune affordable trips) and forces **`PARTIAL_UNKNOWN` in a displayed total** (zero there would be a false statement). Both are correct because they answer different questions.

**Original vs Detoura refuses a numeric `baggage_delta`** unless *both* sides are priceable, and names which side is missing. The brief's Example D — "Detoura saves €30 all-in" while Detoura's own baggage is unknown — is unconstructible.

---

## 3. Regression, determinism, performance

**Golden signatures** (measured against a pristine `c20fbf4`), reproduced exactly at `b9ce87e` **with and without** a baggage requirement:

| Scenario | states | rejected | completed | Pareto | score | route |
|---|---|---|---|---|---|---|
| QUICK 5d | 1196 | 692 | 158 | 13 | 0.719869 | CGN → Berlin → Munich → CGN |
| SMART 5d | 9730 | 6804 | 1076 | 68 | 0.734457 | CGN → Berlin → Munich → CGN |
| SMART 7d | 10066 | 6416 | 1124 | 110 | 0.736198 | CGN → Berlin → Prague → Vienna → CGN |

**Ranking is identical whether or not baggage is requested** — same routes, same `states_generated`, same `total_cost`. Naming a bag changes what Detoura claims, never what it finds.

**Determinism:** repeated identical searches, comparisons and re-optimizations are byte-identical, including `alternatives` ordering.

**Performance:**

| Mode | no baggage | cabin known | cabin unknown |
|---|---|---|---|
| QUICK | 0.18s | 0.26s | 0.24s |
| SMART | 1.05s | 1.11s | 1.09s |

Search metrics identical across all three arms; the delta is the post-search quoting pass. Re-optimization: QUICK edit **0.14s** (fresh 0.25s), SMART edit **0.73s** (fresh 1.51s) — edits run ~2× faster than a fresh search, because locks shrink the space.

**`beam_search.py` and `constraints/validator.py` carry zero diff across all three phases.**

---

## 4. Known limitations

1. **`lower_bound_per_traveller` is not wired into pruning.** It is called only from tests. Baggage is priced post-search by design, so the admissible-bound rule currently protects nothing. It is kept and tested because the moment baggage affects feasibility that rule is needed, and deriving it under deadline is how an inadmissible bound ships.
2. **Baggage does not affect feasibility or ranking.** A trip that is affordable on fare but not with baggage is still returned — correctly labelled `BAGGAGE_EXCEEDS_BUDGET`, but not filtered. Making baggage a hard budget constraint is a search change, deliberately deferred.
3. **No provider reports baggage.** Every real fare is `UNKNOWN`; the known-fee paths are exercised only by synthetic fixtures. Correct today, and the reason the honest branch is the default.
4. **Foreign-currency baggage is dropped, not converted.** Treated as unquoted. Conversion needs a rate source at the provider boundary.
5. **`Money` uses `float`.** Rounding is half-up to cents and precision holds to the cent under test, but a `Decimal` spine would be stronger. Pre-existing, not introduced by V7.
6. **Redis coverage is not currently executing** (22 skips) because Docker is down on this machine. V6.5 code, untouched by V7.
7. **Pareto filtering is O(n²)** — 18.3M comparisons over 10,566 itineraries, ~40% of DEEP wall time. Untouched by V7 and the hardest constraint on any catalog growth.
8. **Catalog-order truncation** in `_candidate_pool()` is inert at 16 cities and becomes harmful past ~50.

## 5. Technical debt

- `contracts.py` (805 lines) and `planner.py` (661) exceed the repo's 500-line guidance. Splitting them is a refactor deliberately not bundled into a release.
- `_selected_itinerary` reconstructs saved legs with `arrival = departure` and `duration_minutes = 0`; nothing reads them, and a comment says so.
- Two stale agent worktrees (`efe5f3b`, `789a357`) remain registered.
- `.codex/`, `AGENTS.md` and `CLAUDE.md` are untracked local tool state, never committed in any branch. Adding them to `.gitignore` would prevent a future accidental `git add -A`.

## 6. Process notes

Recorded because they affected the review, not to pad the document.

- **A second writer touched the shared worktree during Agent 5's review.** While Agent 5 was reviewing Phase 3, `tests/test_v2_explainability.py` was edited to accommodate the new cost component. Agent 5 correctly flagged this as an ownership violation. The edit was legitimate, its timing was not.
- **Two of Agent 5's own tests contradicted each other** after the fixes — one asserted baggage inside `CostBreakdown.total`, its own defect test demanded the opposite. The defect test was the correct one; the other was corrected with the reasoning recorded inline.
- **Benchmark instrumentation errors were caught and discarded, not published.** An early Phase 0 run had `tracemalloc` active in the timed path and inflated every figure ~4×. A secret scan passed `$FILES` unquoted, zsh did not word-split it, grep errored, and the script printed a **false "clean"** — every scan since uses explicit argv and fails closed.

## 7. Provider readiness and V8 prerequisites

The baggage model is provider-neutral by test: no vendor name appears as an import, identifier or attribute in the model or pricing layers (checked via AST, not substring, because the docstrings name Duffel precisely to say they are not coupled to it).

Mapping seams already exist: `TransportOption.baggage: BaggagePolicy | None` (None = provider said nothing), `Money` + `PriceBasis` + `PriceNormalizer` at the boundary, `PriceProvenance` for freshness, `seats_available` for inventory, `FailureLog` + `ProviderIssueDTO` for typed degradation, `Resilient(Caching(inner))` so an outage is never memoised as "no flights".

**Blocking prerequisite:** `DUFFEL_ACCESS_TOKEN` is **absent** from the environment. Per the safety rule, a missing or ambiguous token must fail closed, so no live call can be made and no live mapping can be verified. Buildable offline: the V8 audit, provider seam, normalization against recorded payloads, `BookingIntent` state machine, call budgets, cache-failure semantics. Not verifiable without a token: that Duffel's actual responses parse and normalize correctly.

## 8. Definition of done

| Capability | Status |
|---|---|
| COMPARE the original idea against Detoura, honestly | ✅ |
| EDIT a selected trip via typed operations | ✅ |
| LOCK cities as hard constraints | ✅ |
| REMOVE / EXCLUDE, never reappearing | ✅ |
| REPLACE, without inflating city count | ✅ |
| RE-OPTIMIZE the mutable remainder | ✅ |
| EXPLAIN improvements and trade-offs both ways | ✅ |
| REPRESENT baggage as known-free / known-extra / unknown | ✅ |
| …without changing search for unspecified baggage | ✅ |

**V7 is feature-complete for this roadmap.**
