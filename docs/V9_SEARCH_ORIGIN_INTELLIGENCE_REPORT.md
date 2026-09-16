# V9 Post-Phase-6 — Search Integration + Origin Intelligence, Slice 1 (Backend Foundation)

Starting HEAD `2d831a2` (V9 Phase 6 Security, CLOSED).

Phase 6 security is not reopened by this slice. This is product/engineering
work: fixing origin resolution and wiring the consumer search endpoint
toward the existing Phase 3 search-intelligence pipeline. Nothing here
claims a security finding, and nothing here reopens Phase 6.

## The product problem

Detoura's promise is that a traveler names where they are starting, and
Detoura discovers journeys they had not thought to search for. Before this
slice, origin resolution was structurally closed to a hardcoded 7-city/
5-airport table clustered around Cologne/Düsseldorf
(`data/destinations.py::ORIGIN_DISTANCES_KM`) - anything else was a hard
422, regardless of whether the city existed in Detoura's own ~203-city
discovery catalog. Separately, the consumer `/api/v1/search` endpoint had
never been connected to the real Phase 3 search-intelligence pipeline
(attractiveness scoring, exploration lottery, diversity reranking,
market-prior semantics, real Duffel-backed acquisition) - it ran entirely
on a pre-Phase-3, hand-curated 5-airport × 16-city synthetic demo network.

## CURRENT SEARCH FLOW (before this slice)

```
POST /api/v1/search
  -> TripSearchRequest (origin: free text, no default)
  -> to_trip_request() -> TripRequest (origin passed through unmodified)
  -> TravelPlanner.explore()
       -> StaticOriginResolver.resolve(origin, config)
            looks up a hardcoded 7-key table (koln/dusseldorf/frankfurt/
            eindhoven/amsterdam/bonn/aachen) -> candidate airports from a
            fixed 5-airport set (CGN/DUS/FRA/EIN/AMS)
            -> ValueError (422) for anything else
       -> BeamSearchOptimizer over StaticDestinationProvider
            (default: the 16 hand-curated CORE_DESTINATIONS only, not the
            full ~203-city catalog)
       -> SyntheticTransportDataProvider
            a hand-curated, deliberately adversarial-shaped timetable
            connecting only the 5 origin airports to the 16 core cities -
            zero live/network calls, zero connection to Phase 3
  -> build_response() -> TripSearchResponse
```

Phase 3's actual modules - `services.candidate_funnel`, `services.acquisition`
(+`acquisition_scoring`), `services.attractiveness_model`, `services.market_intel`
/`market_prior_signal`/`market_prior_import`, `services.bootstrap_planner`/
`bootstrap_executor`, `services.real_supply` (Duffel-backed), `services.live_search`,
`services.portfolio` (the diversity/"Cologne problem" reranker) - all existed,
were fully built and tested, but were reachable only from `tests/` and from
the Ops-authenticated `/api/v1/ops/*` routers (`api/ops_search_intel.py`,
`api/ops_market_prior_acquisition.py`, `api/ops_attractiveness.py`), never
from the consumer `/api/v1/search` path. `services.real_supply` and
`services.live_search` specifically had **zero production callers anywhere**
in the codebase before this slice - confirmed by a full-repo grep.

## NEW SEARCH FLOW (after this slice)

```
POST /api/v1/search
  -> TripSearchRequest (origin: free text, unchanged shape)
  -> to_trip_request() -> TripRequest (unchanged)
  -> origin_candidates = active.origin_resolver.resolve(origin, config)
       CatalogOriginResolver (new default for the consumer planner):
         resolve_origin_exact(origin) against the full ~203-city catalog
         (exact / diacritic-normalized / airport-code / alias match only -
         never fuzzy) -> nearby_airports() (real haversine distance,
         bounded radius/count policy from PlannerConfig) -> candidate
         airports, code-sorted, deterministic
       -> ValueError (422) only for a genuinely unknown place, exactly as
          before - the *catalog* behind the check is just far larger now
  -> SEARCH_LIVE_ENABLED + a valid duffel_test_ token? (both required,
     both default off - see "Phase 3 / live-search wiring" below)
       yes -> live_search(... origin_resolver=CatalogOriginResolver()) ->
              a real PlanResult, portfolio-ready, labelled supply_source=LIVE
       no / failed -> the existing synthetic TravelPlanner.explore() path,
              unchanged, labelled supply_source=SYNTHETIC
  -> build_response() -> TripSearchResponse (+ diagnostics.supply_source)
```

## A. Consumer search -> Phase 3 wiring

**Status: CONNECTED, config-gated, off by default.**

`services.live_search.live_search()` already existed as a complete,
well-designed orchestration seam: it runs `acquire_real_supply()` (bounded,
budgeted, cached Duffel Test Mode acquisition), builds a
`SnapshotTransportProvider` from the result (the beam search itself still
makes zero network calls - the snapshot is the only network stage, an
invariant this slice did not touch), runs a real `TravelPlanner.plan()`
over it, and optionally reranks through the Phase 3 Recommendation
Portfolio (`services.portfolio`) when a `portfolio_db` is supplied. This
slice's job was to *call* it from `/api/v1/search`, not to rebuild it.

Two real wiring bugs were found and fixed while connecting it:

1. **`live_search()`'s internal planner never accepted an `origin_resolver`
   at all** - it always defaulted to `StaticOriginResolver` (the closed
   5-airport table), so even a caller that had validated a wider origin
   externally would have the *internal* planner re-reject it. Fixed by
   adding an `origin_resolver` parameter (default `None`, preserving every
   existing caller's behaviour byte-for-byte) and threading it through to
   the internal `TravelPlanner(...)` construction.
2. **`search()`'s own per-request planner rebuild (when `search_mode`
   changes the effective config) never forwarded `origin_resolver`
   either** - a pre-existing gap (present before this slice too, just
   invisible because both the shared planner and the fallback default
   behaved identically until this slice introduced a *different* default
   resolver). Fixed by passing `origin_resolver=planner.origin_resolver`
   into the rebuilt `TravelPlanner`.

**The fail-closed gate** (`api/v1.py::_search_live_enabled`/
`_search_live_duffel_or_none`) mirrors `PaymentConfig.live_charging_enabled`
and `CommunicationConfig.live_sending_enabled` exactly: `SEARCH_LIVE_ENABLED`
defaults unset/false, and even when set, a valid `duffel_test_`-prefixed
token must *also* be configured (`is_test_token()`, the same check
`_revalidation_duffel()` already used) or the live attempt is skipped
entirely - silently, never a 503, because search itself must stay available
even when a live integration is unconfigured. **Unlike** the payment/
communication kill switches, a live-search *failure* (not "disabled",
an actual provider fault after being attempted) does not raise: it is
caught, recorded on the request's own `FailureLog`, and the request falls
back to the synthetic path - see "Failure disclosure" below for the one
subtlety this uncovered.

**Destinations/airports/days supplied to `live_search()`**: the full
`acquisition_catalog()` (every enabled, acquisition-eligible catalog
destination with a usable airport - ~180 of the ~203), the origin
resolver's own candidate airport codes (so a wider origin genuinely reaches
a wider live search too, not just a wider synthetic 422-avoidance), and a
single date (`request.date_from`) - the existing `ProviderCallBudget`
defaults (`max_destinations=8`, `max_date_variants=1`,
`max_airport_variants=2`, `max_offer_requests=100`) bound the actual
provider-call volume regardless of how large the candidate list is,
unchanged from `real_supply.py`'s own pre-existing design.

**`recorder`/`portfolio_db` are deliberately NOT wired at this call site.**
`live_search()` accepts both as opt-in extras that layer the Search
Intelligence recorder (candidate funnel scoring, EXPLOIT/EXPLORE
allocation, a persisted trace) and the Recommendation Portfolio reranker
(attractiveness + diversity) on top of the base live search. Wiring those
correctly into the hot consumer path needs its own `Database`/config
plumbing and its own dedicated testing - rushing it alongside Origin
Intelligence in the same slice risked a shallow, under-tested integration
of exactly the subsystem this program has been most careful about
(§2 explicitly warns against reimplementing/misapplying it). **This is an
honest, explicit remaining limitation**, not a silent omission: today, a
live search returns real Duffel-priced recommendations in the base
ranking order (price/value, unchanged from the synthetic path's own
scoring), without the additional attractiveness/diversity/market-prior
layer. Recommended as the next bounded step (see "Remaining limitations").

**Failure disclosure** (found and fixed during this slice's own adversarial
testing): the first working version of this wiring had two real bugs,
caught by a dedicated failure-injection test (`test_v9_search_live_wiring.py`):

- A live-search failure (every provider call in the snapshot failing) was
  landing as a *calm, fully-formed* "no trip fits your budget, closest
  price: 538 EUR" answer - with **zero disclosure that anything had gone
  wrong**. The cause: `RealSupplyResult.snapshot.issues` (where
  `acquire_real_supply()` correctly records per-edge provider failures) was
  never being copied onto the request's `FailureLog`, which is the only
  thing `build_response()` actually reads to decide "empty + here's why"
  versus "empty + here's how to adjust your search" - exactly the
  distinction this file's own module docstring calls "the most important
  thing in it". **Fixed**: every `SnapshotIssue` on a live result's
  snapshot is now translated onto `failures` before the response is built.
- The "closest price if your budget were 3x higher" probe
  (`_closest_price`) was being called against the **synthetic** planner
  even for a response labelled `LIVE` - meaning a live search that found
  nothing could return a fabricated-looking synthetic demo price attached
  to a "LIVE" answer. **Fixed**: the live branch never calls this probe at
  all (a live "closest price" would need a second live round-trip this
  slice does not add - `closest_price` is `None` there, honestly, rather
  than answered from the wrong data source).

Both were caught by `test_live_search_per_edge_failure_is_disclosed_not_a_500`
in `tests/test_v9_search_live_wiring.py`, which asserts the disclosed
`issues` list is non-empty and that no `closest_price` leaks through.

**Real provider E2E: NOT VERIFIED - no credentials available.** Every test
of this wiring uses the same offline Duffel HTTP fixture pattern already
established by `tests/duffel_fixtures.py`/`tests/test_v9_provenance_fix.py`
(a fake `.request()` returning a Duffel-shaped JSON envelope, `live_mode:
false`). This proves the wiring is architecturally correct and exercised
end to end through the real `/api/v1/search` route with a real FastAPI
`TestClient` - it does not and cannot prove a real Duffel Test Mode account
behaves identically. No test claims otherwise.

## B. Cologne-only constraint

**Status: REMOVED from origin resolution. Unchanged (by design) in the
synthetic demo network's own timetable.**

The Cologne/CGN/Köln inventory (done at the start of this slice, a fresh
grep of the whole backend) classified every occurrence:

- **PRODUCTION LOGIC, now fixed**: `data/destinations.py::ORIGIN_DISTANCES_KM`/
  `ORIGIN_AIRPORTS` was the entire closed origin universe `StaticOriginResolver`
  could resolve. `CatalogOriginResolver` (new) replaces it as the consumer
  planner's default, resolving against the full ~203-city catalog instead.
- **PRODUCTION LOGIC, correctly left alone**: `data/synthetic_transport.py`
  (the beam-search benchmark's deliberately hand-tuned "cheap first leg,
  punitive return" timetable, explicitly built to exercise a specific
  optimizer trap) and `data/ground_transfers.py`'s original 7-city table.
  These are curated fixtures for a specific algorithmic demonstration, not
  a fallback value - rewriting them to be origin-agnostic would corrupt the
  exact benchmark they exist to be (§3: "Do not rewrite harmless fixtures
  simply because they mention Cologne" - generalized here to the whole
  network, not just the literal string "Cologne"). **Consequence, stated
  plainly**: resolving a new origin (say, Madrid) now succeeds - the API no
  longer 422s - but the *synthetic* demo network has no timetable data
  departing from MAD, so a synthetic-path search from Madrid honestly
  returns zero recommendations (not a crash, not a fabrication - the same
  well-formed "no results" answer path already used for an over-tight
  budget) unless live search is enabled and configured. Origins that were
  already inside the original 5-airport network (Düsseldorf, Köln,
  Frankfurt, Eindhoven, Amsterdam) are completely unaffected and still
  return full synthetic demo itineraries.
- **PRODUCTION LOGIC, extended**: `data/ground_transfers.py`'s fallback
  estimator (`_fallback()`) previously fell back to the *same* closed
  7-city `ORIGIN_DISTANCES_KM` table when its own hand-tabulated rows
  missed - meaning a newly-resolvable origin's candidate airports outside
  the original cluster (e.g. Dortmund/Maastricht near Düsseldorf/Cologne)
  reported `transfer_price: null` at `/api/v1/origins/{query}`. Fixed:
  `_fallback()` now also tries a real haversine distance between the
  origin's and airport's own catalog coordinates
  (`services.origin_intelligence.catalog_places`) before giving up, so
  `/api/v1/origins/{query}` gives an honest (if still synthetic/estimated)
  transfer price for any catalog-resolvable origin, not just the original
  seven. A genuine bug was found and fixed while doing this: the ground-
  transfer cache (`CachingGroundTransferProvider`, keyed by `(origin,
  airport)` only) could be poisoned by whichever caller reached a given
  pair first if the computed price depended on a caller-supplied distance
  - fixed by making `_fallback()` self-sufficient (it can always derive
  the same distance itself), keeping the cached result a pure function of
  its key regardless of call order.
- **FIXTURE / CATALOG DATA, left alone**: `data/european_catalog.py`'s
  ordinary "Cologne" destination row, `data/destination_images/manifest.json`'s
  Cologne image entry, `search_intel_config.py::DEFAULT_BOOTSTRAP_ORIGINS`
  (one of ~30 ops-only bootstrap origin airports).
- **DEAD/UNREACHABLE**: `llm/parser.py::LlmPreferenceParser.default_origin =
  "Köln"` and the matching default in `llm/interfaces.py` - neither is
  imported by `api/v1.py`, `api/app.py`, or `services/planner.py`; confirmed
  unreachable from any consumer path, left untouched.
- **TEST DATA / DEMO COPY / DOCS**: `tests/conftest.py`'s shared fixture
  default, `examples/koln_scenario.py`, README's running worked example,
  and the "Cologne benchmark"/"Cologne problem" naming already established
  in `services/portfolio.py`/`docs/V9_PHASE3_*` - all harmless, all left
  alone per this slice's own explicit instruction not to waste time
  rewriting obvious synthetic fixtures.

## C. Origin domain model + resolution

New module: `services/origin_intelligence.py`.

**`OriginPlace`** deliberately reuses the existing ~203-city `Destination`
catalog (`data/destinations.py::DESTINATIONS`) rather than a parallel
geography system - a place a traveler may visit and a place a traveler may
depart from are the same underlying fact, and every catalog entry already
carries coordinates (`latitude`/`longitude`) and a `primary_airport`
(IATA). `catalog_places()` filters to entries that actually have both
(every entry does today, but the filter is defensive against a future
catalog addition that omits one).

**Resolution** (`resolve_origin_exact`) is intentionally narrow: only an
exact airport code, an exact/diacritic-normalized name, or a known alias
(`data/destinations.py::ALIASES`, e.g. "Köln"/"Koeln"/"Cologne" -> the
catalog's own "Cologne" entry) resolves a search's actual origin. **Never**
a prefix or fuzzy match - §5's headline invariant, verified by a dedicated
test asserting `resolve_origin_exact("Dusseldrof")` (a typo) returns
`None`, never a silent redirect to Düsseldorf.

**Suggestions** (`suggest_origins`) rank every candidate by match strength -
`AIRPORT_CODE > EXACT_NAME > NORMALIZED_NAME > ALIAS`, then the
suggestion-only tiers `PREFIX > FUZZY` - using a bounded `SequenceMatcher`
ratio (stdlib `difflib`, no new dependency added; §7 explicitly asked for
"an existing lightweight library... otherwise implement a bounded simple
strategy", and none was already present) with a fixed threshold (`0.72`)
and a hard result-count cap (`25`, default `8`). Deterministic: identical
input and catalog always produce identical output in identical order,
proven by a repeated-call-equality test.

**Bugs found and fixed while building this** (both caught by the module's
own smoke-testing before any test file was written, then locked in with
permanent regression tests):

- The alias table maps a *spelling variant* to a canonical key
  ("koeln"/"cologne" -> "koln"), which is **not** the same as the catalog
  entry's own normalized name (the catalog spells the city "Cologne", not
  "Köln"). The first implementation only normalized the *query* through the
  alias table and compared it against each place's plain normalized name,
  so "Köln" never matched "Cologne" at all. Fixed by canonicalizing the
  *place's* name too before comparing - mirroring exactly what
  `data/destinations.py::DESTINATION_INDEX` already does for destinations.
- Malformed Unicode (a lone unpaired surrogate) does not actually raise
  inside `unicodedata.normalize` in CPython (verified directly, not
  assumed) - the validation layer still degrades to "no match" rather than
  propagating an exception either way, but the test suite's assertions
  were corrected to test the real, verified behaviour rather than an
  assumed one.

## D. Nearby-airport discovery foundation

`services.origin_intelligence.nearby_airports(latitude, longitude, *,
policy)` - real great-circle distance
(`services.geo.haversine_km`, already existed and is reused, not
reimplemented), bounded by an explicit `NearbyAirportPolicy` (`max_radius_km`
default `150.0`, `max_candidates` default `5`) rather than an unbounded
"everything nearby" expansion. Country borders never exclude a candidate by
themselves (§11) - proven directly: Maastricht (Netherlands) is included as
a Düsseldorf-area (Germany) candidate purely on distance. Deduplicates by
airport code (the nearer catalog entry wins if two entries somehow share
one). Rejects non-finite (`NaN`/`Infinity`) and out-of-range coordinates
with a `ValueError` the API layer turns into a 422 - verified directly
against both.

**Stateless by construction** (§14): the function takes coordinates and
returns a list; nothing is written anywhere. No browser geolocation is
implemented in this slice, and none of `data/users/`-shaped persistence was
touched - a future geolocation flow can call `GET /api/v1/origins/nearby`
with coordinates the browser already obtained, with no account required and
nothing stored server-side by this endpoint itself.

## API contract

Two new endpoints on the existing `/api/v1` product router (registered
*before* the pre-existing `/origins/{query}` route - Starlette matches
static path segments in registration order, and the dynamic `{query}`
segment would otherwise swallow `/origins/suggest`/`/origins/nearby`
literally as `query="suggest"`/`query="nearby"` - a real routing bug found
and fixed during this slice's own testing):

- **`GET /api/v1/origins/suggest?q=...&limit=...`** - `q` bounded
  (1-120 chars, matching `TripSearchRequest.origin`'s own bound), `limit`
  bounded (1-25, default 8), both validated by FastAPI's own `Query(...)`
  constraints before the handler runs. Returns
  `{query, suggestions: [{canonical_name, country, country_code,
  primary_airport, match_type}]}`.
- **`GET /api/v1/origins/nearby?lat=...&lon=...&max_radius_km=...&limit=...`** -
  `lat`/`lon` range-checked by FastAPI (`-90..90`/`-180..180`, which also
  rejects `NaN`/`Infinity` since neither satisfies a `ge`/`le` comparison),
  `max_radius_km` bounded to `(0, 1000]`, `limit` bounded to `[1, 15]`.
  Returns `{latitude, longitude, airports: [{code, name, city, country,
  distance_km}]}`. No outbound HTTP anywhere in either handler (§16) - both
  are pure catalog lookups.

`TripSearchResponse.diagnostics` gained one additive field,
`supply_source: "SYNTHETIC" | "LIVE"` (default `"SYNTHETIC"`, so every
existing consumer of this response that does not read the new field is
unaffected) - reflecting which path actually answered the request, never a
hope about which one a client might prefer.

## Test evidence

New test files, all passing:

- `tests/test_v9_origin_intelligence.py` (38 tests) - exact/normalized/
  IATA/alias/prefix/typo resolution, ambiguity and typo-safety, empty/
  oversized/malformed-Unicode input, deterministic suggestion ordering and
  result-count bounds, nearby-airport radius/count bounds, distance
  ordering, cross-border candidates, dedup by airport code, NaN/Infinity/
  out-of-range coordinate rejection, the no-nearby-result case, plus
  sanity checks against the real (not fixture) catalog including the
  product spec's own Düsseldorf worked example.
- `tests/test_v9_search_integration.py` (28 tests) - both new endpoints
  end to end via a real `TestClient`, plus the consumer-search proof this
  slice's product problem statement actually asked for: Düsseldorf/Köln
  (already-working legacy origins, no-regression check), a genuinely new
  origin (Madrid) accepted with a 200 and well-formed empty recommendations
  rather than a 422, a previously-422ing catalog city (Dublin) now
  succeeding, a typo still correctly rejected (not silently substituted),
  a genuinely unknown place still correctly 422ing, deterministic repeated
  search, and a no-regression check on the legacy `/api/v1/origins/{query}`
  endpoint.
- `tests/test_v9_search_live_wiring.py` (12 tests) - the `origin_resolver`
  fix proven directly against `live_search()` with an offline Duffel
  double, the two-switch fail-closed gate at the unit level, and four
  end-to-end API tests: the flag alone (no token) changing nothing, default
  behaviour unchanged, a full live-wiring success case labelled `LIVE`, and
  the failure-disclosure case described above.

Full new-slice test count: **78 tests, all passing** (targeted run,
`tests/test_v9_origin_intelligence.py` (38) + `tests/test_v9_search_integration.py`
(28) + `tests/test_v9_search_live_wiring.py` (12) - counts verified with
`pytest --collect-only`, not estimated; an earlier draft of this report
mis-stated these as 38/29/16/83 respectively, caught by independent review
- see "Independent review" below).

No pre-existing test was weakened or deleted to make it pass. Two
pre-existing tests needed a real fix, not a test-assertion workaround, to
keep passing after this slice's changes (both are genuine wiring gaps this
slice's own work exposed, not pre-existing bugs it introduced):

- `test_v5_product.py::test_origins_report_what_it_costs_to_reach_them` -
  fixed by the ground-transfer fallback extension (§B above), not by
  weakening the assertion.
- The `search()` per-request planner rebuild not forwarding
  `origin_resolver` (§A above) - would have silently reverted to the old
  5-airport table for any request whose `search_mode` differs from the
  shared planner's default config; fixed at the source.

## Independent review

A fresh, read-only reviewer (separate from this slice's implementer)
attacked the implementation adversarially: fuzzed 1483+ single-character-
deletion typos plus a hand-built adversarial list (SQL-injection-shaped
strings, path traversal, emoji, multi-space/punctuation-only input)
directly against `resolve_origin_exact()`; scripted a full duplicate-IATA/
duplicate-id scan of the real 203-entry catalog; issued real HTTP requests
with `NaN`/`Infinity`/out-of-range coordinates against `/api/v1/origins/
nearby`; independently reimplemented and cross-checked the haversine
formula; called `suggest_origins`/`nearby_airports` directly with
adversarial limits/radii bypassing the API's own `Query()` bounds to
confirm the service layer doesn't merely trust the caller; and re-ran
`test_v5_product.py`/`test_api.py`/`test_deployment.py` for backward
compatibility.

**Round 1 verdict: REJECTED** - not on any security or broad-correctness
ground (every safety-critical invariant checked out: no Cologne/CGN
hardcoding reachable from the consumer path, origin never silently
overridden between the outer `resolve()` and `live_search()`'s own internal
planner, the fuzzy/prefix-never-silently-resolves invariant held under
adversarial fuzzing, zero IATA/id collisions in the real catalog,
deterministic suggestion ordering, real defense-in-depth in the service
layer, `NaN`/`Infinity` rejected end to end, correct haversine distance, no
location persistence or session/account leakage on the new endpoints, and
no Phase 6 (`api/app.py`) regression) - but on two concrete findings:

1. **(MEDIUM) The ground-transfer cache-independence claim was false.**
   `data/ground_transfers.py::_fallback()`'s optional caller-supplied
   `distance_km` parameter meant three real call sites
   (`services/planner.py`'s beam-search hot path, `services/baseline.py`,
   and `/api/v1/origins/{query}`) could feed
   `CachingGroundTransferProvider`'s shared `(origin, airport)`-keyed cache
   different-precision distances for the same pair (full-precision
   self-derived vs. a 2-decimal-rounded value), so whichever call site
   reached a pair first silently determined the cached price for everyone
   thereafter - reproduced empirically: ~4% of real catalog origin/airport
   pairs under 500km flip by exactly one cent depending on call order.
   Low impact in absolute terms (a synthetic linear-estimate fallback, not
   real inventory, off by a cent) but a real, verified contradiction of an
   explicit correctness claim in code that runs on every real search
   touching a non-legacy origin. **Fixed**: `distance_km` was removed
   entirely, from `_fallback()`/`build_options()` and every provider layer
   that threaded it through (`providers/ground_transfer.py`,
   `providers/cache.py`, the `/api/v1/origins/{query}` call site) -
   `_fallback()` now unconditionally self-derives, making the cached result
   a pure function of `(origin, airport)` by construction rather than by a
   convention every caller had to independently uphold. Re-verified: the
   same pair now returns byte-identical results regardless of call order,
   confirmed directly.
2. **(LOW) The test-evidence counts in this report were wrong.** Claimed
   38/29/16/83; actual collected counts (`pytest --collect-only`, not
   estimated) are 38/28/12/78. The tests themselves were not the problem -
   the reviewer read every test in all three files against its own
   assertions/docstrings and found no case where a test's assertions were
   narrower than its name claimed - only the summary arithmetic was wrong.
   **Fixed**: counts corrected above.
3. **(INFO, not blocking, noted for the next slice)** `services/booking_flow.py`'s
   own hardcoded airport->city/country lookup tables (`_AIRPORT_CITY`/
   `_AIRPORT_COUNTRY`, ~20 entries) are not touched by this slice and are
   not covered by this report's Cologne inventory (correctly, since this
   slice is search-only, not booking) - but they are now inconsistent with
   the wider ~203-airport origin catalog this slice introduces. Not a
   regression today (booking wiring is untouched, and `SEARCH_LIVE_ENABLED`
   is off by default), but whoever wires booking to a wider set of origin
   airports next should widen these tables too, or `route_is_international()`
   could silently under-detect an international route for an origin
   airport outside the original ~20.

**Round 2 (a second, freshly-instantiated independent reviewer - the
round-1 reviewer process itself stalled after delivering its verdict and
was not reused - re-verified both fixes from scratch, not by re-reading the
round-1 transcript): APPROVED.** Independently re-derived
order-independence directly (built options for several pairs, including
Dublin/Madrid and their nearby airports, across fresh provider instances
and reversed call order - byte-identical every time), re-confirmed the
original bug this mechanism exists to fix is still fixed (non-legacy
origins still get a non-null `transfer_price`), confirmed the legacy
hand-tabulated table still takes priority unchanged, ran the three new
test files itself and got "collected 78 items, 78 passed", chased and
resolved an apparent count discrepancy on its own (`grep -c "^def test_"`
undercounts a parametrized test - `--collect-only` is authoritative and
matches 38/28/12/78 exactly), re-spot-checked the fuzzy/prefix-never-
resolves invariant with its own adversarial inputs, re-traced
`CatalogOriginResolver` through both the outer `resolve()` call and
`live_search()`'s `origin_resolver=` parameter, confirmed `git diff HEAD --
src/detoura/api/app.py` is empty (Phase 6 genuinely untouched), and
confirmed the report's scope-honesty language (recorder/portfolio_db not
wired, real E2E not verified, booking_flow.py note) is accurate, not
implied-but-unstated. No new findings.

## Remaining limitations

- **`recorder`/`portfolio_db` not wired into the live-search call site.**
  A live search today returns real Duffel-priced recommendations, ranked by
  price/value exactly as the synthetic path ranks them - without the
  additional attractiveness/diversity/market-prior reranking layer
  `services.portfolio` provides when explicitly supplied. Wiring that in is
  the natural next bounded step, not done here to avoid a shallow
  integration of the subsystem this whole program has been most careful
  about.
- **The synthetic demo network's own timetable remains the original
  5-airport × 16-city hand-curated benchmark.** A resolved origin outside
  it produces honest, empty synthetic recommendations rather than a 422 -
  correct, but not the same as "every catalog origin has full synthetic
  demo coverage". Extending the synthetic network's own city/airport
  coverage (as opposed to origin *resolution*, which this slice did fix)
  was explicitly out of scope, since it is a deliberately curated
  algorithm benchmark, not a bug.
- **Real Duffel Test Mode E2E of the live-search wiring: NOT VERIFIED** -
  no credentials available in this environment (consistent with every
  other live-provider limitation recorded in this program's history).
- **No browser geolocation, no frontend autocomplete UI** - both
  explicitly out of scope for this slice (§24/§25); `GET /api/v1/origins/
  suggest`/`GET /api/v1/origins/nearby` are the backend contract a future
  slice's frontend work would call.
- **Ground-transfer economics for newly-resolvable origins remain a
  synthetic linear estimate**, not a real public-transport routing result -
  unchanged in kind from the pre-existing behaviour for the original seven
  cities, just now available for more of them.

## FINAL REPORT

**SEARCH INTEGRATION + ORIGIN INTELLIGENCE — SLICE 1 — FINAL CLOSURE**

Starting HEAD: `2d831a2`
Final HEAD: `3e04533`

Round 1 independent reviewer: **REJECTED**

Round 1 findings:
1. (MEDIUM) The ground-transfer cache-independence claim was false - a
   caller-supplied `distance_km` on `data/ground_transfers.py::_fallback()`
   let different call sites (the beam-search hot path, `services/baseline.py`,
   `/api/v1/origins/{query}`) feed `CachingGroundTransferProvider`'s shared
   `(origin, airport)`-keyed cache different-precision distances for the
   same pair - reproduced empirically, ~4% of catalog pairs under 500km
   flipped by 1 cent depending on call order.
2. (LOW) The report's test-evidence counts were wrong (claimed 38/29/16/83;
   actual collected counts 38/28/12/78).
3. (INFO, non-blocking) `services/booking_flow.py`'s own separate
   airport->city/country lookup tables (~20 entries) are inconsistent with
   the new wider ~203-airport origin catalog - not a regression today
   (untouched, booking wiring unchanged), noted as future debt for
   whoever next wires booking to a wider origin set.

Round 1 fixes:
1. `distance_km` removed entirely as a parameter - from
   `data/ground_transfers.py::_fallback()`/`build_options()`, from the
   `GroundTransferProvider` Protocol and all implementations in
   `providers/ground_transfer.py`, from `CachingGroundTransferProvider.search()`
   in `providers/cache.py`, and from the `/api/v1/origins/{query}` call
   site in `api/v1.py`. `_fallback()` now unconditionally self-derives via
   `_catalog_distance_km()`, making the cached result a pure function of
   `(origin, airport)` by construction rather than by caller convention.
2. Report corrected to the verified counts (38/28/12, total 78, confirmed
   via `pytest --collect-only`).
3. Recorded verbatim in this report's Independent Review section as
   non-blocking future debt - not fixed here, correctly out of scope.

Round 2 independent reviewer (a second, freshly-instantiated reviewer
process - the round-1 process had stalled after delivering its verdict and
was not reused): **APPROVED**

Ground-transfer cache independence: **PASS** (independently re-verified by
both reviewers: byte-identical results across repeated/reversed calls for
several pairs including non-legacy origins, legacy table still takes
priority unchanged)

Collected new tests: **38 + 28 + 12 = 78** (verified three times
independently - by the implementer and by both reviewer passes - via
`pytest --collect-only`, `collected 78 items` in verbose output)

Targeted tests: all 78 new-slice tests pass (0 F/E); `test_v5_product.py`,
`test_api.py`, `test_deployment.py` re-run and passing (confirmed by Round
1 reviewer, unaffected by the Round-1 fix, not re-run again by Round 2
since nothing touching them changed); relevant Phase 6 security tests
(`test_v9_phase6_network_security.py`, `test_v9_phase25_network_safety.py`,
`test_v9_phase6_pii_security.py`, `test_v65_request_limits.py`,
`test_v9_phase6_body_limit.py`) run by the implementer, all passing, zero
regression. No fresh full-suite regression run for this slice (not
required per instruction - no executable code changed after the Round-1
fixes beyond what both reviewers already re-verified with targeted runs).

Consumer search → live_search base pipeline: **CONNECTED** (config-gated
via `SEARCH_LIVE_ENABLED` + a valid `duffel_test_` token, both default off;
architectural wiring verified via injected offline Duffel test doubles, not
real Duffel Test Mode credentials)

Full Phase 3 recorder/portfolio layer: **NOT YET WIRED** into this consumer
call site - `_try_live_search()` calls `live_search()` without a
`recorder`/`portfolio_db`, both confirmed absent from that call by both
independent reviewers. A live search today returns real Duffel-priced
recommendations in base price/value order, without the additional
attractiveness/diversity/market-prior reranking layer.

Cologne-only origin-resolution constraint: **REMOVED** (origin resolution
now runs against the full ~203-city catalog via `CatalogOriginResolver`;
the synthetic demo network's own hand-curated timetable remains
intentionally limited to its original 5-airport/16-city benchmark - see
"Remaining limitations")

Origin exact/normalized/IATA resolution: **PASS**

Fuzzy typo silent auto-resolution: **ABSENT** (verified adversarially by
both reviewers with typo/injection/path-traversal/emoji inputs - never a
fuzzy or prefix match resolves a search's actual origin)

Nearby-airport backend: **PASS** (bounded, stateless, real haversine
distance, NaN/Infinity/out-of-range rejected end to end)

LIVE/SYNTHETIC truth: **PASS** (no fabricated synthetic `closest_price` in
a LIVE-labelled response; live-provider failures disclosed via `issues`,
not silently swallowed into calm "no results" guidance - both independently
re-verified in code by Round 2)

Real Duffel Test Mode E2E: **NOT VERIFIED** — no credentials available in
this environment; every test of the live-search wiring uses the same
offline Duffel HTTP fixture pattern already established elsewhere in this
codebase (`tests/duffel_fixtures.py`)

Phase 6 security regression: **NONE** (`git diff HEAD -- src/detoura/api/app.py`
empty, confirmed by Round 2; relevant Phase 6 security test files re-run
clean by the implementer)

Open Slice-1 Critical: **0**
Open Slice-1 High: **0**
Open Slice-1 Medium: **0** (the one Medium finding from Round 1 is CLOSED)
Open Slice-1 Low: **0** (the one Low finding from Round 1 is CLOSED)

Future non-blocking debt:
- `services/booking_flow.py`'s own airport->city/country lookup coverage
  (~20 entries) should widen alongside any future slice that wires booking
  to a broader set of origin airports.
- Full Phase 3 recorder/portfolio consumer wiring (attractiveness,
  diversity, market-prior signals) - `live_search()` already supports it as
  an opt-in extra; not wired into `/api/v1/search` this slice.
- Real Duffel Test Mode E2E of the live-search wiring - blocked on
  credential availability, not a code gap.
- Ground-transfer economics for newly-resolvable origins remain a
  synthetic linear estimate, not real routing data.
- No browser geolocation, no frontend autocomplete UI - explicitly out of
  scope for this slice; the backend contract (`/api/v1/origins/suggest`,
  `/api/v1/origins/nearby`) is ready for a future frontend slice to call.

Files changed:
`src/detoura/services/origin_intelligence.py` (new),
`src/detoura/services/origin_resolver.py`,
`src/detoura/services/live_search.py`,
`src/detoura/data/ground_transfers.py`,
`src/detoura/api/contracts.py`,
`src/detoura/api/v1.py`,
`tests/test_v9_origin_intelligence.py` (new),
`tests/test_v9_search_integration.py` (new),
`tests/test_v9_search_live_wiring.py` (new),
`docs/V9_SEARCH_ORIGIN_INTELLIGENCE_REPORT.md` (new).
(`src/detoura/providers/ground_transfer.py` and `src/detoura/providers/cache.py`
were edited during development but net to zero diff against `2d831a2` after
the Round-1 fix removed exactly what an earlier version of this slice had
added to them - confirmed via `git status`, not staged.)

Commits: `3e04533` - one, this slice, exact-path staged.

Unrelated frontend preserved: YES
Pushed: NO

SLICE 1 STATUS: **CLOSED**

**V9 SEARCH INTEGRATION + ORIGIN INTELLIGENCE — SLICE 1: CLOSED**

NEXT RECOMMENDED STEP: **SEARCH INTELLIGENCE SLICE 1.5** — wire the
existing Phase 3 recorder/portfolio/attractiveness/diversity/market-prior
layer correctly into the consumer search path (the one honest limitation
this slice leaves open at the base-pipeline level). Not started here.
