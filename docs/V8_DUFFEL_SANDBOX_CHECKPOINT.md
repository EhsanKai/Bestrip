# V8 — Duffel Sandbox Probe Checkpoint

**Baseline:** `e70c0ee` (V7.6) · **Nothing staged, nothing committed.**
**Requests made: exactly one** (plus one earlier attempt that never left the machine — see TLS below).

## Result

| | |
|---|---|
| Request | **SUCCEEDED** |
| HTTP status | **201 Created** |
| Route | `CGN → BCN`, 2026-10-15, 1 adult, economy |
| Offers returned | **23** |
| Offers normalized | **20** (capped by `max_offers=20`) |
| Offers dropped | **0** |
| Parser errors | **none** |

**Duffel Test Mode is now VERIFIED for the offer-search path.** The adapter written against modelled fixtures parsed real sandbox data on the first attempt with zero dropped offers.

## Sample normalized output

```
CGN->BCN  10:50-12:55  125min  €70.35pp  BA 1516   cabin=included  checked=included  1 segment
CGN->BCN  10:50-12:55  125min  €71.25pp  ZZ 9289   cabin=included  checked=included  1 segment
CGN->BCN  08:30-12:40  250min  €153.60pp LH 4105   cabin=included  checked=unknown   2 segments
```

The third is the important one: a **real one-stop offer mapped to a single `CGN→BCN` leg**, with the connection kept as segment metadata. The "a connection is not a visited city" rule holds against live data, not just fixtures.

## Response shape vs our fixtures

**Matched exactly:** `data.offers[]`, `total_amount`/`total_currency` as decimal *strings*, `slices[].segments[]`, `origin`/`destination` with `iata_code`, `departing_at`/`arriving_at`, ISO-8601 `duration` (`PT2H5M`), `marketing_carrier.iata_code`, `marketing_carrier_flight_number`, `expires_at`, `payment_requirements.requires_instant_payment`, `owner.iata_code`, and `segments[].passengers[].baggages[]` as `{type, quantity}`.

**Present in real responses, absent from our fixtures** (none broke the parser — we ignore unknown keys):

| Field | Why it matters |
|---|---|
| **`live_mode`** (on `data` *and* each offer) | **Safety.** A machine-readable statement that this was sandbox. V8 should assert `live_mode is False` and refuse otherwise — a second, independent guard beyond the token prefix. |
| `partial` | Offers can be partial. Unexamined; may affect bookability. |
| `conditions` (offer and slice) | Change/refund rules. Needed for V8 booking copy. |
| `passenger_identity_documents_required`, `supported_passenger_identity_document_types` | Passport requirements — drives the traveller-details form. |
| `segments[].stops` | Duffel models *technical stops within a segment*. Our `stops = len(segments) - 1` may undercount. |
| `operating_carrier_flight_number` | We keep marketing only; a codeshare displays differently. |
| `origin_type` / `destination_type` | Slice endpoints can be city or airport. |
| `available_services` (present, `true`) | Paid-baggage path exists in real data but was **not** exercised — no offer needed it. |
| `total_emissions_kg`, `intended_*`, `private_fares`, `ngs_shelf`, `fare_brand_name`, `comparison_key`, `aircraft`, `media`, `distance`, terminals | Informational; no action. |

Airport objects are much richer than modelled (`latitude`, `longitude`, `time_zone`, `icao_code`, `city`, …). We read only `iata_code`, which is all we need.

## Parser compatibility: no changes required

Everything the adapter reads exists and is shaped as expected. `_parse_dt` handled microsecond precision (`2026-09-09T01:11:45.139847Z`). `201` passed `response.ok`. Multi-slice refusal never triggered — a one-way Offer Request returns single-slice offers, exactly as designed.

## Baggage observations

Real BA economy returned `[{"type":"checked","quantity":1},{"type":"carry_on","quantity":1}]` → `cabin=INCLUDED`, `checked=INCLUDED`. Correct.

The LH two-segment offer produced `checked=UNKNOWN`: one segment stated it, the other did not, and the conservative combine rule fired **on real data**. That is the rule doing exactly its job — a bag included on one leg and unmentioned on the next is not included for the journey.

`personal_item` was `UNKNOWN` on every offer, correctly — Duffel has no personal-item concept.

## Currency observations

Every offer: `EUR`, matching the request. `total_amount` is a decimal string, `base_amount` + `tax_amount` provided separately. No conversion path was exercised, so **cross-currency handling remains unverified against real data** — it works against fixtures only.

## Offer expiry and IDs

- `expires_at` is populated on every offer and is **~30 minutes out** (`01:11:45Z` for a probe at ~`00:41Z`). Real, short, and enforced.
- Microsecond precision, `Z` suffix — parsed correctly.
- `payment_requirements.requires_instant_payment: false` → `hold_supported = True`, with `payment_required_by` (3 days) and `price_guarantee_expires_at` (2 days) both populated. **Hold is genuinely available in sandbox.**
- Offer ids are `off_0000BADSEwWhjmCpAdMZTm`-style; we prefix to `duffel-off_…`.

A 30-minute expiry validates the `ExpiringProviderCache` design and makes the 30-second safety margin look reasonable rather than paranoid.

## Environment finding: TLS

The first attempt failed with `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` — **no request left the machine**. `UrllibHttpClient` uses the stdlib default trust store, and this Python's `openssl_cafile` points at a non-existent Framework path.

Worked around for the probe with `SSL_CERT_FILE=$(python3 -c 'import certifi;print(certifi.where())')`. **This is a real deployment concern, not a local quirk:** any environment whose trust store is unconfigured will fail every Duffel call at the handshake. V8 should either depend on `certifi` explicitly or document the requirement.

## Exact changes V8 will require

1. **Assert `live_mode is False`** on the response and refuse otherwise. Cheap, and an independent check on the token prefix.
2. **Handle the `max_offers` cap honestly** — 23 offers became 20 with no record. Either raise the cap or record the truncation the way `CALL_BUDGET_EXHAUSTED` does.
3. **TLS trust store** — depend on `certifi` or document `SSL_CERT_FILE`.
4. **Add real-shape fixtures** for `live_mode`, `partial`, `conditions`, `passenger_identity_documents_required`, `segments[].stops`.
5. **Revisit stop counting** against `segments[].stops` (technical stops).
6. **Exercise `available_services`** for paid baggage — present in real data, untested against it.
7. **Verify cross-currency** with a non-EUR request; unverified against real data.
8. **Budget for a ~30-minute offer lifetime** in the booking flow — revalidation is not optional.

None of these is a parser defect. All are additions.

## Tests run

`tests/test_v75_provider.py`, `tests/test_v75_adversarial.py`, `tests/test_v76_preflight.py` → **76 passed, 0 failed**. No test was changed.

## Git status

HEAD `e70c0ee`. Nothing staged, nothing committed, nothing pushed. Working tree carries 13 untracked/modified paths, all pre-existing frontend and local tool state. This document is the only new file.

## Next recommended step

Add the `live_mode is False` assertion and the truncation record (items 1–2) — both small, both safety-relevant — then capture the real payload as a sanitized fixture so the shape is locked before V8 builds on it. Only then start Sandbox Order work.
