# V7.5 — Provider Architecture

**Status of the live path: UNVERIFIED.** No `duffel_test_` token has been available, so no real Duffel response has ever been parsed by this code. The fixtures are *modelled on documented shapes*, never captured. Everything below is offline-verified; nothing below is sandbox-verified.

---

## The problem this solves

Measured on V7's own HEAD, one search issues this many upstream route lookups:

| Search | cache lookups | **upstream calls** |
|---|---|---|
| QUICK 5d | 2,568 | **1,566** |
| SMART 5d | 5,272 | **2,177** |
| DEEP 5d | 65,944 | **2,927** |
| SMART, flexible 30-day | 20,864 | **7,515** |

At ~1s per Offer Request that is 36 minutes for one SMART search — at 16 destinations, with 50 on the roadmap. Wiring a real provider into beam expansion is not slow, it is impossible.

## The inversion

```
TripRequest
   ↓ origin resolution              existing, bounded (≤4 airports)
   ↓ candidate discovery            rank_candidates() — affinity + exploration quota
   ↓ acquisition plan               build_plan() — counted BEFORE anything is sent
   ↓ bounded provider calls         acquire() — one pass, budgeted
   ↓ OfferSnapshot                  normalized, provider-neutral
   ↓ beam search                    reads the snapshot; ZERO network calls
```

**Enforced, not documented.** `SnapshotTransportProvider` holds no HTTP client, host or token — it physically cannot reach the network. And `DuffelTransportProvider.search()` (the `TransportDataProvider` entry point beam search calls) **raises**, naming the acquisition path; `fetch_offers()` is the network method.

That refusal exists because an independent review took the construction example from this module's own docstring, wired Duffel directly into `TravelPlanner`, and measured **64 provider calls in one QUICK search**. The guarantee had held only while callers remembered to route correctly. A guarantee that depends on remembering is not one.

## Call budget, measured

`ProviderCallBudget` caps every dimension, because the explosion is multiplicative: cities × airports × dates × directions.

| budget | planned | offers | states | best score | route |
|---|---|---|---|---|---|
| 100 | 100 | 175 | 582 | 0.717781 | CGN→Berlin→CGN *(1 city)* |
| **200** | **200** | **400** | 607 | **0.723338** | CGN→Copenhagen→Berlin→CGN |
| 400 | 400 | 715 | 1,510 | 0.723338 | same |
| 600 | 440 *(full)* | 760 | 1,986 | 0.723338 | same |

**The knee is 200.** Below it coverage is too thin to build a multi-city trip at all; above it the answer stops improving.

**The quality cost is real and stated:** bounded acquisition reaches **0.723338** where full-catalog synthetic search reaches **0.734457** — roughly 1.5% — because only 8 of 16 destinations get priced. Every dropped city is named in `plan.dropped_destinations`, and truncation raises a typed `CALL_BUDGET_EXHAUSTED` issue whose `is_infrastructure` is `True`, so bounded coverage can never be reported as "no trips found".

## Candidate discovery

Explicitly **not** `catalog[:limit]` — catalog order is an artefact of the order somebody typed the file in. Also not alphabetical, not cheapest-first-leg (blind to a city that is expensive to enter and cheap to leave), and not nearest-first (just a smaller map).

Cities rank on preference affinity, richness and novelty, and **a quarter of the pool is reserved for cities the ranking rejected**. Sampling spreads at even fractions strictly *inside* the rejected tail: an earlier version stepped from the tail's head and spent one of two exploration slots on the city affinity would have picked next anyway.

This is a deliberate trade, not a free lunch. On the standard request the pool is ranks 1–6 plus 10 and 13 — ranks 7–9 are genuinely given up. That is the honest cost of picking 8 from 16, and it is disclosed rather than hidden.

## Normalization boundary

| Duffel | Detoura | Note |
|---|---|---|
| slice | one `TransportOption` leg | **not** segments — see below |
| segment | `raw_segments` metadata | opaque by contract |
| `total_amount` + `total_currency` | `Money` → `PriceNormalizer` | never relabelled |
| baggage quantity ≥ 1 | `INCLUDED` | |
| baggage quantity 0 + priced service | `EXTRA` with `Money` | |
| **field missing** | **`UNKNOWN`** | never `INCLUDED`, never €0 |
| `expires_at` | `ProviderOfferReference.expires_at` | absent = `UNKNOWN`, not "never" |
| `requires_instant_payment` | `hold_supported` tri-state | absent = unknown, not "no" |

**A slice is a leg; a segment is not a city.** `CGN → LHR → MAD` is one journey to Madrid with a connection in London. Mapping segments to legs would make the beam count Heathrow as a visited city and let a layover masquerade as a destination.

**A multi-slice offer is refused.** Its total covers every slice and nothing says how that divides; attaching the whole round-trip figure to the outbound leg priced a one-way flight at the return fare. Splitting evenly would be worse — an invented number wearing the provider's authority.

**Party size is part of an offer's identity.** `AcquisitionEdge` carries `travelers`, so a family of four cannot be served the solo fare from a `(origin, destination, day)` key.

## Cache

`ExpiringProviderCache` adds what real offers need and synthetic fares never did:

- **Expiry-aware** — entries die at the earlier of our TTL and the provider's own `expires_at`, minus a 30s safety margin. An offer that dies while the user reads the page was never usable.
- **Bounded** — LRU-capped. V7's cache is an unbounded dict on a module-level singleton planner: invisible with generated fares, a slow leak with real ones.
- **Never caches failure** — the V6.5 invariant preserved exactly: `compute()` raising stores nothing, so one timeout cannot become a permanent "no flights on this route".

## Failure taxonomy

Unchanged from V7 except one addition: **`CALL_BUDGET_EXHAUSTED`**. Its `is_infrastructure` is `True`, so it can never surface as "no trips found", and its copy says what we did rather than what exists — *"We searched part of the market for this trip, not all of it."*

## Token safety

`duffel_test_` or nothing. Missing, malformed and live-looking tokens all fail closed **before any socket opens**. The token never appears in an exception, a `repr`, or CLI output — asserted by tests that grep the output for the secret. A `max_calls` ceiling raises `ProviderCallBudgetExceeded` as a backstop against anything bypassing the plan: the alternative to an exception there is an invoice.

## To lift UNVERIFIED

```
DUFFEL_ACCESS_TOKEN=duffel_test_... \
  python3 -m detoura.tools.duffel_probe --origin CGN --destination BCN --date 2026-10-15
```

One Offer Request, read-only, no order, no payment. Compare its output against `tests/duffel_fixtures.py`, adjust normalization **only where real evidence requires it**, then re-run the suites. Only then may this document say VERIFIED.
