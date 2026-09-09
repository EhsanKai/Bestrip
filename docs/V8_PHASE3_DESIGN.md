# V8 Phase 3 — Design & Contract Audit (Agent 1)

**Read-only audit. No implementation in this document.**
Baseline: Phase 2 working tree (`docs/V8_PROGRESS.md`). Scope: revalidate a
selected itinerary against real Duffel Test Mode before any move toward booking.

---

## The ten questions

### 1. What provider identifier survives normalization?

`detoura.models.provider_reference.ProviderOfferReference`, carried on
`TransportOption.provider_ref`. Fields that matter to revalidation:

| field | source | meaning |
|---|---|---|
| `provider` | `"duffel"` | which system issued the quote |
| `offer_id` | Duffel `offer["id"]` **raw** (`off_0000...`) | the handle to re-fetch |
| `expires_at` | Duffel `offer["expires_at"]` | quote deadline, `None` = unstated |
| `hold_supported` | `not requires_instant_payment` (tri-state) | `None` = Duffel didn't say |
| `hold_until` | `payment_requirements.payment_required_by` | |
| `owner_iata` | `offer["owner"]["iata_code"]` | airline that owns the fare |
| `raw_segments` | tuple of dicts, **opaque by contract** | for a future booking call |

`TransportOption.id` is `f"duffel-{offer_id}"` (prefixed); `provider_ref.offer_id`
is the **unprefixed** Duffel id. Revalidation uses `provider_ref.offer_id`.

Synthetic fares have `provider_ref = None` — they are not revalidatable and never
reach this path.

### 2. Can every selected leg be traced back to a Duffel offer?

**In the engine: yes.** Each `TransportOption` in a snapshot from
`acquire_real_supply` carries `provider_ref.offer_id`.

**Across the API boundary: no, today.** `LegDTO` / `TripRecommendation.legs`
deliberately expose no provider id — `api/v1.py::_selected_itinerary` states
"legs carry no provider id — ids are internal and never reach a client". So a
client cannot send an `offer_id` back because it was never given one.

**Consequence:** Phase 3 needs server-side retention of the search → offer_id
mapping. A stateless "client resends the offer" design is not available without
first breaking the internal-ids stance, which this phase should not do. See
*Selection retention* below.

### 3. What exactly must be re-fetched?

One Duffel **Get Offer** call per distinct `offer_id` in the selected itinerary:
`GET /air/offers/{id}?return_available_services=true`, Duffel-Version `v2`,
bearer test token. One selected trip = 1–4 offers = 1–4 calls, hard-capped.

Not an Offer Request (that is discovery and is what Phase 2 bounds). Not the
cache — revalidation must bypass any snapshot/cache and hit Duffel, the same
rule `services/recheck.py` holds ("Re-checks go to `.inner`").

### 4. What does Duffel return during revalidation?

**Confirmed by a real sandbox probe** (`GET /air/offers/{id}?return_available_services=true`):

```
200  live_mode: False
     total_amount "69.63"  total_currency "EUR"   base 59.01  tax 10.62
     expires_at "2026-09-09T12:24:35.893879Z"     (~30 min out, µs precision, Z)
     payment_requirements {requires_instant_payment: false,
                           payment_required_by, price_guarantee_expires_at}
     available_services: present
     slices[].segments[].passengers[].baggages: [{type:checked,quantity:1},
                                                 {type:carry_on,quantity:1}]
     conditions: {refund_before_departure, change_before_departure}
```

Response is `{"data": {<offer>}}` where `data` **is** the offer. Phase 1's
`assert_test_mode()` works unchanged: it reads `data["live_mode"]` (present,
`False`) and iterates `data.get("offers")` (absent → empty, fine).

**Not-found — confirmed:** a bogus id returns

```
404  {"errors":[{"code":"not_found","type":"invalid_request_error",
      "message":"The resource you are trying to access does not exist."}]}
```

Failure modes → classification:
- **404** → `UNAVAILABLE` (gone; can't distinguish deleted from expired-past-grace, so the honest state is unavailable)
- offer returned with `expires_at` in the past → `EXPIRED`
- **401/403** → `PROVIDER_ERROR` (fail closed, token never logged)
- **429** → `PROVIDER_ERROR` after the existing bounded retry
- **5xx / timeout / non-JSON** → `PROVIDER_ERROR`
- **`live_mode != false`** → `DuffelLiveModeError` → fail closed, never compared

`assert_test_mode()` runs on every Get Offer response before any field is read.

### 5. Which fields can change?

Between discovery and revalidation, per offer:

| field | change → classification input |
|---|---|
| `total_amount` | price delta → tolerance check |
| `total_currency` | currency change → `NOT_BOOKABLE` (never relabel; V7 P3) |
| `expires_at` | now in the past → `EXPIRED` |
| offer existence | 404 → `UNAVAILABLE` |
| `slices[].segments[]` departure/arrival times, flight numbers | itinerary identity change → `USER_RECONFIRMATION_REQUIRED` |
| baggage (`segments[].passengers[].baggages`) | `INCLUDED → UNKNOWN` / `→ EXTRA` / `→ excluded` → terms change, **never** folded into "price changed" |
| `payment_requirements.requires_instant_payment` | `hold → instant` → terms change (reconfirmation); `instant → hold` disclosed, not blocking |
| `available_services` baggage pricing | changed bag fee → terms change |

`owner_iata` change would mean a different fare entirely → treat as identity
change (`USER_RECONFIRMATION_REQUIRED`).

### 6. How is expiry represented?

`ProviderOfferReference.expires_at: datetime | None` and
`freshness_at(now) -> OfferFreshness{FRESH, EXPIRING_SOON (≤300s), EXPIRED, UNKNOWN}`.
`is_expired_at(now)` is `freshness is EXPIRED`. **`None`/`UNKNOWN` is not
expired** but is not bookable-fresh either — Phase 3 treats `UNKNOWN` freshness
on a revalidated offer as `USER_RECONFIRMATION_REQUIRED` (a quote nobody
timestamped has not been shown current). Observed real sandbox `expires_at`:
~30 minutes out, microsecond precision, `Z` suffix.

### 7. How should baggage changes be detected?

Compare the discovered `BaggagePolicy` (cabin/checked/personal `BaggageStatus`)
against the revalidated one, per bag kind. `models/baggage.py` has the vocabulary
(`INCLUDED`, `EXTRA`, `UNKNOWN`, ...). Rules:

- `INCLUDED → anything else` = **downgrade** → always at least
  `USER_RECONFIRMATION_REQUIRED`, regardless of price movement.
- `UNKNOWN → INCLUDED` = improvement, disclosed, not blocking.
- `EXTRA` fee amount change = terms change, disclosed; blocking only if it
  pushes the all-in past tolerance.
- A price that *dropped* because a bag was *removed* must not read as a saving —
  the comparator sees the baggage downgrade first.

### 8. How should instant-payment / hold changes be detected?

`ProviderOfferReference.hold_supported` (tri-state). Detect:
- `True → False` (hold withdrawn, instant payment now required) → terms change → `USER_RECONFIRMATION_REQUIRED`
- `False → True` → disclosed, not blocking
- `True/False → None` (provider stopped saying) → treat as `True → False` (absent is not favourable)

V8 must **not** depend on hold — this is disclosure, not a booking prerequisite.

### 9. What constitutes a material price change?

**Explicit configuration only — no hidden tolerance.** Reuse the shape of
`models/booking.PriceTolerance` (`absolute: float`, `percentage: float`, both
checked; a decrease always passes). Add a `NO_INCREASE` mode (absolute=0,
percentage=0). Classification bands:

| condition | status |
|---|---|
| no change, or decrease, all terms equal | `READY` (was `UNCHANGED`) |
| increase within tolerance, no terms downgrade | `READY_WITH_MINOR_CHANGE` |
| increase beyond tolerance, OR any terms downgrade, OR `UNKNOWN` freshness | `USER_RECONFIRMATION_REQUIRED` |
| currency change, expired, unavailable, provider error, identity change | `NOT_BOOKABLE` |

Example: `403.02 → 404.00` within a `{absolute: 5.0}` tolerance →
`READY_WITH_MINOR_CHANGE`. `403.02 → 469.00` → `USER_RECONFIRMATION_REQUIRED`.
A decrease `403.02 → 390.00` → disclosed in `changes[]`, status `READY`.

### 10. What state transition leads toward booking?

`models/booking.BookingState`: `DRAFT → REVALIDATING → READY → USER_CONFIRMED → BOOKING → CONFIRMED`.
Revalidation is the `REVALIDATING → {READY | PRICE_CHANGED | UNAVAILABLE | EXPIRED | ...}`
edge. A journey may only reach `USER_CONFIRMED` from `READY`, and
`JourneyBookingIntent.with_state(CONFIRMED)` already refuses unless
`can_confirm` (every required item settled) — **the multi-ticket invariant is
already enforced in the domain** (`ticket1 ok + ticket2 ok + ticket3 unavailable`
→ `outcome` is `PARTIAL_FAILURE`/`FAILED`, never `CONFIRMED`).

Phase 3 stops at producing the revalidation verdict + allowing (not performing)
the `READY → USER_CONFIRMED` transition. No Order, no payment.

---

## A real-API constraint the spec did not anticipate: Duffel offers are immutable

`GET /air/offers/{id}` returns the **same** offer it was created with, or 404.
A Duffel offer's `total_amount`, `slices`, and baggage do not drift for a fixed
id — the offer is a frozen snapshot that simply expires (~30 min). Confirmed by
probe: re-fetching a just-searched offer returns byte-identical price and terms.

So against real Duffel sandbox, revalidation-by-id detects exactly:
- **still there** (200, at the price quoted) → `READY`
- **gone** (404) → `UNAVAILABLE`
- **past its stated expiry** → `EXPIRED`

A genuine market re-price does not show up here — it surfaces at Order creation
(Phase 5, Duffel returns `offer_no_longer_available` / a price-change error) or
by re-running discovery. That is a real limitation and it is documented, not
hidden.

**The comparator still handles every drift case** (price up/down, currency,
baggage downgrade, hold withdrawn, time change) because the adversarial tests
drive it with synthesized "current" offers — and because a future revalidation
that re-runs the Offer Request (matching by itinerary key, as `recheck.py`
does) will feed it real movement. The classification logic is where the safety
lives; the transport is swappable.

## Architecture to add (minimal)

```
models/revalidation.py     OfferChange, RevalidatedOffer, OfferRevalidationResult,
                           RevalidationStatus{READY, READY_WITH_MINOR_CHANGE,
                           USER_RECONFIRMATION_REQUIRED, NOT_BOOKABLE} +
                           per-offer {UNCHANGED, PRICE_CHANGED, TERMS_CHANGED,
                           UNAVAILABLE, EXPIRED, PROVIDER_ERROR}

providers/duffel.py        + get_offer(offer_id) -> Duffel offer dict
                             (Get Offer path; assert_test_mode; typed failures;
                             bounded; no cache)

services/revalidation.py   revalidate_selection(offers, *, duffel, tolerance, now)
                           -> OfferRevalidationResult. One get_offer per offer,
                           bounded, parses through the existing adapter, compares
                           discovered vs current, classifies. No snapshot-price
                           fallback ever.

services/offer_comparator.py   compare(discovered, current, *, tolerance)
                               -> list[OfferChange] + per-offer status.
                               Price, currency, times, baggage, hold, expiry.

services/selection_store.py    In-memory, TTL ~20min, bounded. record(offers)
                               -> selection_id. get(selection_id). Populated by
                               the real search path; read by /revalidate.

api/v1.py                  POST /api/v1/trips/revalidate {selection_id, tolerance?}
                           -> revalidation DTO. Server owns price/baggage/status.
api/contracts.py           RevalidateRequest, RevalidateResponse, OfferChangeDTO
```

### Selection retention

`/api/v1/search` gains an optional `live: bool = false`. `live=false` is byte-for-byte
the current synthetic path (**regression guarantee**). `live=true` runs
`acquire_real_supply` + planner, and records `{selection_id -> [SelectedOffer(
offer_id, discovered_total, discovered_currency, discovered_baggage,
discovered_expires_at, ...)]}` per returned recommendation, returning
`selection_id` on each. `/revalidate` takes only `selection_id` — the client
never resends authoritative provider data.

For test paths, `selection_store.record()` is callable directly.

### Security (Agent 6 preconditions, checked here)

- token: only in `DuffelTransportProvider._token`, never in DTOs, logs, or errors
  (Phase 1 `redact()` + `DuffelLiveModeError` carry none). Get Offer inherits this.
- `live_mode`: `assert_test_mode()` on every Get Offer response.
- URL: `offer_id` is path-segment-substituted into a fixed host+path; validate it
  matches `^off_[A-Za-z0-9]+$` before building the URL — no user-controlled host.
- bounded: ≤ N (= leg count, hard cap ~6) Get Offer calls per revalidate; reuse
  `RetryingHttpClient` bounds; explicit timeout.
- `/revalidate` cannot be a Duffel query amplifier: it only re-fetches offer_ids
  the server itself recorded for a `selection_id` it issued — not arbitrary ids.
- client-supplied price/baggage/status: not trusted. `discovered_*` values come
  from the server's own selection record, not the request.

## Regression guarantees

- No revalidation call inside beam search. `revalidate_*` is only reachable from
  the new endpoint / service, post-selection. **Network calls during beam = 0**,
  unchanged.
- `live=false` search path untouched → identical routes/prices/scores/state counts.
- New `RevalidationStatus` / per-offer enums are additive; `BookingState` and
  `PriceTolerance` unchanged.
