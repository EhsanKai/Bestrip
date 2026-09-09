# V7.6 → V8 Handoff

**V7.6 HEAD:** see the release commit on `claude/travel-planner-mvp-nvb267`.
**Duffel Test Mode: UNVERIFIED.** No real Duffel response has ever been parsed by this code. The fixtures are modelled on documented shapes, never captured.
**Token: not required for V7.6.** The application starts, searches and passes its whole suite with `DUFFEL_ACCESS_TOKEN` unset — asserted, not assumed.

---

## What V8 can rely on

| Guarantee | How it is enforced |
|---|---|
| Beam search makes no network calls | `SnapshotTransportProvider` has no client, host or token; `DuffelTransportProvider.search()` refuses outright |
| Acquisition is bounded | `ProviderCallBudget` caps requests, destinations, dates and airports; the plan is counted before anything is sent |
| Truncation is disclosed | typed `CALL_BUDGET_EXHAUSTED`, `is_infrastructure=True`, never "no trips found" |
| Party size cannot leak | `travelers` is part of `AcquisitionEdge` identity; two party sizes cannot share a snapshot |
| Offers expire | `ExpiringProviderCache` dies at the earlier of TTL and provider expiry, minus a 30s margin |
| Failures are never cached | `compute()` raising stores nothing — the V6.5 invariant, intact |
| A connection is not a city | Duffel slice → one leg; segments are opaque metadata |
| Currency is never relabelled | converted via `PriceNormalizer` or refused as `CURRENCY_UNAVAILABLE` |
| Missing baggage stays UNKNOWN | never `INCLUDED`, never €0 |
| A journey cannot falsely confirm | `can_confirm` requires every required item settled; no `force` parameter exists |

**Call budget default is an INITIAL EMPIRICAL DEFAULT, not a globally optimal constant.** In the benchmarked scenario 200 requests reproduced the same winning itinerary as full 440-call coverage, against a naive 2,177. Re-derive it once real latency and rate limits are known.

## The exact V8 starting sequence

1. Obtain a `duffel_test_…` token; export `DUFFEL_ACCESS_TOKEN`.
2. Run **one** controlled Offer Request:
   `python3 -m detoura.tools.duffel_probe --origin CGN --destination BCN --date 2026-10-15`
3. Compare the real response against `tests/duffel_fixtures.py`.
4. Repair the parser **only from real evidence** — do not pre-emptively "fix" what the fixtures already model.
5. Re-run `tests/test_v75_provider.py` and `tests/test_v75_adversarial.py`.
6. Run a bounded acquisition: 2 origins × 3 destinations × 1 date.
7. Verify cache behaviour and offer expiry against real `expires_at` values.
8. First real Duffel-backed QUICK search.
9. First real Duffel-backed SMART search.
10. Begin Sandbox Order implementation.
11. Multi-ticket booking execution.
12. Revalidation.
13. Price tolerance.
14. Simulate partial booking failure.
15. Only after sandbox success, consider live-provider onboarding.

Steps 1–5 are the gate. If the real shape diverges from the fixtures, everything after step 5 is built on a guess.

## Explicitly left for V8

Real sandbox connection · real Offer Requests · real response-shape verification · real test-mode search · real offer-expiry behaviour · real baggage validation · Sandbox Order creation · multi-ticket orchestration · revalidation · price-change handling · hold support · partial-booking recovery · traveller details · booking confirmation.

**No real-money or live booking.**

## Known limitations carried in

- **The live path is unverified.** If Duffel's JSON differs from the modelled fixtures, the adapter is wrong in ways no test here can detect. This is the single largest risk V8 inherits.
- **Cabin is not part of acquisition identity.** Every acquisition requests economy today, so cabin cannot vary within one snapshot. The moment V8 offers a cabin choice, `AcquisitionEdge` must gain the field — otherwise a business-class lookup could be served an economy fare, the same class of bug the `travelers` field was added to prevent.
- **`lower_bound_per_traveller` is still called only from tests.** Baggage is priced post-search, so the admissible-bound helper guards nothing yet. It is kept and tested because deriving it under deadline is how an inadmissible bound ships.
- **Multi-slice offers are refused, not priced.** Correct today, since only one-way requests are sent. If V8 sends return requests, this needs a real per-slice attribution strategy — and there may not be an honest one.
- **`Money` uses float.** Half-up to cents, precision holds under test; a `Decimal` spine would be stronger before real money moves.
- **No rollback is modelled.** Airlines do not universally support it. Partial failure routes to a human by design.
