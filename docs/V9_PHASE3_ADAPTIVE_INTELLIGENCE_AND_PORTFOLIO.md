# V9 Phase 3 — Adaptive Search Intelligence, Destination Attractiveness & Recommendation Diversity

Branch `claude/travel-planner-mvp-nvb267`. Answers the product question
Detoura must optimize for: not *"where can I travel most cheaply?"* but
*"which trips are unusually good opportunities AND genuinely worth
considering?"* — **cheap != good recommendation**.

## Architecture

```
TripRequest
      |
Catalog (data.destinations.acquisition_catalog(), 203 destinations)
      |
Price Memory / Market Prior     (Phase 1/2, unchanged)
      |
Candidate Intelligence           <- V9 Phase 3 §A: + global attractiveness
  (services/opportunity.py,         component, + geo/experience-aware
   services/candidate_funnel.py)    diversity in the pre-acquisition shortlist
      |
Bounded Acquisition Plan         (unchanged hard provider-call budget)
      |
Live Provider Acquisition        (unchanged)
      |
Normalized OfferSnapshot         (unchanged)
      |
Zero-network Optimizer           (services/planner.py, unchanged beam search)
      |
Recommendation Portfolio         <- V9 Phase 3 §C/§D: NEW — services/portfolio.py
  (optional, additive: services/live_search.py's `portfolio_db` kwarg)
```

Two genuinely new capabilities, wired at two different points in the
existing pipeline — deliberately not one giant rewrite:

1. **§A/§B — Candidate Intelligence** gets a new, independent
   "how attractive is this destination, in general" signal (never
   "how cheap is it") feeding the pre-acquisition scoring that decides which
   destinations receive a scarce provider call.
2. **§C/§D — Recommendation Portfolio** is a brand-new, optional, additive
   reranking pass over the *already-acquired, already-ranked* recommendations,
   with real geography and real experience-similarity, a diminishing-returns
   treatment of price, and full explainability.

Both make **zero provider network calls** and preserve every hard
provider-call budget, the exploration floor, and the LIVE > PRIOR > UNKNOWN
precedence untouched.

---

## §B — Destination Attractiveness

### The honest data-provenance decision

No authorized external tourism-quality dataset exists in this phase (§B2
explicitly allows and asks this to be reported honestly rather than
fabricating one). Rather than inventing a second, parallel personality
dataset, `DestinationAttractivenessProfile` is **derived deterministically**
from data the catalog *already* honestly carries:

* the 16 hand-tuned "core" cities' individually-authored 12-attribute
  profiles (`data/destinations.py`) → provenance `CURATED`, confidence `HIGH`;
* the ~187-city discovery catalog's tag-derived attribute profiles
  (`data/european_catalog.py::profile_from_tags`, V9 Phase 2 §17) →
  provenance `DERIVED`, confidence `MEDIUM`.

Nothing this phase claims `MEASURED` (a real external source) — a future
integration would report that honestly, not silently reuse `CURATED`/
`DERIVED`. A destination outside the catalog, or before `seed_attractiveness`
has ever run, is `UNKNOWN`: every dimension is `None`, never a fabricated
neutral 50 (`models/attractiveness.py::unknown_profile`).

### Eight dimensions (§B1 — a small, understandable model, not pseudo-precision)

| Dimension | Source attributes |
|---|---|
| `sightseeing_score` | mean(history, architecture, museums) |
| `culture_score` | culture |
| `food_score` | food |
| `nightlife_score` | nightlife |
| `nature_score` | mean(nature, beaches) |
| `short_trip_score` | blend of "suited to 1-2 days" (low `recommended_min_days`) **and** `richness` — a merely-quick, unremarkable city does not score high here (§B4) |
| `experience_density_score` | `Destination.richness` (already "how much is there to do") |
| `uniqueness_score` | this destination's Euclidean distance from the **catalog's own** attribute centroid, scaled — never a popularity/fame proxy (§B3) |

Dimensions are **not** mathematically orthogonal by design — a history-rich
city legitimately scores well on both `sightseeing` and `culture`, matching
how a traveler actually experiences a place. `aggregate_score` is the mean
of the known dimensions (`services/attractiveness_model.py`).

### Not popularity (§B3, proven by test)

`uniqueness_score` is computed purely from attribute-vector distance from
the catalog centroid. `test_uniqueness_is_not_a_popularity_proxy` constructs
an artificial "generic famous" profile (all attributes at 0.5) and an
artificial "unusual unknown" profile (extreme attributes) and proves the
unusual one scores higher — the mechanism cannot be fooled by fame, because
it never reads fame at all.

### Versioning and persistence (§Persistence/Versioning)

`persistence/attractiveness.py` — one row per `(destination_id,
model_version)` (schema v9, table `destination_attractiveness`). Writing a
new `model_version` **never** touches an old version's rows: both stay
independently readable forever (`test_new_model_version_never_reinterprets_old_rows`).
Batch reads (`batch_get_profiles`) are one query for an entire shortlist,
never N+1. `services/attractiveness_import.py::seed_attractiveness` is the
"import/update mechanism" the spec asks for — deterministic, offline,
idempotent (an unchanged catalog produces zero writes on a re-run, reported
as `unchanged` rather than silently touching `updated_at`).

### Ops (`api/ops_attractiveness.py`, `require_ops`-gated, no consumer route)

* `GET /api/v1/ops/attractiveness/overview` — catalog coverage, missing
  destination ids, low-confidence ids, provenance breakdown.
* `GET /api/v1/ops/attractiveness/profiles` / `/{destination_id}` — inspect
  profiles.
* `POST /api/v1/ops/attractiveness/reseed` — recompute from the current
  catalog at the current model version.

### Global vs. user fit (§B5)

`DestinationAttractivenessProfile.aggregate_score` is the same number for
every traveler. **User fit** stays exactly what it already was in Phase 1/2 —
`services/acquisition.py::preference_affinity`, computed fresh per search
from `TripRequest.preferences`/`preferred_experiences` against
`Destination.experience_vector()` — and the two are threaded through
`opportunity.py` and `services/portfolio.py` as **separate, always-preserved
components** (`attractiveness` vs. `preference`/`user_fit` in every
components dict), never merged into one stored number.

---

## §A — Adaptive Search Intelligence

`services/opportunity.py::score_opportunities` gains one new weighted
component, `attractiveness` (`SearchIntelConfig.weight_attractiveness`,
default 0.14) — additive alongside the existing price/confidence/freshness/
preference/feasibility/supply/contribution components, `UNKNOWN` scored the
same neutral 0.5 every other unset component gets (never a penalty, never
optimistic). `services/candidate_funnel.py::run_funnel` batch-fetches
attractiveness for the whole feasible shortlist (one query) and feeds it in.

LIVE > PRIOR > UNKNOWN precedence is completely untouched — attractiveness
is one more component in the weighted average, never a way to bypass
freshness/confidence (`test_attractiveness_does_not_override_live_price_precedence`,
`test_benchmark_misleading_prior_is_overridden_by_disconfirming_live_data`).

### §C6 generalized into the pre-acquisition funnel too

The Phase 2 subregion soft cap (`_diversity_adjust`) is extended, not
replaced: **within** the same named subregion, a real geographic +
experience-similarity redundancy signal (the identical reusable functions
the final portfolio uses — `services/geo.py`, `services/experience_similarity.py`)
further discounts a candidate that is both close to *and* experientially
similar to a stronger candidate already accepted from that group. This
catches markets that are neighbours but sit in different named subregions —
without ever comparing the *whole* catalog pairwise (bounded to
within-subregion comparisons, a handful of candidates each).

### Provider budget, exploration floor (§A2/§A5)

`run_funnel`'s `slots` parameter is the only ceiling; nothing about
attractiveness, catalog size, or diversity changes it
(`test_provider_budget_not_increased_by_attractiveness_or_catalog_size`).
`acquisition_scoring.py::allocate`'s EXPLORE floor is untouched
(`test_explore_floor_preserved_with_attractiveness_enabled`). A destination
with **no** seeded attractiveness profile at all still competes normally —
UNKNOWN, not unreachable (`test_unknown_attractiveness_destination_remains_reachable`).

---

## §C — Recommendation Diversity / Portfolio Selection

`services/portfolio.py` — the "Recommendation Portfolio" step of the
pipeline, entirely new, zero network calls, operating only on data already
in hand (acquired price, attractiveness, user fit, trip quality).

### Two different concepts, kept separate (§C1)

* **Geographic proximity** — `services/geo.py::geo_redundancy_signal`, a
  smooth `exp(-distance/scale)` decay against every already-selected
  destination, summed, saturated, never a hard "under 100km forbidden" rule.
* **Experience similarity** — `services/experience_similarity.py`, cosine
  similarity over `Destination.experience_vector()` (the 12 interpretable
  V1/V3 attributes: historic/coastal/nightlife/food/culture/architecture/
  nature/beach/... — exactly the dimensions §C3 asks for, not an opaque
  embedding).

Amsterdam-near-Cologne-but-distinctive and eight-near-identical-nearby-cities
are explicitly different cases under this model — distance alone never
decides anything.

### §D1 — the cheapness diminishing-returns guard

`cheapness_value(price, floor, ceiling, gamma)`: price is normalized to
`t ∈ [0,1]` between the field's cheapest and priciest observed price
(linear), then raised to `gamma` (default 1.6, config-tunable,
`SearchIntelConfig.cheapness_curve_gamma`, never < 1.0). Because `t` sits
closer to 1 for very cheap prices and `t**gamma` compresses values near 1
more than values in the middle for `gamma > 1`, **a EUR30-vs-EUR15 gap is
worth more value than a EUR10-vs-EUR5 gap of the identical 2x ratio**
(`test_cheapness_gap_is_larger_at_higher_absolute_price_than_lower_for_same_ratio`)
— exactly the product principle the spec states, chosen and verified by
direct numeric benchmarking of several `gamma` values rather than assumed.

### Six components, never collapsed unexplained (§D)

`base_value()` combines, each already in `[0,1]`, neutral 0.5 when unknown:
price (via the curve above), `attractiveness`, `user_fit`, `market_opportunity`,
`trip_quality`, `novelty` — weighted sum
(`SearchIntelConfig.value_weight_*`, price deliberately not dominant at
0.26 of the total, same order as attractiveness at 0.22).

### Diversified greedy selection (§C4/§C5/§C6/§C7)

`select_portfolio` picks, one slot at a time, the remaining candidate with
the highest `base_value * (1 - geo_penalty) * (1 - experience_penalty)`,
where both penalties are computed against the set already chosen and are
individually capped (`portfolio_geo_penalty_scale`/
`portfolio_experience_penalty_scale`, default 0.55 each, benchmarked — see
below) so **no penalty can ever reach 1.0**: an exceptional candidate can
never be discounted to worthless (§C5,
`test_nearby_exceptional_destination_still_makes_the_cut`). Country
diversity (`services/geo.py::country_diversity_ratio`) is reported as a
**diagnostic only**, never a quota (§C7,
`test_country_diversity_is_a_diagnostic_not_a_quota`) — three excellent
Spanish destinations may legitimately all appear.

**A documented tradeoff, found and fixed during benchmarking**: an early
version of the Cologne fixture gave every candidate the same flat
`market_opportunity`, which let the diversification step occasionally favor
a genuinely mediocre-but-geographically-isolated destination purely for
having zero redundancy penalty, edging out a heavily-discounted-but-strong
one — precisely the failure mode §C5/adversarial-test-6 warn against. The
root cause was the fixture, not the algorithm: `market_opportunity` is a
*separate* signal ("is this price a good deal for this market") from price
and attractiveness, and once the fixture gave a deliberately expensive,
deliberately poor-value destination a correspondingly low
`market_opportunity` (reflecting what a real Phase 1/2 acquisition-stage
score would report for such a market), the failure mode did not recur. This
is documented as a known limitation of the design, not silently patched
away: **the portfolio pass is only as good as the `market_opportunity`
input it is given** — a caller that feeds a flat/placeholder opportunity
score for every candidate weakens this specific safeguard. Real callers
(the acquisition pipeline) always have a genuine, differentiated Phase 1/2
opportunity score.

### Itinerary adapter — optional, additive live wiring (§D2)

`services/portfolio.py::candidates_from_itineraries` /
`apply_portfolio_to_itineraries` bridge real `Itinerary` recommendations
(from `services/planner.py::TravelPlanner.plan`) into the portfolio pass.
Wired into `services/live_search.py::live_search` as a **new, optional**
`portfolio_db` keyword argument (default `None`): omitting it reproduces the
exact pre-Phase-3 ranking byte-for-byte, and every existing test/caller of
`live_search` is completely unaffected (verified: the full pre-existing
`test_v9_search_recorder.py` suite passes unchanged). It is also a
conservative no-op whenever a recommendation is not a clean
single-destination trip, or names a destination outside the given catalog —
a multi-city itinerary's own ranking is never second-guessed. When it does
apply, the recorder's `attribute()`/`finalize()` calls run *after*
reranking, so Top-K/winner contribution and the persisted
`SearchIntelligenceTrace.portfolio`/`portfolio_metrics` (§D2) both reflect
the actual final order shown to the traveler.

**Note on production wiring**: `services/live_search.py` is, as of Phase
2.5, a fully-built and tested V9 intelligence capability that is not itself
called from the currently-deployed `/api/v1/search` demo endpoint (that
endpoint uses `services/planner.py` directly against a synthetic supply, a
pre-existing Phase 1 architectural fact, not something this phase changed).
Phase 3's portfolio reranking is real, tested, and ready wherever
`live_search` is used — exactly the same status Phase 1's recorder already
had before this phase.

---

## The Cologne Benchmark (`scripts/bench_phase3_cologne.py`)

A deterministic fixture built entirely from the **real** 203-destination
catalog and real derived attractiveness/geography — never a hand-picked
city-name whitelist. The adversarial pattern comes from a **pricing model**,
not from choosing favourable cities:

    expected_price(d) = 5 + distance_km(origin, d) / 45

closer is systematically cheaper (real short-haul bus/train economics), plus
a small seeded per-destination jitter that also drives `market_opportunity`
(a price below its distance-expected price is a genuine deal; above is
poor) — so "cheap because close" and "an actual bargain" are deliberately
different signals. A random (seeded) 60-candidate sample is drawn from the
202 non-origin destinations for each run.

Three strategies, identical candidate pool, identical simulated
provider-call budget (20):

1. price-heavy / legacy-like baseline (sort by price alone)
2. Phase 3 value only (attractiveness + opportunity + cheapness curve, no
   diversification)
3. Phase 3 full portfolio (adds geo/experience diversification)

See the final Phase 3 report for the exact measured numbers. The property
this benchmark and `test_v9_phase3_benchmarks.py::test_benchmark_origin_generalization_no_worse_than_baseline`
assert — never a fixed expected city list — is that the full portfolio's
mean pairwise geographic distance and mean pairwise experience similarity
are never worse than the naive price-sorted baseline's, for Cologne and for
five other representative European origins (Paris, Milan, Vienna,
Barcelona, Warsaw).

---

## Performance

* No provider network calls anywhere in `opportunity.py`, `candidate_funnel.py`,
  or `portfolio.py` (§A1, asserted by every test in this phase using only
  in-memory fixtures/SQLite).
* Attractiveness lookups are batched (one query per search, `attractiveness_by_id`
  built once) — no N+1 (`test_funnel_uses_batched_attractiveness_lookup_no_n_plus_one`).
  `uniqueness_score`'s catalog-centroid computation is O(N) over the catalog,
  computed once per import run, not per destination.
  Within-subregion geo/experience redundancy in the pre-acquisition funnel
  is bounded to comparisons *within* a subregion (a handful of candidates
  each), never O(catalog²) over the full ~203-destination catalog.
* The final portfolio's greedy selection is O(size × candidates) per round,
  bounded by the acquisition shortlist size (tens of candidates), not the
  full catalog.
* `test_full_catalog_scale_completes_without_error` /
  `test_benchmark_unknown_discovery_excellent_destination_still_surfaces` run
  the full pre-acquisition funnel over the entire live 203-destination
  catalog and complete well within a test-suite-acceptable time budget.

## Known limitations

* No authorized external tourism-quality dataset was integrated — every
  attractiveness score is `CURATED` (16 cities) or `DERIVED` (the rest),
  honestly labelled, never `MEASURED`.
* `market_opportunity` in the final portfolio value is only as
  discriminating as what the caller supplies — see the documented
  benchmarking finding above.
* `services/live_search.py`'s portfolio reranking only applies to clean
  single-destination-per-recommendation searches; a multi-city itinerary's
  ranking is left untouched rather than guessed at.
* The pre-acquisition geo/experience redundancy check operates within a
  named subregion only, by design (bounded cost) — two candidates that are
  geographically close but happen to sit in different named subregions with
  few other members are not compared to each other at this stage (they
  still are at the final portfolio stage, which compares against the whole
  selected set).
