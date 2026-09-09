# V8 progress log

Running checkpoint for the V8 backend/Duffel/security release. One section per
completed phase; the tail carries the resume point.

---

# V8 Phase 1 — safety + TLS. Checkpoint.

**Baseline:** `e70c0ee` (V7.6). **Nothing staged, nothing committed, nothing pushed.**
**Prereq for everything after:** this phase closes the two blockers the sandbox
probe checkpoint (`docs/V8_DUFFEL_SANDBOX_CHECKPOINT.md`) flagged.

## What changed

| Concern | Change | File |
|---|---|---|
| `live_mode` guard | `assert_test_mode(body)` + `DuffelLiveModeError`. Called in `fetch_offers` **after** JSON parse, **before** `parse_offers`. Envelope `data.live_mode` must be exactly `False`; missing / `True` / non-boolean, or any offer asserting a truthy `live_mode`, fails closed with a message that carries no offer payload and no token. | `providers/duffel.py` |
| Truncation disclosure | Instance counters `offers_received` / `offers_retained` / `offers_truncated`; `supply_metrics()`. Offers mapped cleanly but dropped by the `max_offers` cap are counted and `log.info`-logged per route. The probe's 23→20 is no longer silent. | `providers/duffel.py` |
| Portable TLS trust | `_build_ssl_context()`: env override (`SSL_CERT_FILE` / `SSL_CERT_DIR`) → `certifi` bundle → interpreter default. Built once per `UrllibHttpClient`, passed to `urlopen(context=...)`. **Verification is never disabled** — no `CERT_NONE`, no `_create_unverified_context`, no `verify=False` anywhere in the module (asserted by a test). | `providers/http.py` |
| certifi dependency | Added to core `dependencies` (pure data, no transitive deps). | `pyproject.toml` |
| Probe output | States `live_mode=false confirmed` and prints `supply_metrics()`. | `tools/duffel_probe.py` |
| Fixtures | `response()` / `_offer()` default `live_mode=False`; `LIVE_MODE_ENVELOPE` (true), `LIVE_MODE_MISSING` (absent), `LIVE_MODE_MIXED_OFFERS` (contaminated offer) added. `parse_offers` unit tests unaffected — the guard lives on the network path only. | `tests/duffel_fixtures.py` |

## Verification

**Real Duffel Test Mode probe** (`python -m detoura.tools.duffel_probe --origin CGN --destination BCN --date 2026-10-15`), token from `~/.config/detoura/secrets.env`:

```
live_mode=false confirmed on the Duffel response envelope.
supply: 23 received, 20 retained, 3 dropped to max_offers=20, 0 unusable
```

- TLS handshake to `api.duffel.com` succeeded **with no `SSL_CERT_FILE`** — the certifi path works. The probe checkpoint's "any environment whose trust store is unconfigured will fail every Duffel call" blocker is closed.
- Real response envelope carried `live_mode: false`; `assert_test_mode` passed it.
- 23 offers / 20 normalized / 0 dropped — matches the probe checkpoint exactly; the 3-offer truncation is now recorded.

**Tests:** `tests/test_v8_safety.py` — 18, all pass. Targeted V7.5/V7.6 suites — pass. Full suite — exit 0, no regressions (runs are environmentally slow on this host; two independent clean runs observed).

## Not done (V8 remaining)

Phase 2 bounded real supply — **blocked on a design gap**: destination cities in
`data/destinations.py` carry no IATA codes, and the synthetic transport graph
keys on city *names* ("Berlin"). A real Duffel `fetch_offers` needs airport
codes, so V8 needs a city→primary-airport table before a real trip search
(not just the CGN→BCN probe) can run. Then: Duffel-backed acquisition service
(cache + rate-limit wrap), truncation → `OfferSnapshot` `SnapshotIssue`,
supply-diagnostics contract, real QUICK/SMART ramp 5→10→25→50.

Phases 3–5 (revalidation + price tolerance, traveler PII model, sandbox Order
creation + multi-ticket orchestration + partial-failure recovery + idempotency),
security hardening, Docker, frontend integration adapters, `V8_FINAL_REPORT.md`
— all untouched.

---

# V8 Phase 2 — bounded real supply. Checkpoint.

**Design gap resolved:** `Destination.primary_airport` (IATA, `str | None`) added
to the model; all 16 catalog cities given their real primary airport
(LHR/BRU/CDG/AMS/PRG/VIE/MAD/BCN/MXP/FCO/DUB/CPH/BUD/BER/MUC/ZRH) — synthetic
catalog, real codes, because a made-up code just earns a 422.

## What changed

| Change | File |
|---|---|
| `primary_airport` field | `models/destination.py` |
| 16 real airport codes | `data/destinations.py` |
| `ExpiringProviderCache.get_or_compute(expires_from=...)` — derive the provider deadline from the freshly-computed value (Duffel states expiry in the response, not before it) | `providers/cache.py` |
| `ProviderFailureKind.OFFERS_TRUNCATED` + message | `providers/failures.py` |
| `services/real_supply.py` — **new.** `acquire_real_supply()`: `build_plan` → per-edge city→IATA translation → cached `duffel.fetch_offers` → results remapped back to city space → `acquire()` → `OfferSnapshot`. Records `OFFERS_TRUNCATED` and unresolved-airport gaps as typed `SnapshotIssue`s. Returns `RealSupplyResult` (plan, snapshot, `RealSupplyMetrics`, unresolved nodes). | `services/real_supply.py` |
| `tests/test_v8_supply.py` — **new**, 9 tests | |

## Verification — real Duffel Test Mode

**Bounded acquisition ramp** (`airports=["CGN"]`, 1 departure day, inter-city on):

| cap | planned | calls | wall | offers received → retained (truncated) | unusable |
|---|---|---|---|---|---|
| 5 | 5 | 5 | 12.0s | 534 → 92 (442) | 0 |
| 10 | 10 | 10 | 18.7s | 1127 → 195 (932) | 0 |
| 25 | 25 | 25 | 51.8s | 2369 → 490 (1879) | 0 |

**Real QUICK + SMART** (`preferred=["Vienna","Prague"]`, 3 destinations × 4 days, 48 edges, 83.9s acquisition, 3209 real offers → 922 retained, 1 edge `UNAVAILABLE` recorded):

```
QUICK  34ms   150 states   CGN -> Prague -> Vienna -> CGN   EUR 403.02   score 0.703856
SMART  158ms 1036 states   CGN -> Vienna -> Prague -> CGN   EUR 689.02   score 0.727588
```

- **Zero-network beam invariant holds on a real snapshot:** 440 / 484 snapshot
  lookups during the search, 0 network calls (`SnapshotTransportProvider` has no
  client).
- **Normalization:** ~9000 real offers across all runs, **0 parser errors, 0 unusable**.
- **Truncation never silent:** every run recorded `OFFERS_TRUNCATED` with the exact count.
- **Budget bound holds:** calls ≤ plan ≤ cap in every case.
- **Offer expiry** captured (~30 min out), feeds the snapshot's `earliest_expiry`.
- `CALL_BUDGET_EXHAUSTED` recorded whenever coverage was bounded.

**Regression:** targeted suites (destination/catalog/provider/cache/planner/beam/
acquisition/v5/v75/v76) — exit 0. Full suite — running.

## Not done (Phase 2 remainder + beyond)

- Supply-diagnostics **API contract** for the frozen frontend (metrics DTO on a
  search response) — `RealSupplyResult.metrics` exists but is not surfaced on
  `/api/v1`.
- Wiring `acquire_real_supply` into an actual endpoint behind a feature flag
  (real search is still opt-in library-only; `/api/v1/search` is synthetic).
- Real cross-currency: **still UNVERIFIED** — every sandbox offer was EUR.
- 50-call ramp and a production-coverage SMART (~200 calls, ~6 min) not run —
  acquisition latency (~1.7s/edge) makes them slow; boundedness is proven at 25.
- Phases 3–5, security, Docker, frontend adapters, final report — untouched.

## Resume point

Phase 1 committed as `224446f` (not pushed). Phase 2 code in the working tree,
uncommitted: `models/destination.py`, `data/destinations.py`,
`providers/cache.py`, `providers/failures.py`, `services/real_supply.py` (new),
`tests/test_v8_supply.py` (new).

Next: Phase 3 — `services/` revalidation. Revalidate each `BookingItem` of a
`JourneyBookingIntent` against Duffel (existence, availability, price/currency,
expiry, baggage, payment/hold metadata), return original/revalidated totals +
deltas + changed items, wire `PriceTolerance` (NO_INCREASE / ABSOLUTE_EUR /
PERCENTAGE), require renewed confirmation on any breach.

`models/booking.py` already has the full V7.5 booking domain (state machine,
`JourneyBookingIntent`, `RevalidationResult`, `PriceTolerance`) — Phase 3/5
execute against it, they do not rebuild it. Duffel offer→`BookingItem` and the
supply metrics contract for the frozen frontend still need building.
