# Detoura V7 — Phase 3 Design: Baggage-Aware Pricing, Flexible Budget, Worth Stretching For

**Baseline:** `4686371` (V7 Phase 2, pushed). Phase 1 golden anchors still reproduce exactly.
**Status:** read-only audit. No source file was modified to produce this document. `beam_search.py` remains untouched.

---

## 0. What exists today

**Baggage: nothing.** A search across `src/` for `baggage`, `luggage`, `cabin_bag`, `checked_bag`, `carry_on`, `personal_item` returns **zero matches**. This is greenfield, not a retrofit — which is good news, because there is no wrong assumption already baked in.

What that means in product terms is worth stating plainly: **every price Detoura has ever shown is a bare fare of unknown baggage allowance.** The cheap flights that make multi-city itineraries look attractive are exactly the ones most likely to be personal-item-only. The product is not currently lying, but it is silent in a place where silence reads as "included".

**The pricing spine that does exist and should be reused:**

| Component | State |
|---|---|
| `Money` | amount + currency + `tax_included`. Addition refuses to mix currencies or tax bases. |
| `PriceBasis` | `PER_PERSON` / `PER_ROOM_NIGHT` / `TOTAL` — already the right vocabulary for baggage |
| `PriceNormalizer` | the single conversion point; `per_person()` and `party_total()` already exist |
| `CostBreakdown` | `transport` + `accommodation` + `ground_transfer`, with `total` derived |
| `TransportOption` | id, origin, destination, departure, arrival, `price_per_person`, type, duration, operator, `seats_available`, `provenance`. **No fare metadata.** |
| `PriceProvenance` | already models "where did this number come from, and when" |
| `seats_available: int \| None` | **the precedent to copy.** Its docstring says it outright: "``None`` is *unknown*, not *unlimited*." |

`seats_available` is the model to follow. V4 already faced "the provider didn't say" and answered it correctly, in the type system, with a documented reason. Baggage is the same problem about a different field.

---

## 1. What providers actually expose

**Amadeus (`providers/amadeus.py`) reads exactly one price field:** `offer["price"]["grandTotal"]`, mapped to `Money(tax_included=True)`. Nothing else is read.

The live Amadeus Flight Offers schema *does* carry baggage, in two places this integration ignores:

- `travelerPricings[].fareDetailsBySegment[].includedCheckedBags` — `{quantity: N}` **or** `{weight: N, weightUnit: "KG"}`, and **sometimes absent entirely**
- `travelerPricings[].fareDetailsBySegment[].includedCabinBags` — same shape, less consistently populated
- `price.additionalServices[]` with `{type: "CHECKED_BAGS", amount: "..."}` — a *fare-level* figure, frequently missing

Three distinct facts, and the schema can express all three: **included**, **purchasable at a stated price**, and **not stated**. The mapping must preserve that three-way distinction rather than flattening it to a number.

**Synthetic providers (`data/synthetic_transport.py`):** `Connection` carries `price_per_person` and nothing else. Whatever baggage model is added, the synthetic network must be able to express *unknown* — otherwise every test runs against a world where baggage is always known, and the unknown path ships untested. That would be the most likely way this phase fails.

---

## 2. Known vs unknown

```
BaggageAllowance
  PERSONAL_ITEM_ONLY   provider stated: nothing but a under-seat bag
  CABIN_BAG            provider stated: cabin bag included
  CHECKED_BAG          provider stated: checked bag included
  UNKNOWN              provider did not say
```

`UNKNOWN` is a first-class value and **never** collapses into `PERSONAL_ITEM_ONLY`. Assuming the worst is as much an invention as assuming the best; it would make honest carriers look expensive and would be wrong about exactly the cheap fares this feature exists to explain.

The same three-way split applies to price:

```
BaggagePrice
  included            0.00, because the provider said it is included
  priced   Money      the provider quoted a fee
  unknown             the provider did not say
```

`included` and `unknown` must be different objects in the type system, not `0.0` and `None` in the same float field where a careless `or` collapses them. This repo has already shipped that exact bug once — V6.5's `if not raw:` conflated a missing session with an empty one.

---

## 3. Per traveler, per leg, per direction, or per booking?

**All four occur, so the model must carry the basis rather than assume one.** `PriceBasis` already exists for this.

- Low-cost carriers: **per passenger, per leg, per direction.** A cabin bag on a 4-leg trip for 2 travellers is 8 charges.
- Legacy carriers: often **per booking, per direction**, included in the fare.
- Rail/bus: usually included, frequently unstated.

The calculation is therefore per `(leg, traveller)` and summed — never a trip-level multiplier. A trip-level "+€40 for baggage" is the shape of the bug §21 is guarding against, and it is wrong by a factor of the leg count.

---

## 4. Mixed carriers

Normal, not exceptional: `CGN → Prague` on one carrier, `Prague → Vienna` on another, `Vienna → CGN` on a third. Baggage resolves **per leg**, and a trip's overall baggage status is the *weakest* of its legs:

- any leg `UNKNOWN` → the trip's baggage total is **UNKNOWN**, even if every other leg is priced
- otherwise the sum of the per-leg figures

This mirrors `PriceFreshness.combine()`, which already takes the worst of all a trip's prices rather than the average. Same principle, same reason: a total is only as trustworthy as its least trustworthy part, and averaging hides the gap.

**Consequence to state plainly:** with realistic provider data, many trips will report an unknown baggage total. That is the honest answer and the UI must be able to render it. A design that quietly needs baggage to be known in order to look good will get "fixed" later by inventing a number.

---

## 5. Preventing double-counting

The risk is real because two totals will exist side by side.

- `CostBreakdown` gains a **fourth** component, `baggage`, alongside transport/accommodation/ground_transfer. It is never folded into `transport`.
- `TransportOption.price_per_person` stays the **bare fare**. Baggage is never added into it, because that field is what the provider quoted and what `recheck` re-finds.
- A single function computes a trip's baggage total from its legs and the selection, and every caller uses it. The Phase 1 lesson applies directly: two extraction sites are two chances to disagree.
- `SearchState` accumulates baggage in its own field, the way it already separates transport/accommodation/transfer.

**Regression test to write first:** total with baggage minus total without equals the baggage component exactly, across a matrix of leg counts and party sizes.

---

## 6. Budget feasibility — and the admissible-bound trap

This is the sharpest correctness risk in Phase 3, and it is not obvious.

`ConstraintValidator._validate_partial` prunes on remaining budget using a **floor** on the cost still to come (cheapest return, cheapest remaining nights). The bound is admissible because it never overestimates: it can only prune states that genuinely cannot finish.

If a *known* mandatory baggage fee is added to that floor, the bound gets tighter and stays admissible. **If an unknown baggage fee is guessed into the floor, the bound becomes inadmissible and the search will prune trips that were actually affordable.** That is a silent loss of correct results — the failure mode V6 spent effort establishing rules against, restated in the V7 brief as "never treat an unknown direct-return bound as unreachable".

**The rule:** only baggage the provider actually stated may enter an admissible bound. `UNKNOWN` contributes **zero** to the pruning floor and is disclosed at the top, not silently priced in. That is the one place where treating unknown as zero is correct — because a *lower bound* of zero is a true statement about a cost nobody has quoted, whereas a *displayed total* of zero would be a false one. The asymmetry is the whole point and belongs in a comment where it is implemented.

---

## 7. Target budget vs hard ceiling

The split maps cleanly onto machinery that already exists:

| Concept | Where it lives today | Phase 3 |
|---|---|---|
| **hard ceiling** | `_validate_common`: `state.total_cost > request.budget` → reject | unchanged; `budget` *is* the ceiling |
| **target budget** | `travel_value.cost_score` + `config.budget_utilization_target` | becomes the soft target it already behaves like |

So `BudgetEnvelope(target, ceiling)` is mostly a renaming of an existing relationship, plus one new rule: **results above target but under the ceiling are returned in a separate band**, never blended into the main list. `ceiling` defaults to `target` so that a request that names only a budget behaves exactly as it does today — every existing golden signature must survive this phase.

---

## 8. Effect on Original vs Detoura (Phase 1)

Phase 1 shipped `baggage_delta: float | None = None` and puts `"baggage"` in `unknowns` on **every** comparison, with a test asserting it is `None` and not `0.0`. That seam is already cut and Phase 3 fills it.

The interesting case, and one §22 names explicitly: **baggage can reverse the verdict.** A €286 personal-item fare plus €40 cabin bag loses to a €310 fare with a bag included. Phase 1's comparison already reports both directions and can already return `ORIGINAL_BETTER`, so no new honesty machinery is needed — but a test that the verdict actually flips when baggage is included is mandatory, because that is the case the feature exists for.

Where either side's baggage is `UNKNOWN`, `baggage_delta` stays `None` and the comparison keeps saying so. A partial comparison is honest; a completed one built on a guess is not.

---

## 9. Effect on re-optimization (Phase 2)

- `ChangeBaggageRequirement` already exists in `TripPatch`, is already accepted, and is already reported in `unsupported_operations`. Phase 3 moves it into `SUPPORTED_OPS` — the client contract does not change.
- Requiring a cabin bag becomes a **hard** constraint on the derived request: legs that cannot carry one are infeasible, in the same way an excluded city is.
- `ChangeDiff` gains a baggage line, using the existing `build_metrics` path so an edit and a comparison cannot disagree about what a baggage delta means.
- `TripSimilarity` does **not** gain a baggage dimension. Baggage is a requirement, not a resemblance.

---

## 10. "Worth Stretching For" without manipulation

The manipulation risk is obvious and worth naming: a band labelled *worth stretching for* is an upsell unless it is computed and falsifiable.

**Rules:**

1. **Computed, never curated.** A trip qualifies only if it is above target, under ceiling, and beats the best within-budget option on a *material* axis — reusing Phase 1's `DIRECTIONS`/`EPSILONS` so "better" means the same thing it means everywhere else.
2. **Both directions, always.** "+€34 buys +1 city and +21 usable hours" must appear beside what it costs. Phase 1's `improvements`/`costs` split already enforces this shape.
3. **Silence is a valid answer.** If nothing above target is materially better, the band is **empty**. A "worth stretching for" section that is never empty is advertising.
4. **Meaningful steps only.** §25 forbids €1 increments. Budget sensitivity already computes *thresholds* — steps where a new city combination becomes reachable — and reports `unlocked`. Reuse `analyze_budget_sensitivity`; do not write a second sweep.
5. **Never auto-applied.** The ceiling is never silently exceeded. The stretch is offered; the traveler decides.

---

## 11. Proposed models

```
BaggageAllowance   PERSONAL_ITEM_ONLY | CABIN_BAG | CHECKED_BAG | UNKNOWN
BaggagePolicy      per-leg: what is included, what each upgrade costs,
                   each independently known-or-unknown
BaggageSelection   what the traveler asked for (the requirement)
BaggageQuote       resolved cost for one (leg, traveller), or UNKNOWN
BudgetEnvelope     target + ceiling, ceiling defaulting to target
```

`TransportOption` gains `baggage: BaggagePolicy | None = None` — `None` meaning the provider said nothing, consistent with `provenance` and `seats_available`.

---

## 12. Risks

1. **Inventing a fee under pressure to show a total.** The single largest risk, and the one the brief calls out twice. Mitigated by making `UNKNOWN` a type, not a sentinel float.
2. **Poisoning the admissible bound with a guessed fee** (§6). Silent loss of valid trips.
3. **Double-counting** between fare and baggage (§5).
4. **Per-leg arithmetic** collapsed into a trip-level multiplier (§3).
5. **Golden-signature drift.** Every existing anchor assumed no baggage; a default that is not exactly "no baggage priced" changes every published number.
6. **Synthetic data that always knows**, leaving the unknown path untested (§1).

---

## 13. Ownership and sequence

| File | Owner | Action |
|---|---|---|
| `models/baggage.py` | Agent 3 | new |
| `models/transport.py` | Agent 3 | additive field |
| `models/itinerary.py` (`CostBreakdown`) | Agent 3 | additive component |
| `providers/amadeus.py` | Agent 3 | read the baggage fields, preserve UNKNOWN |
| `data/synthetic_transport.py` | Agent 3 | must express unknown |
| `constraints/validator.py` | Agent 3 | **coordinated** — admissible bound |
| `services/budget_sensitivity.py` | Agent 3 | extend for the stretch band |
| `services/trip_comparison.py` | Agent 1 → loaned | fill `baggage_delta` |
| `services/reoptimizer.py` | Agent 2 → loaned | promote `ChangeBaggageRequirement` |
| `algorithms/beam_search.py` | **LOCKED** | no write |

Sequence: models → synthetic data (including unknown) → per-leg totals → `CostBreakdown` → budget envelope → comparison/reoptimization seams → stretch band. Tests for unknown-handling are written **before** the first known-baggage path, so the honest case cannot be retrofitted.

---

## 14. Verdict

**No architectural blocker.** Baggage is genuinely additive: the pricing spine (`Money`, `PriceBasis`, `PriceNormalizer`, `CostBreakdown`) is already the right shape, `seats_available` is a working precedent for known-vs-unknown, and Phase 1 already left `baggage_delta` and the `unknowns` list as seams.

Two items need care rather than code volume: **the admissible-bound asymmetry** in §6 — unknown contributes zero to a *lower bound* and must never contribute to a *displayed total* — and **synthetic data that can express ignorance**, without which the unknown path ships untested.

---

## 15. Phase 0 revalidation addendum (at HEAD `4686371`)

The audit was written before Phase 2 was committed. Re-checked against current HEAD; **every assumption still holds** and the design is unchanged.

| Audit claim | Verified at `4686371` |
|---|---|
| Baggage is greenfield in `src/` | 21 mentions, all placeholders and seams — `baggage_cost: float \| None = None`, `baggage_delta: float \| None = None`, `unknowns = ["baggage"]`, and a `ChangeBaggageRequirement` op declared but routed to `unsupported`. No model, no pricing, no provider field. |
| `CostBreakdown` has three components | `transport`, `accommodation`, `ground_transfer` + a `total` property. A fourth component is additive. |
| `ChangeBaggageRequirement` already exists | `models/patch.py:172`, `baggage: str`, listed in `PatchOp` but absent from `SUPPORTED_OPS`. |
| Admissible bound charges a floor | `beam_search._estimate` adds cheapest return price and `min_stay_cost`. Unchanged. |
| `seats_available=None` precedent | Intact in `models/transport.py`. |
| Golden anchors | QUICK-5d / SMART-5d / SMART-7d reproduce exactly. |

One correction to §13's ownership table: it names agents inconsistently with the Phase 3 brief (which assigns the domain model to Agent 1, cost to Agent 2, API to Agent 3, fixtures to Agent 4). **The brief's numbering governs**; the file list itself is unchanged and correct.
