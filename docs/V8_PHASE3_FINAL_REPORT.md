# V8 Phase 3 — Final Report

Real offer revalidation, price tolerance & booking safety.

| | |
|---|---|
| Baseline HEAD | `7f53ddc` (V8 Phase 2) |
| Final HEAD | `11803d0` |
| Push status | **not pushed** — awaiting go-ahead |
| Remote parity | n/a (unpushed) |

## 1. Files changed

**Modified**
- `src/detoura/providers/duffel.py` — `get_offer` / `revalidate_offer`, `OFFER_ID_RE`, `DuffelOfferGone`, `OFFER_PATH`
- `src/detoura/models/provider_reference.py` — `quoted_amount`, `quoted_currency` (both optional)
- `src/detoura/api/contracts.py` — `RevalidateRequest`, `RevalidateResponse`, `OfferChangeDTO`, `RevalidatedOfferDTO`, `REVALIDATION_MESSAGES`
- `src/detoura/api/v1.py` — `POST /api/v1/trips/revalidate`, `_revalidation_duffel()`
- `docs/V8_PROGRESS.md`

**New**
- `src/detoura/models/revalidation.py`
- `src/detoura/services/selection_store.py`
- `src/detoura/services/offer_comparator.py`
- `src/detoura/services/revalidation.py`
- `src/detoura/services/live_search.py`
- `tests/test_v8_revalidation.py`
- `docs/V8_PHASE3_DESIGN.md`

**Frozen frontend:** untouched. **Unrelated files** (`.codex/`, `AGENTS.md`, `CLAUDE.md`, `frontend/*`): untouched, unstaged.

## 2. Architecture added

```
Search (real)                        POST /api/v1/trips/revalidate
  live_search()                          │  {selection_id, tolerance?}
   ├─ acquire_real_supply() [Phase 2]    ▼
   ├─ TravelPlanner (unchanged ranking)  selection_store.get(id)  ── 404 if unknown/expired
   └─ selection_store.record()  ────────▶ revalidate_selection()
        {sel_id → SelectedOffer[]}          ├─ per offer: duffel.get_offer(offer_id)  (≤8, bounded)
                                            │     ├─ 404/410 → UNAVAILABLE
                                            │     ├─ assert_test_mode fail → PROVIDER_ERROR
                                            │     ├─ past expiry → EXPIRED
                                            │     └─ compare_offer(discovered, current, tolerance)
                                            └─ aggregate → RevalidationStatus + bookable
```

The server holds the discovered price/terms. The client sends only an opaque
`selection_id`. No client-supplied price, baggage, or status is trusted anywhere.

## 3. Sandbox calls made

- Design probe: 1 Offer Request + 1 Get Offer (real id) + 1 Get Offer (bogus id → 404 confirmed).
- Revalidation demo: 1 live search (48 Offer Requests) + 3 Get Offer.
- All read-only. **No Order, payment, or ticketing path exists in Phase 3 code.**

## 4. Exact revalidation behavior

Per offer, one Get Offer call (never cached, bypasses the snapshot):

| Provider result | Offer status | Contributes |
|---|---|---|
| 200, within noise, terms equal | `UNCHANGED` | — |
| 200, price moved (any direction) | `PRICE_CHANGED` | tolerance check |
| 200, baggage / hold / currency / time changed materially | `TERMS_CHANGED` | blocking |
| 200, `expires_at` in the past | `EXPIRED` | not bookable |
| 404 / 410 | `UNAVAILABLE` | not bookable |
| `live_mode≠false`, 401/403/5xx/429/timeout/malformed | `PROVIDER_ERROR` | not bookable |

Itinerary verdict, in order: any required offer not bookable **or** any currency
change → `NOT_BOOKABLE`. Else any BLOCKING change → `USER_RECONFIRMATION_REQUIRED`.
Else any MINOR change → `READY_WITH_MINOR_CHANGE`. Else → `READY`.

`current_total` = discovered trip total + revalidated flight movement
(accommodation/transfers carried forward — Phase 3 re-prices flights only).
`None` when any offer could not be re-priced — never a partial sum.

## 5. Price tolerance semantics

Explicit configuration, no hidden default. `PriceTolerance(absolute, percentage)`
— both bounds apply; a decrease always passes. Presets: `NO_INCREASE` (0/0),
`absolute_eur(n)`, `percentage(p)`. The endpoint takes `tolerance_absolute` /
`tolerance_percentage`; unset → `NO_INCREASE`.

- `403.02 → 404.00`, tol `absolute_eur(5)` → `READY_WITH_MINOR_CHANGE`
- `403.02 → 469.00`, tol `absolute_eur(5)` → `USER_RECONFIRMATION_REQUIRED`
- `403.02 → 390.00` → `READY`, decrease disclosed in `changes[]`

## 6. Baggage / terms comparison

- `INCLUDED → UNKNOWN` / `→ EXTRA` / `→ excluded` = downgrade → **BLOCKING**, regardless of price movement. A price drop caused by a baggage removal is never shown as a saving.
- `UNKNOWN → INCLUDED`, hold `not-supported → supported` = **INFO**, disclosed, not blocking.
- Hold `supported → not-supported/unknown` (instant payment now required) = **BLOCKING**.
- Currency change = **BLOCKING**, `NOT_BOOKABLE`, and explicitly not counted as a price delta.
- Departure/arrival instant change vs the selected times = **BLOCKING**.
- Re-fetched quote with no `expires_at` = `UNKNOWN` freshness = **BLOCKING** (not shown to be current).

## 7. Security findings

All Agent 6 checks pass — see `docs/V8_PROGRESS.md` § *Security review*. Summary:
token never leaves the server (zero log/print in Phase 3 code; not in any DTO;
not in errors — tested); live-token and `live_mode=true` refused; `offer_id`
matched to `^off_[A-Za-z0-9]+$` before any URL is built; timeout 12s, 2 retries,
`max_calls=16`, `MAX_OFFERS_PER_REVALIDATION=8`; the endpoint re-fetches only
ids the server itself recorded for a server-issued `selection_id`.

**Limitations disclosed:** per-request rate limiting only (no global concurrent
revalidation budget); no endpoint auth (the app has none — stateless product);
a genuine market re-price is not detectable by a fixed Duffel offer id.

## 8. Agent 5 verdict — **APPROVED**

`tests/test_v8_revalidation.py` is the independent adversarial pass — 34 tests,
each attacking a specific claim. All of the spec's required attacks are covered
and pass: expired, deleted, price up/down, tiny/large, currency change,
baggage `INCLUDED→UNKNOWN` / `→ not-available`, hold→instant, provider
timeout/429/5xx, malformed response, `off_` id URL-injection, fake `live_mode`,
token leakage, multi-city partial revalidation, and the multi-ticket invariant
(2 valid + 1 unavailable → `NOT_BOOKABLE`, never ready). Serialization honesty:
an unpriced total serializes as `null`, not `0`.

## 9. Agent 6 verdict — **APPROVED**

Fail-closed secret scan of staged files: 0 Duffel tokens, 0 authorization
headers, 0 passwords/keys, 0 PII/payment fixtures. Every staged path classified
and V8-Phase-3-required. Selective staging only — no `git add .`.

## 10. Targeted test count

- Spec regression set (V7 comparison/reopt/baggage, V7.5 provider/adversarial/booking, V7.6, V8 P1/P2/P3): **276 passed**
- Broader (api, end_to_end, v5, comparison/baggage/reopt adversarial, recheck): **247 passed, 1 skipped**
- `tests/test_v8_revalidation.py`: **34 passed**

## 11. Full-suite count

**1234 passed, 23 skipped, 0 failed** (668s on a loaded host). No unresolved
XPASS/XFAIL.

## 12. Regressions

None. Golden search signatures unchanged. `live=false`/synthetic `/api/v1/search`
byte-identical. Beam search network calls: still **0** by construction and test.

## 13. Known limitations

- Real market re-price not detected via a fixed offer id (Duffel offers are
  immutable) — surfaces at Order creation (Phase 5) or re-discovery.
- Cross-currency **UNVERIFIED** against real sandbox — every offer observed was EUR.
- `live_search` is not yet wired to an endpoint; `/api/v1/search` remains
  synthetic. Selections are created via the service (used by the demo and tests).
- No concurrent-request revalidation budget; no endpoint auth.

## 14. Commit hash

`11803d0` (Phase 3). Chain: `e70c0ee` → `224446f` (P1) → `7f53ddc` (P2) → `11803d0` (P3).

## 15. Push status

Not pushed.

## 16. Remote parity

n/a until pushed.

## Real sandbox example

```
DISCOVERED   CGN → Prague → Vienna → CGN        EUR 404.76
             off_...BAET5ORWYW2fov6bXY  CGN→Prague  EUR 97.10  cabin/checked included
             off_...BAEVGCPh3eP72KRODK  Prague→Vienna EUR 80.18  cabin/checked included
             off_...BAEVJwZgeSbDdkzPqi  Vienna→CGN   EUR 109.98 cabin/checked included

WAIT / REVALIDATE   3 × Get Offer, 0.9s

CURRENT      EUR 404.76
DELTA        EUR +0.00   (0.0%)
STATUS       READY   ·   bookable=true   ·   may_proceed=true
             CGN→Prague: INFO note "valid → expiring soon" (30-min window nearly
             elapsed during the 100s acquisition; disclosed, not blocking)
```

No price change was manufactured — Duffel produced none.
