# V9 Phase 2 — Bootstrap Market Prior: acceptable source options

Branch `claude/travel-planner-mvp-nvb267`. This document exists so nobody has
to re-derive, mid-incident, whether a candidate data source for the Bootstrap
Market Prior is allowed. If a source is not clearly in the APPROVED list
below, treat it as **NOT APPROVED FOR INGESTION** until Legal/BD signs off and
this document is updated.

## The hard rule

> Detoura does not scrape consumer flight-shopping websites to build the
> Bootstrap Market Prior, does not bypass bot protection, rate limits or
> CAPTCHAs, and does not reverse-engineer a private/undocumented API to get
> around those controls. This applies regardless of technical feasibility.

This is enforced architecturally, not just by policy: every source ingested by
`services/market_prior_import.py` implements the provider-neutral
`MarketPriorSource` protocol (`services/market_prior_source.py`), which only
*receives* records — it has no HTTP client, no browser automation, no
credential store, and nothing in Phase 2 schedules an external crawl (§27).
Adding a new source means writing an adapter that produces the same
provider-neutral record shape; it does not change the ingestion pipeline, the
prohibition above, or the not-a-quote guarantees in
`docs/V9_PHASE2_MARKET_PRIOR_AND_CATALOG.md`.

## Category matrix

| Category | Example | Status | Notes |
|---|---|---|---|
| An airline's or GDS's **official, licensed data API** with a signed agreement covering this use | An airline's public fares API, a GDS market-data product | **APPROVED**, once contracted | Build a `MarketPriorSource` adapter behind the same protocol; nothing about ingestion changes. |
| A **licensed third-party market-data feed** (a data vendor selling aggregated, anonymized historical fares) | A travel-data vendor's historical-fares product | **APPROVED**, once licensed | Same as above. Record the license/version in `source_version`. |
| A **contracted supply partner's aggregate reporting** (e.g., Duffel or another supplier's own aggregate/market-insights product, if and when offered under contract) | A supplier's "market insights" export | **APPROVED**, once contracted | Not the live per-request Offer Request API — a separate, explicitly aggregate product. |
| A **permitted public dataset** with a license that allows this use (open government tourism/transport statistics, an explicitly public-domain fares dataset) | National statistics office flight volume/price series | **APPROVED**, with the license text kept alongside the import config | These are usually coarse (route/season, not day-level) — a good match for the sparse bucket model in §1/§4. |
| A **manual, approved import** — an analyst-curated CSV/JSON of illustrative or approximate figures, explicitly reviewed before use | An ops-curated seasonal-fare estimate spreadsheet | **APPROVED**, per-file sign-off | Use `CsvMarketPriorSource` / `JsonMarketPriorSource`; the `source` field must identify the reviewer/process, not just "csv". |
| **Detoura's own accumulated live observations** | Nothing new — already Phase 1 | **N/A to this document** | This is `DETOURA_LIVE_OBSERVATION`, not `BOOTSTRAP_PRIOR` — see the two-source-type separation in the Phase 2 doc. It is never confused with a bootstrap prior (`models/market_prior.py::MarketDataSource`). |
| **Scraping Google Flights, Skyscanner, Kiwi, airline websites, or any other consumer-facing flight-shopping site**, with or without headless-browser automation | — | **NOT APPROVED FOR INGESTION** | Terms-of-service and anti-automation violation regardless of technical feasibility. Not built, not planned. |
| **Bypassing or defeating bot protection, CAPTCHAs, or rate limiting** on any site to obtain fare data | — | **NOT APPROVED FOR INGESTION** | Same rule restated for emphasis — this is the one most likely to be "technically easy, still not done." |
| **Reverse-engineering a private/undocumented API** (an internal endpoint a site's own frontend calls, not published or licensed for third-party use) | — | **NOT APPROVED FOR INGESTION** | Applies even if the endpoint is unauthenticated or trivially discoverable. |
| An **unlicensed aggregator or "fare data" reseller** whose own sourcing is unclear or itself scraped | — | **NOT APPROVED FOR INGESTION** until their sourcing is verified | The prohibition follows the data, not just the immediate vendor — Detoura will not launder a scraped dataset through a reseller. |
| **Purchasing data of unknown/undisclosed provenance** ("fare history" sold with no stated collection method) | — | **NOT APPROVED FOR INGESTION** until provenance is verified in writing | Ask the seller how it was collected before any technical integration work starts. |

## What Phase 2 actually ships

No real external source is wired up in this phase. Phase 2 ships:

* the architecture (`BootstrapMarketPrior`, `MarketPriorSource` protocol,
  import pipeline, decay/confidence, opportunity scoring, ~200-city catalog);
* `FixtureMarketPriorSource` — a deterministic, offline, honestly-labelled
  synthetic generator, used for tests and the benchmarks in
  `scripts/bench_catalog_scale.py`, `scripts/bench_prior_value.py`,
  `scripts/bench_legacy_vs_200.py`;
* `JsonMarketPriorSource` / `CsvMarketPriorSource` — generic readers for an
  operator-approved file, for the "manual, approved import" category above.

This is reported truthfully in the release gate as:

* **MARKET PRIOR ARCHITECTURE: READY** — the domain model, persistence,
  import pipeline, decay, opportunity scoring and catalog all work end-to-end
  and are tested (`tests/test_v9_market_prior.py`, `tests/test_v9_catalog.py`,
  `tests/test_v9_phase2_budget.py`).
* **REAL BOOTSTRAP SOURCE: NOT CONFIGURED** — no category-`APPROVED` external
  feed is contracted or wired up yet. Every acquisition-eligible destination
  therefore starts cold on the prior axis until (a) a source from the matrix
  above is approved and an adapter is written for it, or (b) Detoura's own
  live Price Memory accumulates (which needs no prior at all — see the Phase 1
  doc). This is not a defect to "fix later in secret"; it is the honest state
  of a bootstrap system with no bootstrap data yet.

## Adding a real source later

1. Confirm it against the matrix above (or add a new row with sign-off).
2. Write a small adapter implementing `MarketPriorSource` (`meta()` +
   `records()`), yielding the provider-neutral record shape documented in
   `services/market_prior_source.py`.
3. Run it once with `dry_run=True` (`services/market_prior_import.run_import`)
   and inspect `rows_rejected` / `rejected` before a real import.
4. Never add a name/email/phone/document field to the record shape — the
   importer's `_ALLOWED_KEYS` allow-list drops anything not already an
   approved market/price/availability field, and this is a deliberate control,
   not an oversight to work around.

## V9 Phase 2.5 update — the authorization/acquisition layer

Phase 2.5 adds the *infrastructure* that can safely acquire from a real
source once one is approved — `SourceRegistration` (fail-closed
`AuthorizationStatus`, default `REVIEW_REQUIRED`), a bounded
`BootstrapJob`/`BootstrapAcquisitionTask` model, and
`AuthorizedHttpFetcher` (domain allowlist, SSRF guard, redirect validation,
response-size limit, CAPTCHA/challenge detection, rate limiting) — see
`docs/V9_PHASE2_5_MARKET_PRIOR_ACQUISITION.md` for the full architecture.

**This does not change the rule above or this document's status matrix.**
An `AUTHORIZED_WEB_SOURCE` / `API_SOURCE` registration still defaults to
`REVIEW_REQUIRED` and `AuthorizedHttpFetcher.registration.network_allowed`
is checked before *every* request; nothing in Phase 2.5 makes a live network
call to a real third-party domain, and no such domain is registered.

**REAL AUTHORIZED EXTERNAL SOURCE: NOT CONFIGURED** — unchanged from Phase 2.
Phase 2.5 ships exactly one wired-up source: `fixture-europe-demo`
(`SourceType.FILE_IMPORT`, deterministic, offline, `APPROVED` because it makes
no external request at all — see `services/bootstrap_registry.py`). The full
acquisition pipeline (job -> task -> fetch -> parse -> validate -> import) is
demonstrated end to end against it in `scripts/bootstrap_e2e_demo.py`, and the
network-safety controls (`AuthorizedHttpFetcher`) are proven against local
stub HTTP clients in `tests/test_v9_phase25_network_safety.py` — never against
a real domain.
