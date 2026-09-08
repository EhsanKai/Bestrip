# Detoura V7.5 — Phase 0 Architecture Audit

**Baseline:** `769e682` (V7, released) · **Branch:** `claude/travel-planner-mvp-nvb267` · **Remote parity:** `0 0`
**Audit type:** read-only. No source file was modified to produce this document.
**`DUFFEL_ACCESS_TOKEN`:** **absent**. All offline work proceeds; the live path stays **UNVERIFIED**.

---

## 0. Headline findings

Three things decide the shape of V7.5.

**1. The provider seam already exists and is good.** `HttpClient` is an injected Protocol; `RetryingHttpClient` handles 429 with `Retry-After`, 5xx and timeouts with bounded backoff; `RateLimiter` throttles; `HttpMetrics` counts. `AmadeusTransportProvider` (358 lines) is a working precedent for a real provider behind `TransportDataProvider`. Duffel is a *sibling of Amadeus*, not new architecture.

**2. Naive real-provider integration is impossible, by measurement.** Instrumented on this HEAD, one search issues this many **upstream** transport lookups (cache misses — i.e. what would become Duffel Offer Requests):

| Search | cache lookups | **upstream calls** | states |
|---|---|---|---|
| QUICK 5d | 2,568 | **1,566** | 1,196 |
| SMART 5d | 5,272 | **2,177** | 9,730 |
| DEEP 5d | 65,944 | **2,927** | 148,840 |
| SMART flexible, 30-day window | 20,864 | **7,515** | — |

At a realistic ~1s per Offer Request, one SMART search would take **36 minutes** and one flexible search **over two hours** — before considering rate limits or cost. This is at **16 destinations**; the roadmap targets 50+. **A bounded acquisition stage is not an optimization, it is a precondition.**

**3. The cache is right about failure and wrong about time.** `Resilient(Caching(inner))` is already ordered correctly, and `ProviderCache.get_or_compute` stores nothing when `compute()` raises — so a provider outage cannot be memoised as "no flights". But `ProviderCache` is a **plain dict with no TTL and no eviction**, held on a module-level singleton planner. Synthetic fares never expire, so this has been harmless. Real offers expire in minutes, and the cache would serve them indefinitely while growing without bound.

---

## 1–5. Where Duffel code lives, and where it stops

| Question | Answer |
|---|---|
| **1. Authentication** | Inside the adapter, `providers/duffel.py`, reading `DUFFEL_ACCESS_TOKEN` from the environment. Amadeus already does OAuth token caching in-adapter (`access_token`, `_token_is_valid`); Duffel is simpler — a static bearer token — so it needs less, not more. |
| **2. HTTP abstraction** | The existing `providers/http.py`. `RetryingHttpClient(UrllibHttpClient())` gives 429/`Retry-After`, 5xx, timeout and backoff for free. Duffel adds no networking code. |
| **3. Offer Requests** | `providers/duffel.py` only. `POST /air/offer_requests` then reading the returned offers. |
| **4. Duffel DTOs stop** | At the adapter's private `_map_offer` boundary. No `dict` shaped like a Duffel payload leaves the module. |
| **5. Neutral normalization begins** | At the `TransportDataProvider.search()` return type: `list[TransportOption]`. Everything above that already exists and already works. |

The rule the codebase already enforces for Amadeus, restated: **no `if provider == "duffel"` anywhere in planner, beam search, comparison or reoptimizer.** The optimizer understands `TransportOption`; it must never learn what an Offer is.

## 6–11. Structural mapping

Duffel's shape does not match Detoura's, and the mismatch is the interesting part.

- A Duffel **Offer** carries one or more **Slices**; each Slice has one or more **Segments**. A Segment is one flight number.
- Detoura's `TransportOption` is **one leg between two network nodes**, priced per person.

**Proposed mapping:** one Detoura leg per Duffel *slice*, not per segment. A slice is what the traveler experiences as "the flight from A to B" — a one-stop journey is still one leg of their trip, and modelling each segment separately would make the beam treat a connection as a city visit. `duration_minutes` comes from the slice; `stops = len(segments) - 1`; the marketing carrier and flight numbers of every segment are preserved on the provider reference for later display and booking.

A one-way Offer Request per (origin, destination, date) yields single-slice offers, which is the clean case and what V7.5 will use. Multi-slice offers are normalized to the first slice with the rest recorded on the reference — but **not** silently discarded, because that would misprice a return.

Airports are already IATA-coded strings throughout (`CGN`, `DUS`), matching Duffel's identifiers directly. No translation layer is needed for V7.5's routes.

## 12–13. Money

`Money(amount, currency, tax_included)` and `PriceNormalizer.to_base()` already exist, and `Money.__add__` **refuses** to add mismatched currencies. V7 Phase 3 shipped a defect where `quote_trip` summed `.amount` floats directly and turned `$20` into `€20`; the fix was to treat a foreign-currency fee as unquoted.

For V7.5: a Duffel offer's `total_amount` + `total_currency` becomes a `Money`, converted via `PriceNormalizer` when a rate exists. **If no rate exists, the failure is typed** — `ProviderFailureKind.CURRENCY_UNAVAILABLE` already exists for exactly this and is documented as "a misconfiguration". The offer must not be silently dropped as "no result", because that reports a configuration problem as an absence of flights.

`Money` uses `float` with half-up rounding to cents. Adequate and tested, but a `Decimal` spine remains the stronger choice; carried as debt, not addressed here.

## 14. Baggage

V7's semantics are authoritative and must not weaken:

| Duffel evidence | Detoura `BaggageStatus` |
|---|---|
| Passenger baggage entry says a cabin bag is included | `INCLUDED` |
| An available service quotes a price for a bag | `EXTRA` with `Money` |
| Baggage object present but says nothing usable | `UNKNOWN` |
| **Field missing entirely** | `UNKNOWN` — never `INCLUDED` |
| Provider explicitly states the bag cannot be added | `NOT_AVAILABLE` |
| Price missing on an otherwise-known extra | `EXTRA` with `price=None` (known to cost, amount unquoted) |

`BaggageAllowance` already **refuses to construct** `UNKNOWN` with a price, `NOT_AVAILABLE` with a price, or `INCLUDED` with a non-zero price. The adapter cannot invent a fee even by accident.

## 15–17. Expiry, hold, references

`PriceFreshness` (FRESH/RECENT/STALE/UNKNOWN) and `PriceProvenance` exist, but freshness is derived from *how old our quote is*, not from a **provider-stated expiry**. Duffel gives an explicit `expires_at`. This needs a new provider-neutral carrier:

```
ProviderOfferReference
    provider          "duffel"
    offer_id          opaque
    expires_at        datetime | None      None = unknown, never "never"
    hold_supported    bool | None          None = unknown
    hold_until        datetime | None
    segment_refs      opaque, for booking
```

Domain code may carry this and check `expires_at`; it must not parse it. `hold_supported=None` must stay possible — airlines are inconsistent, and multi-ticket correctness must not be designed as if hold were guaranteed.

## 18–24. Failure, retry, cache

The failure taxonomy is **already sufficient** — `NO_RESULTS`, `SOLD_OUT`, `TIMEOUT`, `UNAVAILABLE`, `MALFORMED_RESPONSE`, `CURRENCY_UNAVAILABLE`, `STALE_OFFER`, `AUTHENTICATION_FAILED`, `RATE_LIMITED` — with an `is_infrastructure` predicate whose docstring already says these "may never be reported to a user as *no trips found*". V7.5 adds exactly one member: **`CALL_BUDGET_EXHAUSTED`**.

Retries live **only** in `RetryingHttpClient`. The adapter must not retry, and the acquisition stage must not retry, or a search-level retry multiplies a provider-level retry and one bad minute becomes a request storm.

**Question 24 — can a degraded provider be cached as "no flights"? No.** `Resilient(Caching(inner))` is ordered deliberately and commented; an exception travels up through the cache, which stores nothing when `compute()` raises. This is a V6.5 lesson already learned.

**But the cache has no TTL** (`ProviderCache` is a bare dict) and lives on a module-level singleton planner. For real offers this is two defects: expired offers served as current, and unbounded memory growth. V7.5 must add TTL + expiry-aware eviction, keyed to include passenger count and cabin — a two-traveller quote must never satisfy a one-traveller lookup.

## 25–29. The call budget

Measured worst case is in §0. The architecture that fixes it:

```
TripRequest
   ↓ origin resolution                    (existing, bounded: ≤4 airports)
   ↓ cheap candidate discovery            NEW - rank destinations without network
   ↓ bounded candidate pool               NEW - hard cap, e.g. 8-10 cities
   ↓ bounded provider acquisition         NEW - one pass, budgeted, before search
   ↓ normalized offer store               NEW - pre-fetched TransportOptions
   ↓ optimizer                            (existing, unchanged - reads the store)
```

The essential inversion: today the optimizer *pulls* from the provider during beam expansion. With a real API it must instead search over a **pre-fetched, bounded set**. The beam then makes **zero** network calls.

```
ProviderCallBudget
    max_offer_requests      hard ceiling on HTTP calls
    max_destinations        candidate pool cap
    max_date_variants       flexible-date sampling cap
    max_airport_variants    origin airports actually queried
```

With 8 destinations, 2 origin airports and 1 date: 16 outbound + 56 inter-city + 16 return ≈ **88 requests** — three orders of magnitude below 7,515, and the number can be lowered further by not pre-fetching every inter-city edge.

Exhaustion is **explicit**: a typed `CALL_BUDGET_EXHAUSTED` issue on the response, never a silent short result. Flexible dates sample representative dates rather than enumerating every one.

**Question 29 — what stays deterministic offline?** Everything. The synthetic provider remains the default, golden signatures are measured against it, and `pytest` must not touch the network.

## 30–33. Booking foundation and token dependency

`BookingIntent` needs, at minimum: a journey id, the ordered `BookingItem`s with their `ProviderOfferReference`s, a quoted total with currency, an expiry derived from the **earliest** item expiry, a price tolerance, and a state. Multi-ticket orchestration additionally needs per-item outcome recording so that "booked / failed / never attempted" is reconstructible — because partial failure is the normal failure and must never read as `CONFIRMED`.

**Completable without a token:** the adapter, normalization against offline fixtures, money and baggage mapping, call budget, cache TTL, freshness, the probe CLI (which must exit cleanly when unconfigured), the entire booking domain and its state machine, and every offline test.

**Requires a real Test-Mode token:** that Duffel's *actual* JSON matches the fixtures, real latency and rate-limit behaviour, and end-to-end normalization of a live sandbox offer. Until then the live path is reported as **UNVERIFIED** and never as verified-by-skip.

---

## Verdict

**No architectural blocker.** The provider seam, failure taxonomy, retry policy, money spine and baggage semantics are all already the right shape; Duffel is a sibling of an existing adapter.

Two items are genuine new work rather than integration: **bounded acquisition** (§25–29), without which real providers are unusable at any catalog size, and **cache TTL/expiry** (§18–24), without which real offers are served after they die. Both are P0 for V7.5 and neither requires touching `beam_search.py`.

Proceeding into implementation.
