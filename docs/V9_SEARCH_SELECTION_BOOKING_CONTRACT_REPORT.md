# V9 Search Selection → Booking Intent Contract

Backend contract patch closing the gap Codex found while implementing
Frontend Reconnection Slice B (Checkout + Purchase): a real, provider-backed
search recommendation had a server-issued selection identifier recorded
internally, but no way for the consumer to learn it, so it could never
truthfully create its corresponding `BookingIntent`.

## Baseline

- Authoritative baseline before concurrent work: `07d1b1e57ca738d3ef9e48025224911a7b586d59`
- HEAD when this task started: `07d1b1e57ca738d3ef9e48025224911a7b586d59`
- HEAD advanced during this task, independent of this work, when a concurrent
  Claude session committed the financial-document-download API
  (`cb5e10f59062a93a6d08b339548d81f7893e8949`, "V9 expose secure financial
  document downloads"). That commit touches only `src/detoura/api/me_trips.py`
  and its own test file — no overlap with anything below.

## Selection lifecycle (as found, before this patch)

- **Generation**: `SelectionStore.record()` (`src/detoura/services/selection_store.py:112-140`)
  mints `"sel_" + secrets.token_urlsafe(18)`. Triggered from `live_search()`
  (`src/detoura/services/live_search.py`), which - for every LIVE
  recommendation whose legs *all* carry a Duffel `provider_ref`
  (`_selected_offers_for`) - records the offers behind it and gets back an id.
- **Persistence**: in-memory only (`SelectionStore`, an `OrderedDict` guarded
  by a lock), 20-minute TTL, 5,000-entry LRU cap - deliberately not a
  session/DB store (module docstring: a selection is only valid as long as
  its underlying Duffel offers are).
- **The gap**: `LiveSearchResult.selection_ids` (recommendation → selection
  id) was computed and returned by `live_search()`, but `v1.py`'s `search()`
  handler only ever read `live_result.plan_result` - the map was discarded,
  and neither `TripRecommendation` (`contracts.py`) nor
  `assembler.build_response`/`recommendation_dto` had any notion of it. Not a
  deliberate exclusion; an integration that was never finished.
- **A second, independent bug found while fixing the first**: the discarded
  map was keyed by a string rebuilt from `enumerate(result.recommendations)`
  (0-based) inside `live_search.py`, while the client-facing recommendation
  `id` is built independently in `assembler.py` from the itinerary's own
  1-based `rank`. The two schemes never matched. Wiring the old map through
  unchanged would have produced a `selection_id` that resolved to the wrong
  recommendation.

## The fix

Smallest change that closes both gaps, entirely inside the LIVE search
response path:

1. `src/detoura/api/contracts.py` — `TripRecommendation` gains
   `selection_id: str | None = None`. `null` for anything that cannot
   honestly enter the real booking flow (every synthetic recommendation, and
   any LIVE recommendation with a leg missing a bookable Duffel offer
   reference).
2. `src/detoura/services/live_search.py` — `LiveSearchResult.selection_ids`
   changed from `dict[str, str]` (mismatched string ids) to `dict[int, str]`,
   keyed by the recommendation's position in `result.recommendations` at the
   moment the selection is recorded (after any portfolio reranking has
   already run and rebound that list - see Security/Testing below).
3. `src/detoura/api/assembler.py` — `build_response()` takes an optional
   `selection_ids: dict[int, str] | None`; its recommendation list
   comprehension now enumerates `result.recommendations` and passes
   `selection_id=(selection_ids or {}).get(index)` into `recommendation_dto()`,
   which threads it straight into the `TripRecommendation` it builds.
4. `src/detoura/api/v1.py` — the LIVE branch of `search()` now passes
   `selection_ids=live_result.selection_ids` into `build_response(...)`. The
   synthetic branch (`active.plan(...)`) is untouched and never supplies this
   argument, so every synthetic recommendation is truthfully `null`.

No other file changed. `POST /api/v1/booking-intents`, `create_run_from_selection`,
`CreateBookingIntentRequest`, and everything about payment/booking
architecture are byte-for-byte unchanged - the existing lookup
(`selection_store().get(body.selection_id)` → 404 on unknown/expired →
`create_run_from_selection`, entirely server-derived price/currency/itinerary)
already did everything this contract needed; it just never received a real
id from a real client before.

## Bookability semantics

A recommendation gets a non-null `selection_id` if and only if:

- it came from the LIVE (Duffel-backed) path, **and**
- every leg has a `provider_ref` with `provider == "duffel"` and a non-empty
  `offer_id` (`_selected_offers_for`, unchanged by this patch).

Everything else — every synthetic/demo recommendation, and any LIVE
recommendation with even one leg lacking a full provider reference — gets
`selection_id: null`. This patch changed no eligibility rule; it only
exposed the id for the recommendations that were already eligible.

## Synthetic/Prior semantics

Unaffected. The synthetic search path never calls `live_search()` or
`selection_store.record()`, so it was already, and remains, incapable of
producing a selection id. `SearchDiagnostics.supply_source` (`LIVE` /
`SYNTHETIC`) is unchanged.

## Expiry semantics

Unaffected. `SelectionStore.get()`'s lazy TTL expiry (20 minutes default,
`DETOURA_SELECTION_TTL_SECONDS` override) is untouched; an expired or unknown
id still 404s from `/api/v1/booking-intents` with the same "unknown or
expired" message, verified by a new test
(`test_an_expired_selection_still_fails_even_though_it_once_existed`).

## Security model

- `selection_id` is opaque (`secrets.token_urlsafe(18)`-derived) - never a
  raw Duffel offer id, price, or provider payload.
- `POST /api/v1/booking-intents` still derives price, currency and itinerary
  entirely from the server-stored `Selection`; `CreateBookingIntentRequest`
  has no field a client could use to override any of them (verified by
  `test_the_booking_intent_request_has_no_field_that_can_override_price_currency_or_itinerary`,
  asserting the exact allowed field set).
- No ownership/session scoping existed on `Selection` before this patch and
  none was added - the selection is a reference to the caller's own
  just-completed search, not a shared or predictable resource; nothing here
  changes who can redeem an id or widens what a valid id can do.
- Malformed input fails before ever reaching the store: `selection_id` has a
  `max_length=200` `Field` constraint on `CreateBookingIntentRequest`, so an
  oversized string 422s at validation.

## Search → BookingIntent proof

Because the offline Duffel test double in this repo's existing fixtures
cannot be made to survive the full synthetic-planner/accommodation pipeline
into a non-empty LIVE recommendation list through the actual HTTP endpoint
(confirmed: even the pre-existing `test_v9_search_live_wiring.py` and
`test_v9_provenance_fix.py` never assert a non-zero LIVE recommendation
count), the contract is proven at each real seam it touches instead of one
single live HTTP round trip:

1. `live_search()` (the real function, with only the Duffel HTTP call and
   planner faked) is proven to generate, key, and persist selection ids
   correctly - including through a **real** Recommendation Portfolio rerank
   that actually reorders two recommendations, closing the identified risk
   that an index-keyed map could silently point at the wrong offer after a
   reorder.
2. `assembler.build_response()` is proven to thread that map into the exact
   `TripRecommendation.selection_id` a client would see, using a **real**
   synthetic-planner result (guaranteed well-formed) so the DTO wiring itself
   is exercised without needing a live recommendation.
3. `/api/v1/booking-intents` is proven, through the real HTTP endpoint and
   the real process-global `SelectionStore`, to turn exactly such an id into
   the exact matching `BookingIntent` (`trip_label`, `currency`,
   `discovered_total` all asserted equal to what was recorded).

Chained together, (1)+(2) prove the search side truthfully exposes the id
`SelectionStore.record()` issued for the right recommendation, and (3) proves
that id (whichever endpoint it reached the client through) redeems correctly
- the same proof as one HTTP-to-HTTP round trip, assembled from its two real
halves rather than faked in the middle.

## Tests

New file: `tests/test_v9_search_selection_booking_contract.py` (13 tests, all
passing):

- `test_a_real_bookable_recommendation_gets_a_selection_id`
- `test_two_bookable_recommendations_never_share_a_selection_id`
- `test_a_selection_id_binds_to_the_exact_recommendation_not_a_neighbour`
- `test_selection_ids_still_bind_correctly_after_real_portfolio_reranking`
- `test_a_recommendation_with_an_unbookable_leg_gets_no_selection_id`
- `test_build_response_threads_selection_id_by_index`
- `test_build_response_defaults_every_selection_id_to_null`
- `test_the_real_synthetic_search_endpoint_never_exposes_a_selection_id`
- `test_a_search_issued_selection_id_creates_the_matching_booking_intent`
- `test_an_unknown_selection_id_fails_safely_not_a_500`
- `test_a_malformed_overlong_selection_id_is_rejected_before_lookup`
- `test_an_expired_selection_still_fails_even_though_it_once_existed`
- `test_the_booking_intent_request_has_no_field_that_can_override_price_currency_or_itinerary`

## Real Duffel E2E

NOT VERIFIED — CREDENTIALS UNAVAILABLE. No Duffel Test Mode token is
configured in this environment; only the offline HTTP-double based tests
above ran.

## Independent review

An independent, read-only review agent traced the full
`live_search → v1.search → assembler.build_response → TripRecommendation`
call path against the actual diff (not assumptions) and checked for: wrong
recommendation ↔ wrong id, id collisions, false bookability on
synthetic/unbookable recommendations, provider-secret leakage,
price/currency/itinerary tampering, expiry bypass, and ownership/enumeration
risk.

**Verdict: APPROVED.** One Low-severity nitpick — no test exercised the
portfolio-reranking branch, the one place a reorder could occur before the
selection loop runs — was closed by adding
`test_selection_ids_still_bind_correctly_after_real_portfolio_reranking`,
which drives the real reranker and confirms the id keyed by post-rerank
index still points at the correct offer.

## Regression

- Targeted (search/live-search/selection/booking-intent/ownership/portfolio):
  all green.
- Full suite (`pytest tests/`, single run): exit code 0, all green (one
  pre-existing skip, unrelated to this patch).

## Concurrent work preserved

- Frontend files (`frontend/**`) and `src/detoura/api/me_trips.py` /
  `tests/test_v9_financial_document_download_api.py` (the financial-document
  session, since committed as `cb5e10f`) were never read, staged, or
  modified by this task.
- `AGENTS.md` / `CLAUDE.md` left untouched and unstaged.

## Checkpoint commit

Isolated backend commit containing only:
`src/detoura/api/contracts.py`, `src/detoura/api/assembler.py`,
`src/detoura/api/v1.py`, `src/detoura/services/live_search.py`,
`tests/test_v9_search_selection_booking_contract.py`,
`docs/V9_SEARCH_SELECTION_BOOKING_CONTRACT_REPORT.md`.

Not pushed.
