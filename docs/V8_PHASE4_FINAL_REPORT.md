# V8 Phase 4 — Final Report

Complete booking experience · traveller flow · Detoura Test Travel Pass.
**No real payment anywhere.**

| | |
|---|---|
| Baseline HEAD | `4a1b482` (V8 Phase 3 report) |
| Final HEAD | `bd4b009` |
| Push status | **not pushed** |
| Remote parity | n/a (unpushed) |

---

## 1. Files changed

**Backend — new**
`src/detoura/models/traveler.py` · `src/detoura/models/travel_pass.py` ·
`src/detoura/services/journey_reference.py` ·
`src/detoura/services/booking_orchestrator.py` ·
`src/detoura/services/booking_flow.py` · `tests/test_v8_booking.py`

**Backend — modified**
`src/detoura/providers/duffel.py` (`create_test_order`, `duffel_passengers_from`,
`DuffelOrderError`, `ORDER_PATH`) ·
`src/detoura/api/contracts.py` (booking DTOs) ·
`src/detoura/api/v1.py` (5 booking endpoints)

**Frontend — new**
`frontend/src/components/booking/TravelPass.{tsx,css}` ·
`frontend/src/screens/BookingExperience.{tsx,css}` (was a 21-line untracked
scaffold; now a 594-line real flow)

**Frontend — modified**
`frontend/src/api/types.ts`, `frontend/src/api/client.ts`,
`frontend/src/App.tsx`, `frontend/src/screens/TripDetail.tsx`

**Also brought under version control** (separately-supplied frontend, needed for
a clean-checkout build): `JourneyPoster.*`, `CityIllustration.*`,
`cityLandmarks.ts`, `illustrationVariant.ts`, `Results.*`, `RecommendationCard.*`,
`base.css`, the two `frontend/*.md` handoff docs.

**Untouched:** `.codex/`, `AGENTS.md`, `CLAUDE.md`, `frontend.zip`.

## 2. Architecture added

```
Choose this journey
      │
POST /booking-intents  {selection_id}  → SANDBOX_BOOKED (real Duffel offers)
                       {demo_legs...}   → DEMO_ONLY (synthetic trip, no Order)
      │  BookingStore (in-memory, TTL 1h)   journey_reference = DTR-V8-XXXXXX
      ▼
POST /booking-intents/{id}/travelers   TravelerParty (validated, size = trip)
      ▼
POST /booking-intents/{id}/confirm     ONE confirmation → spawns run thread
      │
run_booking():  all → REVALIDATING
                per leg: revalidate_offer (real) / carry (demo)
                all → READY   (or FAILED → journey FAILED, nothing ordered)
                tolerance breach → RECONFIRM_REQUIRED (poll shows discovered/current/delta)
                per leg: USER_CONFIRMED → BOOKING → create_test_order (SANDBOX)
                         → CONFIRMED (+ ord_ id)  |  FAILED → STOP
                outcome = JourneyBookingIntent.outcome  (derived)
      ▼
GET /booking-intents/{id}          poll: phase + per-leg BookingItem state
GET /booking-intents/{id}/travel-pass   409 until terminal; then DetouraTravelPass
```

## 3. Sandbox calls made

- Live search for the SANDBOX demo: 48 Offer Requests.
- Per-leg revalidation: 3 Get Offer.
- **Per-leg Order creation: 3 `POST /air/orders` (Duffel Test Mode).** Payment
  `type: "balance"` — the test account balance, no card, no real money. Real
  Order objects `ord_0000BAEfWrpOAsDFCizCwv`, `…Wzjni9LEEjxiLO`, `…X7PXBNsq2CXdrX`.

## 4. Exact booking behavior

| Step | SANDBOX_BOOKED | DEMO_ONLY |
|---|---|---|
| revalidate | real `get_offer` per leg; gone → `FAILED`; expired → `FAILED` | carry the discovered price |
| tolerance | current total vs discovered vs `PriceTolerance`; breach → `RECONFIRM_REQUIRED` | (no drift) |
| issue | `create_test_order` per leg → `CONFIRMED` + `ord_` id; refusal → `FAILED` | mark `CONFIRMED`, no Order |
| first failed **required** leg | run stops; remaining legs `NOT_ATTEMPTED` | same |
| outcome | `JourneyBookingIntent.outcome`: all settled → `CONFIRMED`; some → `PARTIAL_FAILURE`; none → `FAILED` | same |
| pass | `CONFIRMED` → `ready`; `PARTIAL_FAILURE` → `recovery_required`; else `failed` | same |

`create_test_order` refuses unless: test token **and** a fresh `get_offer`
proves `live_mode is False` **and** the offer's current total still equals the
confirmed figure. The created Order response is itself checked for
`live_mode is False`.

## 5. Price tolerance semantics

Endpoint `confirm` takes `tolerance_absolute` (default €25) and
`tolerance_percentage` (default 0). Both bounds apply; a decrease always passes.
On a breach the run parks at `RECONFIRM_REQUIRED` and the poll response carries
`discovered_total`, `current_total`, `reconfirm_note` — the UI shows
*Price when selected / Current price / Difference* and requires a second
confirm. Nothing is booked until the traveller reconfirms.

## 6. Baggage / terms behavior

Per-leg `cabin_baggage` / `checked_baggage` carried from discovery onto every
ticket and the pass. Unknown checked baggage is surfaced on the Review screen
("Detoura will not claim a price it cannot stand behind") and listed in the
pass's `unknowns`. Revalidation reuses the Phase 3 comparator — a baggage
downgrade blocks issuance the same way a tolerance breach does.

## 7. Security findings

| check | result |
|---|---|
| `DUFFEL_ACCESS_TOKEN` to the browser | never — read only from env in `_duffel_for_booking()` / `_revalidation_duffel()` |
| token in a DTO / response / log / error | none — booking services have zero `log`/`print`; `test_the_token_never_appears_in_a_revalidation_error` + booking token-leak test |
| client-supplied price / status / order id | not in `CreateBookingIntentRequest` or `ConfirmBookingRequest`; `discovered_*` and the journey reference are server-set |
| traveller PII | validated; never logged; never in a URL (`test_traveler_pii_never_appears_in_an_error_or_the_url`); not in `localStorage` (drafts live in React state only); sent once in a POST body |
| live token / `live_mode=true` | `is_test_token` gate; `assert_test_mode` on every `get_offer` and on the created Order |
| arbitrary Duffel URL | host fixed; `offer_id` matched to `^off_[A-Za-z0-9]+$` before any URL is built |
| bounded | `max_calls` on the provider; `MAX_OFFERS_PER_REVALIDATION=8`; one Order per leg; 2 retries; 15s timeout |
| double-confirm | `start_confirmation` refuses unless phase is `AWAITING_CONFIRMATION` / `RECONFIRM_REQUIRED` |

## 8. Agent 5 verdict — **APPROVED**

`tests/test_v8_booking.py` is the adversarial pass — 20 tests. The multi-ticket
invariant holds: `test_two_confirmed_and_one_refused_is_never_a_success`
(journey `PARTIAL_FAILURE`, pass `RECOVERY_REQUIRED`, `tickets_prepared == 2`,
never READY), `test_the_run_stops_after_the_first_failed_required_leg`
(`[CONFIRMED, FAILED, NOT_ATTEMPTED]`),
`test_a_gone_offer_at_revalidation_fails_before_any_order` (no Order created for
any leg). `test_the_pass_changes_with_the_traveler_and_the_route` proves the
pass is not a fixture. `create_test_order` guards: non-test token refused, price
drift refused.

## 9. Agent 6 verdict — **APPROVED**

Fail-closed secret scan of the 32 staged files: 0 Duffel tokens, 0
authorization headers, 0 passwords/keys, 0 payment fixtures. Selective staging
(explicit paths; `.codex/`, `AGENTS.md`, `CLAUDE.md`, `frontend.zip` excluded).
`tsc -b` clean; `vite build` clean (272 KB JS / 82 KB gzip).

## 10. Targeted test count

**530 passed, 1 skipped, 0 failed** — the spec's regression list (V7 comparison
/ reoptimization / baggage, V7.5 provider / adversarial / booking, V7.6) + all
V8 phases + `test_api` / `test_end_to_end` / `test_v5_product` /
`test_v6_recheck` / comparison & baggage adversarial / `test_v65_request_limits`.
`tests/test_v8_booking.py`: **20 passed**.

## 11. Full-suite count

Phase 3 baseline: 1234 passed, 23 skipped. Phase 4 full run: *(in progress — the
host runs pytest ~5× slower than at session start; the targeted 530 covers every
module Phase 4 touches — the untouched remainder has no dependency on these
changes)*.

## 12. Regressions

None in the 530 targeted tests. Search ranking unchanged (golden signatures
green). `/api/v1/search` unchanged. Beam search network calls: still **0**.

## 13. Known limitations

- **`live_search` not wired to `/api/v1/search`.** The **UI booking flow runs in
  `DEMO_ONLY`** — a real data-driven pass, but no Duffel Order. The
  `SANDBOX_BOOKED` path (real Orders) is proven by `test_v8_booking.py` and a
  live script, and the `/booking-intents` endpoint accepts `selection_id`.
  Wiring the real search into the UI (behind a flag) is the one step left for a
  fully manual sandbox-order run.
- Idempotency is a phase guard, not a stored key.
- `RECOVERY_REQUIRED` discloses and stops; no automated recovery action.
- Cross-currency still **UNVERIFIED** (all sandbox offers EUR).
- DEMO_ONLY trusts the search price the user already saw (no provider truth
  exists in that mode); the pass is loudly labelled DEMO ONLY / no payment.

## 14. Commit hash

`bd4b009`. Chain: `e70c0ee` → `224446f` → `7f53ddc` → `11803d0` → `4a1b482` → `bd4b009`.

## 15. Push status — not pushed.

## 16. Remote parity — n/a until pushed.

---

## Complete flow capture (through the running container)

```
### 1. SEARCH        Köln, 5 days, 2 travellers, €900, prefer Berlin
    5 recommendations; top: CGN → Berlin → Munich → CGN   €546.76

### 2. CHOOSE THIS JOURNEY
    booking_id=bk_Un76FeCQRK8LO4TkDwAt  ref=DTR-V8-6EY6A5  mode=demo_only
    route=['Cologne', 'Berlin', 'Munich', 'Cologne']

### 3. TRAVELER DETAILS  (×2)
    Arman Delacroix + Mira Delacroix   phase=awaiting_confirmation

### 4. REVIEW
    Ticket 1  CGN → Berlin   2026-09-10 13:10   €33.44
    Ticket 2  Berlin → Munich 2026-09-14 06:40  €38.99
    Ticket 3  Munich → CGN    2026-09-15 07:30  €36.71
    Trip total €546.76 · Travellers 2 · Payment: not required in this test version

### 5. CONFIRM JOURNEY   (one confirmation)   phase=revalidating

### 6. TICKET ISSUANCE PROGRESS   (polled)
    [revalidating] Cologne→Berlin:READY | Berlin→Munich:READY | Munich→Cologne:READY
    [issuing]      Cologne→Berlin:BOOKING | ...
    [issuing]      Cologne→Berlin:CONFIRMED | Berlin→Munich:BOOKING | ...
    [issuing]      ... | Munich→Cologne:USER_CONFIRMED
    [complete]     Cologne→Berlin:CONFIRMED | Berlin→Munich:CONFIRMED | Munich→Cologne:CONFIRMED

### 7. TEST TRAVEL PASS
    Your journey is ready   ·   status=ready   mode=demo_only
    Route:     Cologne → Berlin → Munich → Cologne
    Traveller: Arman Delacroix (+1 more)
    Reference: DTR-V8-6EY6A5   Dates: 2026-09-10, 2026-09-14, 2026-09-15
    3/3 prepared   Total €546.76
      ✓ Cologne → Berlin   CGN    2026-09-10 13:10   [CONFIRMED]
      ✓ Berlin → Munich    Berlin 2026-09-14 06:40   [CONFIRMED]
      ✓ Munich → Cologne   Munich 2026-09-15 07:30   [CONFIRMED]
    Duffel orders: (none — DEMO ONLY)
    DEMO ONLY — no Duffel Order was created. No payment collected.
    Not a valid boarding pass · No payment collected
```

### Real Duffel Test Mode Order run (via `test_v8_booking.py` + live script)

```
booking bk_bu5mbV3vnUBEJ9DWYMbO   ref DTR-V8-YR44VQ   mode sandbox_booked
phases: revalidating → issuing → complete
  Cologne → Prague:  CONFIRMED   ord_0000BAEfWrpOAsDFCizCwv
  Prague → Vienna:   CONFIRMED   ord_0000BAEfWzjni9LEEjxiLO
  Vienna → Cologne:  CONFIRMED   ord_0000BAEfX7PXBNsq2CXdrX
PASS  ready / sandbox_booked   3/3   €250.17
  payment against the Duffel test balance — no card, no real money
```

### Partial failure (fake provider, leg 3 refused)

```
journey → PARTIAL_FAILURE     legs [CONFIRMED, CONFIRMED, FAILED]
pass    → recovery_required   "Detoura stopped the remaining booking process.
                               This journey is NOT booked."   (never READY)
```
