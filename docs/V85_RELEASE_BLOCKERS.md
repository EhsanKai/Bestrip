# V8.5 Release-Blocker Closure

Three consumer-facing correctness defects found in manual acceptance. All three
are fixed, regression-tested (18-point matrix), and accepted in a real browser
against a `--no-cache` container at 1280 px and 390 px.

- **B1 — multi-traveller booking was single-traveller** → fixed
- **B2 — optimizer whole-trip estimate was charged as flight fare** → fixed
- **B3 — Edit / Re-optimize journey was hidden for single-stop trips** → fixed

---

## F. Price provenance report

### The three prices, and which one the customer pays

| Price | What it is | Model / field |
|---|---|---|
| **A. Whole-trip estimate** | Optimizer's guess for the *entire* trip incl. hotels and airport transfers that Detoura is **not** booking | `TripRecommendation.total_price` → `CreateBookingIntentRequest.demo_trip_estimate` → `BookingIntentResponse.trip_estimate` (display only) |
| **B. Bookable transport subtotal** | Sum of the actual per-leg fares × party size — the only supply Detoura actually books | `BookingRun.discovered_total` = Σ `ItemProgress.quoted_price` × `travelers` → `PriceBreakdown.supplier_transport` = `PriceBreakdown.bookable_ticket_subtotal` |
| **C. Customer payable** | B + Detoura fee − promo + tax | `PriceBreakdown.customer_total` |

The customer is charged **C, derived from B**. A never enters the payable amount.

### The observed defect — single traveller, CGN→CDG→BCN→CGN, ALL-IN-ONE

| Amount seen in acceptance | Where it came from (old pipeline) |
|---|---|
| **€575.00 / €574.52** | Optimizer whole-trip estimate. `TripRecommendation.total_price` (= `PricedItinerary.total`, accommodation + transfer + transport) was sent by the frontend as `CreateBookingIntentRequest.demo_total` and stored verbatim as `BookingRun.discovered_total`. |
| **€119.56** | The *correct* bookable transport for one traveller: Σ leg `price_per_person` = 24.20 + 42.00 + 53.36. This is what `supplier_transport` should have been. |
| **€34.73** | Detoura ALL-IN-ONE fee computed on the **wrong** €574.52 base: `DynamicMarkupPolicy` → 574.52 × 5% = 28.73 markup + €6.00 service fee = `PriceBreakdown.detoura_revenue_gross`. |
| **€609.25** | Old `PriceBreakdown.customer_total` = `supplier_total` 574.52 + `detoura_revenue_gross` 34.73. The customer was about to pay **€609.25** for **€119.56** of bookable tickets — a **€489.69** overcharge, the entire hotel + transfer estimate silently relabelled "Flights". |

**Field-level trace of the old pipeline**

```
frontend ensureIntent:      demo_total = trip.total_price            (= optimizer PricedItinerary.total)
contracts.py:               CreateBookingIntentRequest.demo_total
booking_flow.create_run_demo: BookingRun.discovered_total = demo_total
booking_commercial._supplier_transport(run): return run.discovered_total
CommercialPricingService.quote(supplier_transport=574.52)
DynamicMarkupPolicy:        PriceBreakdown.detoura_markup = 28.73, .detoura_service_fee = 6.00
PriceBreakdown.customer_total = 574.52 + 34.73 = 609.25
```

### Corrected numbers — same journey, verified against the rebuilt container

`POST /api/v1/booking-intents` `demo_travelers: 1`, `service_tier: ALL_IN_ONE`,
`demo_trip_estimate.total: 575.0`, `demo_trip_estimate.accommodation: 400.0`,
`demo_trip_estimate.transfer: 55.44`:

| Line | Amount | Source field |
|---|---|---|
| Optimizer whole-trip estimate | €575.00 | `trip_estimate.total` — display only, never priced |
| — optimizer accommodation | €400.00 | `trip_estimate.accommodation` — **not charged** |
| — optimizer transfers | €55.44 | `trip_estimate.transfer` — **not charged** |
| — optimizer transport | €119.56 | `trip_estimate.transport` |
| **Bookable transport subtotal** | **€119.56** | `commercial.breakdown.supplier_transport` = `bookable_ticket_subtotal` = `discovered_total` = Σ `ItemProgress.quoted_price` (24.20 + 42.00 + 53.36) × 1 |
| Detoura service fee | €6.00 | `commercial.breakdown.detoura_service_fee` |
| Detoura markup (5% × 119.56) | €5.98 | `commercial.breakdown.detoura_markup` |
| Detoura fee total | €11.98 | `commercial.breakdown.detoura_revenue_gross` |
| Promo | €0.00 | `commercial.breakdown.discount` |
| Tax | €0.00 | `commercial.breakdown.tax` |
| **Final payable** | **€131.54** | `commercial.breakdown.customer_total` = 119.56 + 11.98 |
| Reconciliation | `ok` | `commercial.breakdown.reconciled: true`, `price_reconciled: true` |

Explanation string returned verbatim by the engine:
`["policy detoura.markup v2", "matched: All-in-One", "base 119.56 x 0.050 = 5.98 markup, + 6.00 service fee"]`

### Reconciliation guard (`src/detoura/models/price_provenance.py`)

`reconcile(currency, legs, priced_supplier_transport, trip_estimate)` recomputes
Σ leg fares independently and compares to the priced `supplier_transport`:

- tolerance = `max(0.50, round_half_up(0.01 × bookable_ticket_subtotal))`
- if the priced base instead equals the **whole-trip estimate** (accommodation
  > 0 and `|priced − trip_estimate.total| ≤ 0.50` while `|priced − bookable| >
  tolerance`) → `PriceReconciliation.ok = False`, reason *"the priced supplier
  transport equals the whole-trip estimate, which includes accommodation Detoura
  is not booking"*.
- `confirm_booking` → if `not rec.ok`: `BookingPhase.PRICE_INCONSISTENT`, **HTTP
  409, nothing booked, no charge**. Verified by
  `test_14_reconciliation_mismatch_blocks_confirmation`.

---

## Root causes and fixes

### B1 — multi-traveller

- **Cause:** `BookingExperience.tsx` had `const partySize = 1;` hard-coded; the
  traveller step always rendered one form; `submit_travelers` accepted any count.
- **Fix:** `partySize = Math.max(1, Math.round(travelers))` from
  `search.request.travelers` (via `App.tsx`). `TravelerStep` maps
  `partySize` drafts, each its own `.booking__traveller` block with its own
  `.booking__doc` document section. `submit_travelers` now requires exactly
  `requested_travelers` records (422 otherwise), rejects a shared
  `passport_number` across travellers (422), and never copies traveller 1 to
  fill the party. `Traveler.safe_summary()` omits number/expiry/DOB.

### B2 — price truth

- **Cause:** above — optimizer estimate `→ demo_total → discovered_total →
  supplier_transport`.
- **Fix:** `demo_total` dropped from the contract. `discovered_total` /
  `supplier_transport` are recomputed from leg `price_per_person` × party.
  `ItemProgress.quoted_price` / `current_price` normalised to per-person
  everywhere. New `price_provenance` model + `reconcile_run_price(run)` +
  `PRICE_INCONSISTENT` phase. Consumer UI: header + cards say **"Estimated trip
  cost"**; review shows **"Tickets (N travellers)"** and **"Pay now"**, with
  accommodation/transfers in a separate *"Not booked by Detoura — estimated, you
  arrange and pay separately"* block.

### B3 — edit visibility

- **Cause:** `TripDetail.tsx` `canEdit` required `trip.cities.length >= 2`; most
  results are single-stop, so the Edit CTA never rendered.
- **Fix:** `canEdit` requires `>= 1` city. `JourneyEditor` disables **Remove**
  when `cities.length === 1` (would empty the trip) with a visible explanation
  and a `title`; **Replace** stays available and re-optimizes.

---

## G. Verdict

| Gate | Result |
|---|---|
| 18-point regression matrix (`tests/test_v85_release_blockers.py`) | **17 items pass** (cases 1–3 parametrised ×3) |
| Full backend suite | see checkpoint commit note |
| Docker `--no-cache` build | **succeeds** (`detoura:v85-blockers`) |
| Browser acceptance @ 1280 px | **pass** — Estimated trip cost label, Edit visible, Keep/Remove/re-optimize/accept, 3 distinct traveller forms, reconciled review |
| Browser acceptance @ 390 px | **pass** — same flow |
| 3-traveller form acceptance | **pass** — "Traveller 1/2/3 of 3", 3 doc sections, 3 distinct passports |
| Pricing reconciliation in UI + server | **pass** — Pay now €386.13 = Tickets €374.88 (Σ leg fares × 3) + fee €11.25; estimate block separate |
| Page errors | **none** |
| Failed API calls | **none** |
| Sensitive document data in analytics / logs | **none** — beacon bodies and `/api/v1/ops/analytics/events` carry only whitelisted keys (`rank`, `result_count`, `search_mode`, `profile`, `repeat`) |

Duffel remains **TEST MODE**. Not pushed.
