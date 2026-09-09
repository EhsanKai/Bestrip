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

## Resume point

HEAD `e70c0ee`, Phase 1 code in the working tree (6 files, listed above),
uncommitted. Next task: decide city→IATA mapping strategy, then build
`services/` Duffel supply integration. `models/booking.py` already has the full
V7.5 booking domain (state machine, `JourneyBookingIntent`, `RevalidationResult`,
`PriceTolerance`) — Phase 3/5 execute against it, they do not rebuild it.
