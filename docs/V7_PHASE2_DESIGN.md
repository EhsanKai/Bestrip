# Detoura V7 — Phase 2 Design: Interactive Re-optimization

**Baseline:** `7e34507` (V7 Phase 1, pushed). Golden anchors from `c20fbf4` still reproduce exactly.
**Status:** read-only audit. No source file was modified to produce this document. `beam_search.py` is untouched and remains locked.

---

## 0. The headline finding

**The core lock/exclude semantics need no change to `beam_search.py`.**

The optimizer already enforces both, as hard constraints, today:

| V7 concept | Existing mechanism | Where enforced | Strength |
|---|---|---|---|
| LOCK city | `TripRequest.must_visit` | `_validate_completed` → `MISSING_MANDATORY_DESTINATION` | hard, at completion |
| EXCLUDE city | `TripRequest.avoid_destinations` | `_validate_common` → `AVOIDED_DESTINATION` | hard, on **every** state |
| lock + exclude same city | `TripRequest._check_window` | model validator | rejected as invalid input |

Two properties fall straight out and are worth stating precisely, because they are exactly what §23's adversarial list asks for:

- **An excluded city can never reappear.** `_validate_common` runs on every state, partial and complete, so a forbidden city is rejected the moment it enters `visited_cities` — not filtered at the end, where a deep branch could slip past.
- **A locked city cannot be silently dropped.** Completion fails without it, and `_beam_rank_key` already sorts by `len(mandatory & visited) / len(mandatory)` *before* score, so the beam actively steers toward mandatory cities instead of filling with attractive doomed routes.

The §22 adversarial case is therefore not a ranking contest at all. Given `must_visit = {Hamburg, Berlin}`, candidate B (`Köln → Brussels → Paris → Lyon → Köln`) is **infeasible**, not merely lower-ranked. It cannot be returned however good its generic Travel Value is. The requirement "an unrelated high-Travel-Value trip must not beat a sufficiently similar valid edit" is satisfied structurally, by constraint, not by tuning a weight.

That is the whole reason Phase 2 can be built without opening the optimizer.

---

## 1. TripPatch

Typed operations, never an unvalidated dict. A frozen discriminated union, validated at the contract boundary the way `TripSearchRequest` already is.

```
TripPatch
  trip_id: str
  operations: list[PatchOperation]     # ordered, applied in sequence
  travelers, budget, ... carried from the original request

PatchOperation = LockCity | UnlockCity | RemoveCity | ReplaceCity
               | AddCity | ExcludeCity | ChangeStayDuration
               | LockStayDuration | ChangeBudget | ChangeTripDuration
               | ChangeTransportPreference | ChangeAccommodationPreference
               | ChangeBaggageRequirement          # accepted, inert until Phase 3
               | ChangeDepartureLocation | ChangeReturnLocation
```

Each carries a literal `op` field as the discriminator, so Pydantic validates the shape and an unknown op is a 422 rather than a silently ignored key. `ChangeBaggageRequirement` is deliberately accepted and recorded but has no pricing effect until Phase 3 — the alternative, rejecting it, would force a client change when baggage lands.

**Phase 2 scope:** implement `lock_city`, `unlock_city`, `remove_city`, `replace_city`, `exclude_city`, `add_city`, `change_budget`, `change_trip_duration`. The rest are declared in the union and rejected with an explicit "not yet supported" issue rather than being quietly dropped. Extensibility is the point of the union; implementing all fifteen at once is not.

---

## 2. Lock / unlock semantics

`lock_city(X)` means: **X appears in the result.** It does not mean the same flight, the same hotel or the same day — those are separate, softer promises, and conflating them would make almost every edit infeasible.

| Aspect | Phase 2 treatment |
|---|---|
| City present | **HARD** — `must_visit` |
| City order | **SOFT** — similarity term |
| Stay duration | **SOFT** unless `lock_stay_duration` is given |
| Specific legs | **SOFT** — similarity term |
| Specific hotel | **SOFT** — similarity term |

`unlock_city(X)` removes X from `must_visit`; it does **not** exclude it. Unlocking means "you may change this", not "remove this" — a distinction worth a test, because collapsing the two would delete cities the user merely stopped protecting.

---

## 3. Remove / exclude semantics

These are **different operations** and the difference matters.

- `remove_city(X)` — take X out of *this* itinerary. X may legitimately be re-proposed later in another search.
- `exclude_city(X)` — X must not appear, now or in any re-optimization of this trip. Maps to `avoid_destinations`, which is hard on every state.

For Phase 2, `remove_city` is implemented **as** an exclusion for the duration of the edit. Removing Budapest and then having the optimizer helpfully re-add Budapest because it scores well would read as the system ignoring the instruction. The distinction is preserved in the patch record so a later phase can offer "actually, Budapest is worth reconsidering" without re-deriving intent.

**Conflict rule:** locking and excluding the same city is rejected at validation. `TripRequest` already refuses the overlap; the patch layer must produce a clear message rather than letting a domain `ValueError` surface as a 500.

---

## 4. Replace semantics

`replace_city(X, with=Y)` = `exclude_city(X)` + `lock_city(Y)`.

`replace_city(X, with=None)` = `exclude_city(X)` and let the optimizer choose, with the city count held at its previous value so "replace" does not quietly become "remove". This is the Budapest → Prague case from §24 and is the more common form.

---

## 5. Hard vs soft constraints

The separation is architectural, not a scoring weight.

**HARD — expressed as a derived `TripRequest`, enforced by `ConstraintValidator`:**
`must_visit` (locks) · `avoid_destinations` (exclusions/removals) · `budget` · `duration_days` · `date_from`/`date_to` · `transport_preferences`

A hard constraint cannot be traded away, because a state violating it is never valid. There is no weight to tune and therefore no weight to get wrong.

**SOFT — expressed as `TripSimilarity`, applied in selection:**
city preservation · city order · stay durations · same legs · same accommodation · same departure/return airport · price proximity · duration proximity

**The invariant:** no soft term may ever admit a state the validator rejected. Similarity re-ranks feasible candidates; it never rescues an infeasible one. Enforced by construction — similarity is applied to `completed` states, which have already passed `_validate_completed`.

---

## 6. TripSimilarity

A weighted score in `[0,1]` against the *original selected itinerary*, computed over dimensions that are all already present on `Itinerary`:

```
city_preservation        |kept ∩ original| / |original|      (Jaccard-like, order-free)
city_order_preservation  longest common subsequence of the kept cities
stay_duration_similarity 1 - normalized |Δnights| per preserved city
transport_similarity     share of original legs reappearing (id match)
accommodation_similarity share of preserved stays keeping the same property
airport_similarity       departure and return airports unchanged
date_similarity          1 - normalized |Δdeparture date|
price_proximity          1 - normalized |Δtotal|
duration_proximity       1 - normalized |Δduration_days|
```

**It must not touch discovery.** `TravelValueScorer` is unchanged; `profiles.py` gains no weight. Similarity is a *selection* concern belonging only to the re-optimization path. Adding it to normal ranking would make every fresh search quietly prefer trips resembling some previous trip, which is not what discovery is for — and it would invalidate every golden signature in the repo.

---

## 7. Minimum-change re-optimization

```
ReoptimizationScore = TravelValue × (1 - λ) + TripSimilarity × λ
```

with λ configurable and defaulting around 0.35, **benchmarked rather than guessed** (§29's rule about not hard-coding a ratio applies here as much as to candidate retrieval).

Note what λ is *not* doing: it is not protecting locked cities — hard constraints already did that. λ decides between candidates that all satisfy the edit, e.g. Hamburg→Berlin→Prague versus Berlin→Hamburg→Prague, or the same route with a different hotel. Its influence is therefore bounded and safe to tune.

### The real risk: post-processing discards the minimum-change candidate

This is the one place the current pipeline actively works against Phase 2.

`TravelPlanner._post_process` applies, in order: Pareto filter → exact-route dedupe → diversity (Jaccard ≥ 0.5 collapses "the same trip") → top 5. For re-optimization, **all three can drop precisely the candidate we want**:

- **Pareto** can dominate away a trip that is 3% worse on generic objectives but preserves the user's route order.
- **Diversity** is actively hostile here: after a lock, every valid candidate shares the locked cities, so their Jaccard similarity is *high by construction* — the filter designed to remove near-duplicates will discard the alternatives that differ only in the mutable remainder, which are exactly the candidates the edit is choosing between.
- **top 5** is a discovery-shaped answer; an edit wants the single best-preserving trip plus a couple of genuine alternatives.

**Therefore re-optimization needs its own selection path** over the completed states, not `_post_process`. Pareto stays (it never removes a candidate that is better on some axis); diversity is disabled or has its threshold raised sharply for edits.

---

## 8. Structured change diff

Phase 1 already built the machinery. `compare_trips()` compares two priced, scored trips and reports signed deltas with a verdict that can go either way — which is exactly what §24 asks for, with the original itinerary in the baseline's place instead of the traveler's idea.

`ReoptimizationDiff` therefore **reuses `TripComparison`** rather than growing a parallel delta model, plus edit-specific structure:

```
requested:  the TripPatch, echoed back
changed:    cities added / removed / reordered, legs changed,
            accommodation changed, airports changed
impact:     TripComparison(new_trip, previous_trip_as_baseline)
preserved:  what the locks actually protected
```

The one piece of plumbing needed: `compare_trips` takes a `BaselineResult`, and here the "before" side is an `Itinerary`. A small adapter (`Itinerary → BaselineResult`-shaped view, or widening the comparison to a shared protocol) is required. It must carry `scored=True` and real scores, or Phase 1's guard will correctly refuse to compare — and `ground_transfer_minutes` must be carried across, or Defect 1 returns in a new costume.

**No invented prose or metrics.** Every number in the diff comes from the same deterministic path as Phase 1.

---

## 9. Optimizer integration points

**`beam_search.py`: NO CHANGE REQUIRED in Phase 2.** Locks and exclusions are `TripRequest` fields the optimizer already honours.

The one change needed is in `planner.py`, and it is a mechanical split:

```
plan(request)  →  explore(request) -> list[SearchState]     # search + wiring
                  _post_process(...)                        # unchanged
```

`reoptimize()` calls `explore()` and applies its **own** selection. `plan()` keeps calling `explore()` then `_post_process()` and must produce byte-identical output — proven against the `c20fbf4`/`7e34507` golden signatures, not asserted.

Optional, explicitly deferred: `_candidate_destinations()` does not filter by `avoid`, so excluded cities are expanded and then rejected by the validator. Correct but wasteful. Fixing it is a `beam_search.py` change and buys only speed, so it is **out of scope for Phase 2** and belongs with Phase 5's candidate-retrieval work, under Agent 4's ownership.

---

## 10. File ownership

| File | Owner | Action |
|---|---|---|
| `src/detoura/models/patch.py` | Agent 2 | **new** |
| `src/detoura/services/similarity.py` | Agent 2 | **new** |
| `src/detoura/services/reoptimizer.py` | Agent 2 | **new** |
| `src/detoura/services/planner.py` | Agent 2 | modify — `explore()` split only |
| `src/detoura/api/contracts.py` | Agent 2 | modify — additive DTOs |
| `src/detoura/api/v1.py` | Agent 2 | modify — new endpoint |
| `src/detoura/services/trip_comparison.py` | Agent 1 → **loaned to Agent 2** | modify — accept an itinerary as the "before" side |
| `tests/test_v7_reoptimization*.py` | Agent 2 | **new** |
| `tests/test_v7_reopt_adversarial.py` | **Agent 5 only** | **new** |
| `src/detoura/algorithms/beam_search.py` | **LOCKED — nobody** | no write in Phase 2 |

If the design proves wrong and `beam_search.py` must change, it is assigned to **Agent 2 alone**, locked before any write, and no other agent may touch it concurrently.

---

## 11. Regression safety nets — required BEFORE any optimizer change

Written and green first, per §26:

1. **Golden signatures** — `c20fbf4`/`7e34507` anchors for QUICK-5d, SMART-5d, SMART-7d: states generated, states rejected, completed, Pareto kept, top score, top route. Already exist in `tests/test_v7_comparison_adversarial.py` and must stay green.
2. **`plan()` invariance across the `explore()` split** — the same request before and after must produce an identical full ranked signature.
3. **Admissible pruning unchanged** — `_estimate()` and `hypothetical_completion()` untouched. **Unknown direct-return cost must remain unknown/optimistic, never unreachable.** A locked city with no known return bound must stay reachable.
4. **Greedy-trap regression** — the existing `trap`, `stay_trap` and `transfer_trap` fixtures keep passing.
5. **Phase 1 comparison tests** — all 75 stay green.
6. **Determinism** — repeated identical re-optimization returns byte-identical results.

### Phase 2 adversarial matrix (§23), Agent 5's file

| Case | Expected |
|---|---|
| lock one city | present in every result |
| lock multiple cities | all present |
| remove middle city | absent; neighbours re-linked |
| remove final city | absent; return leg rebuilt |
| excluded city never reappears | absent at every beam depth |
| preserve city order | LCS-preserving candidate wins among feasible ones |
| impossible locked combination | typed "cannot satisfy" + relaxation offer, **never** a silent drop |
| locked city becomes unavailable | explicit failure, never quiet substitution |
| deterministic repeated reoptimization | identical output |
| unrelated high-value trip must not win | infeasible by construction; asserted, not hoped |
| provider failure during edit | degraded result with typed issues, locks still honoured |
| lock + exclude same city | 422 with a clear message |

---

## 12. Performance risks

Targets from §44: simple local edit **<2s**, SMART edit **<5s**, broad edit **<10s**.

| Risk | Assessment |
|---|---|
| Edit is a full search | Real. Mitigated by locks *shrinking* the space: `must_visit` prunes hard at completion and steers the beam. An edit should be **faster** than the original search. |
| Similarity scoring cost | Negligible — 9 terms over ≤ a few thousand completed states. |
| Pareto O(n²) (audit G7) | Inherited, unchanged by Phase 2. Bites at Phase 5, not here. |
| Beam prunes the high-similarity candidate before completion | **The genuine risk.** The beam ranks by generic score; a route-order-preserving candidate could be pruned early. Bounded because all survivors carry the locked cities. **Must be measured, not assumed** — and if it bites, the fix is a larger beam for edits (config, not code), *not* putting similarity into `_beam_rank_key`. |
| Warm caches | Free win: `v1.py` holds a module-level `_planner`, so provider caches persist across requests. |

---

## 13. Provider / cache reuse

The singleton planner means an edit re-uses the original search's warm `CachingTransportProvider`, `CachingAccommodationProvider` and `CachingGroundTransferProvider`. Rounds pay for search, not I/O.

**Freshness rule (§44):** cached *offers* may be reused within a request; cached **availability must not** be treated as fresh for a trip the user is about to act on. Re-optimization returns proposals, and the existing `recheck` endpoint remains the only thing that claims a price is current. Phase 2 must not blur that line — `PriceProvenance` already flows through and must keep flowing.

---

## 14. API contract impact

**Additive. No breaking change.**

```
POST /api/v1/trips/reoptimize
  → TripReoptimizeRequest { trip_id, original_trip, patch, travelers, budget, ... }
  ← TripReoptimizeResponse { trip, diff, preserved, issues, no_results }
```

Stateless, exactly like `/trips/recheck`: the client sends the trip back because Detoura stores nothing server-side. This keeps Phase 2 free of the account system Phase 7 has not yet decided on — and reuses the `RecheckLeg`/`RecheckStay` precedent for "send back what identifies the trip, not the whole rendered object".

An unsatisfiable edit returns `no_results` with typed `RelaxationSuggestion`s — the machinery already exists in `assembler.relaxations()`. Per §43, the relaxation is **offered, never silently applied**.

---

## 15. Saved-trip / versioning implications

Phase 2 produces the *inputs* to versioning without building it (§38–39 are Checkpoint G):

- Every re-optimization is a pure function of `(original_trip, patch)`, so a version chain is reconstructible by replaying patches. No server-side history is required in Phase 2.
- The response returns the new trip **alongside** the previous one; the client keeps both, so KEEP ORIGINAL / ACCEPT CHANGE is already expressible.
- `TripPatch` is the serializable edit record a future `SavedTripWorkspace` will store. Designing it as a typed, ordered operation list now is what makes UNDO and RESTORE possible later without redesign.
- **Deliberately not built now:** persistence, version storage, account binding.

---

## 16. Verdict

**No material architectural blocker.** The two hard semantics Phase 2 depends on already exist and are already enforced at the right strength. `beam_search.py` stays locked.

The two things that genuinely need care, and where the work actually is:

1. **`_post_process` is wrong for edits** — its diversity filter will discard the candidates an edit is choosing between, because after a lock they are near-duplicates *by construction*. Re-optimization needs its own selection path.
2. **The `explore()` split must be provably behaviour-preserving** — golden signatures are the proof, and they must be green before and after.

Proceeding into Phase 2 implementation: regression nets first, then `TripPatch` and the derived-request layer, then similarity and selection, then the diff and the endpoint.

---

## 17. Implementation addendum — where the design was wrong

Recorded because a design document that quietly matches the code it produced is not evidence of anything.

**§8 said the diff would reuse `TripComparison`. It reuses the *metric path* instead.**
`compare_trips` is built around a traveler's stated destination: it reports "your original Berlin trip" and flags a dropped destination. Neither survives contact with an edit, where the "before" side is a multi-city itinerary and a removed city was removed *on purpose* — flagging it as dropped would turn an obeyed instruction into a reported loss. So `values_of_itinerary`, `values_of_baseline` and `build_metrics` were extracted from `trip_comparison.py` and both callers use them. One metric definition, two presentations, which is what the honesty argument actually required — a shared *definition* of "3.8 hours less transit", not a shared paragraph.

**Similarity matched legs on `TransportOption.id`. It cannot.**
Provider ids are internal and never appear in `LegDTO`, so a trip sent back to be edited carried no id to match on and transport similarity would have silently scored 0.0 for every edit. Matching is now on `(origin, destination, departure)` — the same natural key `recheck` already uses to re-find a saved leg, and more robust anyway, since an id is a provider's private business.

**Two fields called travel-something meant different things — again.**
Adding an exact `travel_minutes` to `TripRecommendation` recreated Phase 1's Defect 1 in the wire format: `travel_hours` is door-to-door, and `travel_minutes` was intercity-only. A round trip through the API reported a trip as 39 minutes different from itself. The fields are now `intercity_minutes` and `total_transit_minutes`, named for what they measure, with `intercity + transfer == total` asserted in a test. This is the second time this exact confusion has cost a defect, which is why the names are now explicit rather than conventional.

**Ordering: "later wins" was too clever.**
The design implied an ordered patch resolves contradictions by taking the later operation. It does not. A patch that both keeps and removes one city is refused in either order — ordering disambiguates a *sequence of intentions*, not a *contradiction*, and picking the later of two opposite instructions is a coin toss dressed as a rule. Later-wins still applies to value-setting operations, where two budgets genuinely are a sequence.

**What the design got right, confirmed by measurement:**
`beam_search.py` was not modified. Locks and exclusions are hard constraints and every candidate in the completed set honours them — asserted at `similarity_weight=0.0`, so preservation demonstrably does not depend on the tuned weight. `_post_process` was indeed wrong for edits and re-optimization uses its own selection, skipping both Pareto and diversity for the reasons given in §7.
