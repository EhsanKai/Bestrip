# V9 Limited Beta Reality Audit — End-to-End Capability & Gap Assessment

**READ-ONLY audit. No executable code was changed. No frontend code was
changed. Nothing was committed.**

Audited HEAD: `6b47e2d`. Method: fresh tracing of the executable system
(backend routes, service call graphs, frontend component/data-flow tracing,
direct shell inspection of environment variables and the real local
database) — not inference from prior phase-completion reports. Where a
prior report's claim is cited, it was independently re-verified against
current code/state, not trusted.

This report does not use vague statuses. Every capability is classified as
exactly one of: **REAL & CONNECTED**, **BUILT BUT DISCONNECTED**, **FIXTURE
/ SYNTHETIC / TEST-ONLY**, **NOT BUILT**, or **EXTERNAL BLOCKER**.

---

## The core finding, stated once, up front

Detoura has a genuinely strong, well-engineered **backend architecture** in
almost every domain this audit traced — search, payments, booking
orchestration, financial documents, Ops recovery. The gap to Limited Beta
is not "unfinished code" in most of these domains; it is **disconnection**:
real, tested capabilities that the actual product path never reaches. Three
disconnections are structural enough to individually block "a real user
takes a real trip":

1. **Payment and booking are two independent systems.** A confirmed booking
   issues real provider orders with zero Detoura-collected payment; an
   authorized payment has no enforced effect on booking issuance beyond a
   shared `booking_id` at the data level. The orchestrator that would couple
   them (`run_paid_booking`) has no caller anywhere under `api/`.
2. **The shipped consumer UI breaks the funnel at its two most trust-
   critical moments.** "Your Journey" (the screen literally named after the
   product's own metaphor) shows a hardcoded fixture regardless of which
   trip was actually searched and selected. The "My Trips" screen the real
   navigation routes to is also a hardcoded fixture — the one real,
   backend-connected trips screen (`SavedTrips.tsx`) exists and works but
   has no navigation path to it anywhere in the shipped app. Login never
   calls the real, working backend auth API at all.
3. **Production email does not exist as code**, not merely as
   configuration — both branches of the provider-resolution function return
   the sandbox adapter; no live adapter (SES/SendGrid/Postmark/SMTP/etc.)
   is implemented anywhere in the repository.

Every other finding in this report should be read against that context: a
lot of excellent, tested engineering exists, and a small number of missing
connective threads are why it doesn't yet add up to a product a real user
can complete a paid trip through.

---

## 1–23. Journey trace, domain by domain

### A/B. Discover + Search Intelligence — executable reality

Traced fresh from `POST /api/v1/search` through the actual current call
graph (this reflects V9 Search Integration Slice 1 + Search Intelligence
Slice 1.5, both closed on this HEAD):

| Stage | Classification | Production caller | Data source | Real or estimated | Known limitation |
|---|---|---|---|---|---|
| Origin resolution | REAL & CONNECTED | `api/v1.py::search()` → `CatalogOriginResolver` | Full 203-city catalog | Real (exact/normalized/IATA/alias match; never fuzzy-auto-resolved) | none material |
| Origin autocomplete backend | REAL & CONNECTED | `GET /api/v1/origins/suggest` | Same catalog | Real | not called from any shipped frontend screen (no UI built yet — correctly out of scope, Slice 2) |
| Nearby-airport backend | REAL & CONNECTED | `GET /api/v1/origins/nearby` | Real haversine over catalog coordinates | Real | not called from any shipped frontend screen |
| User control of nearby-airport expansion | NOT BUILT | — | — | — | no UI/API concept of "include nearby departure airports" as a user toggle exists yet |
| Browser geolocation | NOT BUILT | — | — | — | explicitly out of scope through two slices; backend seam (`/origins/nearby`) is ready for it |
| Candidate catalog | REAL & CONNECTED | `acquisition_catalog()` | 203/203 destinations enabled+eligible | Real | — |
| Candidate funnel | REAL & CONNECTED (when live search active) | `SearchIntelRecorder.plan_candidates` → `candidate_funnel.run_funnel` | Real | Algorithm real; **fed by ~empty production data** (see §3) | active only when `SEARCH_LIVE_ENABLED=1` + valid Duffel token — both unset in this environment and not on by default |
| Attractiveness | REAL & CONNECTED (when live search active) | `attractiveness_store.batch_get_profiles` | Derived from catalog tags | Algorithm real; **table is empty on a clean deployment** (§3) | neutral 0.5 fallback confirmed safe |
| EXPLOIT/EXPLORE | REAL & CONNECTED (when live search active) | `acquisition_scoring.allocate` | — | Real, deterministic | — |
| Market Prior | REAL & CONNECTED (when live search active) | `opportunity.score_opportunities` | `market_priors` table | Algorithm real; **~0.1%/0% coverage** (§3) | capped/superseded by live evidence by design |
| Price Memory | REAL & CONNECTED (when live search active) | `market_intel.batch_market_signals` | `price_observations` table | Real; **0 rows on a clean deployment**, only populated once real live searches run | — |
| Real Duffel acquisition | REAL & CONNECTED, **gated** | `real_supply.acquire_real_supply` | Live Duffel Test Mode | Real when enabled | requires `SEARCH_LIVE_ENABLED=1` + a valid `duffel_test_` token; **neither is set in this environment** |
| Provider-call budget | REAL & CONNECTED | `ProviderCallBudget` | — | Real, enforced | — |
| Portfolio / diversity | REAL & CONNECTED (when live search active) | `portfolio.select_portfolio` | — | Real, proven behavioral (Slice 1.5's own tests) | only active on the live path, not the synthetic fallback |
| Accommodation economics | **FIXTURE / SYNTHETIC — always** | `SyntheticAccommodationDataProvider` | Hand-tabulated nightly rates | Estimated, always, on every path including the live-flight path | `RealAccommodationDataProvider` exists only as a deliberate `NotImplementedError` placeholder — no real hotel data source anywhere |
| Ground-transfer economics | **FIXTURE / SYNTHETIC — always** | `SyntheticGroundTransferProvider` | Linear km-based estimate | Estimated, always | `RealGroundTransferProvider` is likewise a deliberate placeholder |
| Baggage economics | REAL when live, UNKNOWN-safe when not | `duffel.py::_map_baggage` | Real Duffel offer data | Real on the live path; absent → explicitly UNKNOWN, never fabricated | — |
| Whole-trip budget calculation | REAL & CONNECTED | `services/planner.py` | Combines the above | **Partially estimated by construction** — flights can be real, accommodation/ground-transfer never are | this is a real, structural gap against the "whole-trip value" promise, not a bug |
| QUICK/SMART/DEEP | REAL & CONNECTED | `search_modes.apply_mode` | — | Real | controls beam width/rounds only, as documented |
| Provenance (LIVE/SYNTHETIC) | REAL & CONNECTED | `diagnostics.supply_source` | — | Real, truthful | PRIOR/UNKNOWN axis exists internally but is not surfaced to the consumer response |
| Fallback behavior | REAL & CONNECTED | synthetic `TravelPlanner.explore()` | Hand-curated 5-airport/16-city network | Synthetic, honestly labeled | this is the **default** path in this environment (no live credentials) |

**ALGORITHM EXISTS vs. ALGORITHM IS FED WITH MEANINGFUL PRODUCTION DATA —
explicitly distinguished, as instructed**: every algorithmic stage above
(funnel, attractiveness, market prior, portfolio) is real, tested, and
correctly wired. But two of its three data inputs are effectively empty in
any environment that hasn't manually run Ops acquisition jobs, and the
third (live Duffel) requires explicit configuration this environment does
not have. **The architecture is ready; the data is not, and won't be
automatically on a fresh Beta deployment.**

### 3. Search data reality — from `docs/generated/v9_market_prior_coverage.json` + direct queries

- **Market Prior**: fresh-install coverage **0%** (`persistence.bootstrap()`
  seeds only the example markup policy/promo — confirmed by reading it
  directly, nothing market-prior-related). This environment's local
  coverage: **0.0985%** (6 of 6,090 possible origin×destination pairs, 10
  rows, all one manual Ops-flow test source `fixture-europe-demo` in a
  gitignored, untracked local `detoura.db` — not shipped with the repo).
  Populated only by running the Ops Bootstrap Acquisition job control
  (`api/ops_market_prior_acquisition.py`) for real, at real scale — nobody
  has done this beyond a small manual test. **A Beta deployment would NOT
  automatically have meaningful coverage** — it requires a deliberate,
  currently-unstarted acquisition effort.
- **Price Memory**: fresh-install state **0 rows**. Real consumer search
  now genuinely writes to it (Slice 1.5's wiring), but only when
  `SEARCH_LIVE_ENABLED=1` and a real Duffel token are configured — neither
  is true by default or in this environment. It becomes meaningful only
  after real search volume accumulates over time once live search is
  actually turned on.
- **Attractiveness**: fresh-install state **0 rows** (`destination_attractiveness`
  table empty). NOT automatically seeded — requires an explicit
  `POST /api/v1/ops/attractiveness/reseed` Ops action. Consumer search on a
  clean deployment has **no** attractiveness profiles available until that
  action is taken once. When absent: confirmed safe (neutral 0.5 default,
  never crashes, never fabricates a score).
- **Destination catalog**: **203** destinations, provenance: synthetic/
  curated (16 hand-tuned core cities + 187 broadly-tagged catalog entries —
  the codebase's own docstrings are explicit that this metadata is
  synthetic, not sourced from a real travel dataset). **Image readiness:
  REAL & CONNECTED, complete** — 203/203 destinations have a manifest entry
  and a corresponding WebP asset file on disk, served via a real mounted
  static route.

### 4. Real supply — provider map

| Provider | Search | Revalidation | Booking/order | Cancel | Change | Refund | Test/live | Credentials here? | Real E2E verified? |
|---|---|---|---|---|---|---|---|---|---|
| **Duffel** | REAL & CONNECTED (gated) | REAL & CONNECTED | REAL & CONNECTED | REAL & CONNECTED | REAL & CONNECTED | N/A (flight refunds are provider policy-dependent, handled as cancellation) | Fails closed to test-token-only by design | **No** — `DUFFEL_ACCESS_TOKEN` unset | **No** — never run against a real Duffel Test Mode account |
| **Amadeus** | BUILT BUT DISCONNECTED — a real, fully-implemented, well-tested `AmadeusTransportProvider` exists (OAuth2, real request/response mapping, pagination, error handling) but has **zero references anywhere in `api/` or `services/`** | N/A (never wired) | N/A (never wired) | N/A | N/A | N/A | N/A (never constructed in production) | No | Never — not reachable at all |

**LCC/low-cost-carrier coverage — the structural gap, stated as evidence,
not proposal**: Detoura's supply architecture depends entirely on Duffel
today (Amadeus exists but is wired nowhere). Neither Duffel's NDC/GDS-style
aggregation nor a traditional Amadeus GDS integration is known, in the
travel industry generally, to carry major low-cost carriers (Ryanair, Wizz
Air, easyJet, etc.) — those carriers are famously absent from most
GDS/aggregator integrations by the carriers' own commercial choice, not a
gap specific to this codebase's provider selection. No LCC-specific
provider, adapter, or scraping mechanism exists anywhere in the repository
(confirmed by grep — zero mentions of any LCC carrier name or "low-cost" in
provider code). **This is a real, currently-unaddressed structural gap for
any product promise that depends on budget-carrier inventory** — not a
missing credential, a missing integration.

### 5. Frontend reality — see the dedicated subsection below (largest single section of this audit)

### 6. Account reality

| Capability | Classification |
|---|---|
| Register | REAL & CONNECTED (backend); unreachable from shipped frontend |
| Login | REAL & CONNECTED (backend); **Login.tsx never calls it — zero fetch calls in the component, submits always fail client-side** |
| Logout | REAL & CONNECTED (backend); unreachable from consumer frontend |
| Session (HttpOnly cookie, validation, expiry) | REAL & CONNECTED (backend) |
| CSRF (double-submit) | REAL & CONNECTED (backend) |
| Password reset | **NOT BUILT** (zero backend code; frontend "forgot password" UI calls only an unsupplied prop) |
| Password change | **NOT BUILT** (persistence function exists, no API route ever calls it) |
| Ownership (account→booking) | REAL & CONNECTED, single-transaction, idempotent |
| My Trips (backend) | REAL & CONNECTED, anti-enumeration; unreachable from shipped frontend |
| Anonymous booking | REAL & CONNECTED (default path) |
| Claiming at creation time | REAL & CONNECTED |
| Retroactive claiming (claim an existing anonymous booking after later login) | **NOT BUILT** — no such route exists |
| Account deletion | **NOT BUILT** (confirmed, zero code anywhere) |
| Data export | **NOT BUILT** (confirmed, zero code anywhere) |
| Retention (account/session data) | **NOT BUILT** — all "retention" code in the repo is for pricing-cache data, none for accounts/sessions/bookings |
| Session purge | **NOT BUILT** — expiry is checked logically at read time; no job/endpoint physically deletes expired session rows, which accumulate indefinitely |
| Google sign-in | **NOT BUILT** — zero OAuth consumer-login code anywhere |

**The backend account system is genuinely well-built** (enumeration
resistance, rate limiting, Argon2, real CSRF, real ownership). **None of it
is currently reachable by a real user through the shipped app**, because
the login screen never calls it.

### 7. Commercial truth

Configured economics **match the expected BASIC 3% / ALL_IN_ONE 5% + €6**
exactly (`persistence/policies.py`), but the seed policy's own label is
`"Detoura sandbox markup v2 (example - not commercial truth)"` — the
numbers are real, configured, currently-active defaults, explicitly marked
in the code as a placeholder pending a real business decision.

- Markup/fee computed **entirely server-side** (`services/commercial.py`),
  confirmed no client-suppliable price/fee/markup field exists on the
  booking-intent request model.
- `CheckoutSnapshot`: real, immutable, freezes the full quote at payment
  start; expiry blocks stale confirmation.
- Payable now: transport subtotal + markup/fee only, computed from
  revalidated ticket prices.
- Accommodation/transfers/extras: **NOT BUILT as purchasable line items** —
  they exist only as a separate whole-trip *estimate*, explicitly and
  structurally excluded from the charged amount. A consumer cannot actually
  buy the hotel or ground transfer Detoura shows them.
- Invoice/financial-document truth: **REAL & CONNECTED** — the document
  service reads real `captured_amount`/`refunded_amount` from the payments
  table directly, never re-derives from the quoted total, and refuses to
  issue a document without a real captured payment fact. Correct by
  construction — but see §8/§10 for what "captured" currently requires.

### 8. Payment reality

Consumer-facing payment API (create/authorize/refund/webhook) is **REAL &
CONNECTED**: server-owned amounts, real idempotency (client-supplied +
server-derived), real webhook signature verification with dedup and
provider-truth reconciliation (never trusts the payload's own status
claim), real UNKNOWN-status handling, real fail-closed test/live guard.

Manual capture exists and is correct but is **reachable only via the Ops
API** — no consumer-facing capture route exists at all.

`STRIPE_SECRET_KEY` is **not configured in this environment** (confirmed by
direct shell inspection). Real Stripe Test Mode E2E has **never been run**
— an external, credential-blocked gap, independently re-confirmed this
audit, not merely cited from a prior report.

**Most important finding, verified by exhaustive grep, not by trusting
`run_paid_booking()`'s existence**: **a successful payment authorization
today does NOT trigger or authorize real booking orchestration in the
production API.** `run_paid_booking`/`payment_booking_orchestrator` has
**zero callers anywhere under `src/detoura/api/`** — confirmed by a
repo-wide grep returning hits only in the module itself, docstrings, and
test files. The module's own docstring admits this directly. **Classification: BUILT BUT DISCONNECTED.**

### 9. Booking reality

The real, HTTP-reachable confirm flow (`POST /api/v1/booking-intents/{id}/confirm`
→ `start_confirmation` → `run_booking`) is genuinely solid: atomic
duplicate-execution claim (`claim_for_execution`), real multi-leg
sequential issuance with required-vs-optional-leg semantics, real
pre-confirm price reconciliation (409 on mismatch), real partial-failure
handling (`PARTIAL_FAILURE`/`RECOVERY_REQUIRED` states, never silently
collapsed to success or total failure), real provider-timeout distinction
from a definite failure. **This entire flow is REAL & CONNECTED — and
entirely independent of payment.** It creates real Duffel Test Mode orders
with zero Detoura-collected payment today.

**Airport lookup table impact (`booking_flow.py`'s `_AIRPORT_CITY`/
`_AIRPORT_COUNTRY`, ~20 entries) — quantified, not fixed**: of the 203
catalog airport codes now reachable via V9 Search Integration Slice 1's
widened origin resolution, **183 are absent** from this table. When a
booking's origin airport is one of those 183, `route_is_international()`
silently returns `False` regardless of the true country, so
`documents_required()` never flags a genuinely international route as
needing travel documents. **This is a live, currently-reachable gap for
any booking originating outside the original ~20-airport cluster**, not a
theoretical one — confirmed by tracing that `item.origin_airport` is
populated directly from the real resolved offer.

### 10. Payment ↔ Booking coupling — explicit verdict

**Can a real consumer today: (1) choose a real live-priced journey, (2)
authorize payment, (3) confirm once, (4) cause Detoura to issue all
required real provider tickets, (5) capture the correct amount, (6)
recover safely if only some tickets issue?**

**Classification: BUILT BUT DISCONNECTED.**

Every individual capability in that chain exists and is independently
well-built (real payment authorization, real multi-ticket issuance, real
atomic claim, real partial-failure recovery states, real capture logic).
**They are not wired to each other.** Booking confirmation
(`/booking-intents/{id}/confirm`) never touches the payment system.
Payment authorization/capture never triggers or is gated by booking
issuance beyond an incidental shared `booking_id` at the data level. The
one module that implements the intended coupling
(`payment_booking_orchestrator.run_paid_booking`, with the correct
authorization-first strategy and reconciliation-on-partial-failure design)
is fully unit-tested but has no caller reachable from any real HTTP route —
confirmed by exhaustive grep across `src/detoura/api/`, not inferred.

### 11. Customer documents

| Document | Generated | Persisted | Owner-only | Immutable | Real data | From My Trips | Downloadable | Emailed |
|---|---|---|---|---|---|---|---|---|
| Travel Pass | Yes | Server-built at request time | Yes | N/A (built fresh from live state) | Real | Yes (via real route) | N/A (JSON, rendered) | No |
| Receipt/Invoice | Yes, real deterministic PDF | Yes, immutable BLOB, idempotent | Yes | Yes, structurally (no update path) | Real | Metadata only | **No — PDF bytes never served by any route** (explicit stale TODO in code) | No |
| Credit note | Yes (same mechanism) | Yes | Yes | Yes | Real | Metadata only | **No** (same gap) | No |

**Classification: the generation/persistence/immutability/ownership layer
is REAL & CONNECTED; PDF download is BUILT BUT DISCONNECTED.** A customer
cannot download their own receipt or invoice today, even though it exists,
correctly, in the database.

### 12. Email reality

State machine, idempotency, atomic send-slot claiming, and UNKNOWN-status
handling are **REAL & CONNECTED, genuinely well-built**, and wired into the
real booking-finalize pipeline. **The production provider itself does not
exist as code**: `resolve_communication_provider()`'s two branches both
return the sandbox adapter — there is no SES/SendGrid/Postmark/SMTP adapter
anywhere in the repository to even configure. **A clean Beta deployment
cannot send one real transactional email today, regardless of environment
configuration**, because the code path to do so was never written.
**Classification: NOT BUILT** (the orchestration is real; the provider is
absent, which is a harder gap than "missing credentials").

### 13. My Trips / post-booking (combining backend + frontend findings)

Backend `GET /me/trips` is real, correct, anti-enumeration. The shipped
frontend "My Trips" nav destination is a hardcoded five-trip fixture that
never calls it. A separate, genuinely real, `localStorage`-backed
implementation (`SavedTrips.tsx`, wired to the real `/trips/recheck`
endpoint) exists and works but has **no navigation entry point anywhere in
the shipped UI** — every "My Trips" link and tab in the app routes to the
fixture instead. **Net effect: a real user cannot see their real trip
history today**, despite both a real backend and a real (if incompletely
connected) frontend implementation existing.

### 14. Hotel/accommodation reality

**Estimation only, everywhere, always** — `RealAccommodationDataProvider`
is a deliberate `NotImplementedError` placeholder; the synthetic provider
is unconditionally what every search path uses, including the live-flight
path. Accommodation affects whole-trip ranking using **estimates only**,
never real hotel data, and is never a purchasable line item at checkout.

### 15. Ground transfer reality

Same pattern: real haversine distance calculation (genuinely correct math,
verified in Slice 1), but the transfer *price/time* itself is always a
linear km-based synthetic estimate. `RealGroundTransferProvider` is a
deliberate placeholder. No real public-transport routing or booking exists
anywhere.

### 16. Baggage reality

| Stage | Status |
|---|---|
| Search input | Accepted (`BaggageRequirement`), passed through |
| Provider offer normalization | Real, on the live Duffel path — mapped from actual offer data, "absent is unknown," never fabricated |
| Journey economics | Real on the live path; UNKNOWN on the synthetic path (the synthetic network doesn't model baggage at all) |
| Checkout | Baggage is part of the revalidated transport price, not broken out |
| Revalidation | Real, re-checked against the live offer |
| Booking | Carried through to the issued order |

Baggage handling is genuinely correct where it matters (real data on the
live path, honest UNKNOWN elsewhere) — the limitation is upstream: the live
path itself is gated behind configuration this environment doesn't have.

### 17. LCC/airline coverage

Covered in §4 above. Restated for completeness: **no structural path to
low-cost-carrier inventory currently exists** through either wired provider
(Duffel) or the built-but-disconnected one (Amadeus) — this reflects the
travel industry's own well-known LCC/GDS-distribution gap, not a defect
specific to this codebase, but it is real and unaddressed regardless of
cause.

### 18. Privacy / account lifecycle (product completeness, not security — Phase 6 not reopened)

Account deletion, data export, retention-expiry, and session purge are
confirmed **NOT BUILT** (§6), consistent with the Phase 6 PII slice's prior
finding, independently re-confirmed this audit via fresh grep. Classified
here explicitly as **pre-Beta product/compliance readiness gaps**, not
security vulnerabilities — no evidence found that their absence constitutes
an active vulnerability (Phase 6 Security remains CLOSED and is not
reopened by this finding).

### 19. Operations

Ops recovery tooling for **payments** and **confirmations/documents/
communications** is **REAL & CONNECTED** and correctly domain-scoped (no
generic "set any status" backdoor exists — every recovery action is a
specific, validated domain operation). Ops auth fails closed correctly. **A
human operator could realistically recover an individual failed Beta
booking or payment today, provided they know to go looking for it** — see
§20 for why that's a real caveat. One real gap: the central `audit_events`
table is not written by either `ops_payments.py` or `ops_confirmations.py`
(payments have their own separate ledger, which partially compensates;
confirmations/communications recovery actions have no audit trail at all).
`DETOURA_OPS_TOKEN` is **not configured in this environment** — Ops is
correctly disabled (503) rather than open, but that also means it could not
be exercised live in this audit.

### 20. Observability

**Essentially NOT BUILT, stated plainly.** Only 3 files in the entire
backend call `logging` at all (5 call sites total, across the whole
payment/booking/provider path). Zero metrics/telemetry export of any kind
(no Prometheus/OpenTelemetry/StatsD/Sentry/equivalent). Zero alerting.
`/health` returns a static "ok" with no dependency check (no DB ping, no
provider check). `SearchIntelligenceTrace` is a real, useful internal
search-quality/ranking-debug record — **explicitly not production
observability**, and this audit does not conflate the two. **An operator
today has no automated way to learn that something is wrong** — they would
have to already suspect a problem and go look at Ops screens for a specific
booking/payment.

### 21. Deployment / production config

Dockerfile is real and sound (multi-stage, non-root, single SQLite volume).
The **single-worker constraint is explicit and load-bearing**, not merely a
default: the session store refuses to start with >1 worker without Redis,
and live `BookingRun` state lives in an in-process dict until a run reaches
a terminal phase — a mid-confirmation process restart loses that run's live
progress. This is a real architectural constraint for Beta scale-out, not
a defect at Beta's expected traffic level. SQLite/WAL is appropriate at
that scale, per the code's own design statement. CORS defaults to
localhost-only dev origins and must be explicitly configured for any real
deployment. No HTTPS-termination assumption breaks behind a standard
TLS-terminating proxy (X-Forwarded-For handling is real and configurable).
A long, compiled list of required environment variables exists for a real
deployment (Duffel/Stripe/Ops/search-live/communication/CORS/session-store
— see the full list generated during this audit) — none are set in this
audit environment.

### 22. External blockers (evidence-backed only)

- **Stripe Test credentials**: unavailable in this environment (confirmed
  directly, not inferred).
- **Duffel Test credentials**: unavailable in this environment (confirmed
  directly).
- **Production email provider**: not an external blocker in the
  credentials sense — **no code exists to configure a credential into**.
  This is a NOT BUILT item that happens to also need an external service
  once built.
- **Germany/EU legal review, tax/invoicing review, package-travel
  classification, privacy/retention review**: external, unstarted,
  consistent with every prior slice's own honest reporting; not
  re-litigated here beyond confirming no code in this repo claims to have
  resolved any of them (the financial-document module explicitly disclaims
  legal invoice-numbering compliance in its own docstring).
- **Provider commercial agreements / domain / trademark**: no evidence
  found either way in this repository; out of an engineering audit's scope
  to assess.

---

## Frontend reality — dedicated summary

Traced fresh: `frontend/` is **not** purely an uncommitted prototype — the
core screens (Landing, Discover, Results, Trip Detail, the real
BookingExperience checkout, the real API client) are genuinely committed,
backend-connected product code with working-tree edits on top. The
**newest** screens (Login, MyTrips, the orphaned Checkout.tsx, the "Your
Journey" drawer fixture) are the ones found to be disconnected — and they
are exactly the ones still **untracked** in git, a detail that itself
corroborates the finding rather than contradicting it.

**Exact points where Discover → Results → Your Journey → Checkout → My
Trips stops being connected to the real backend**:

1. Discover → Results → Trip Detail: fully real, no break.
2. Results → "Select journey" → **the "Your Journey" drawer: BREAK #1** —
   shows a hardcoded fixture (a fake Prague/Vienna/Budapest trip with fake
   prices) regardless of the trip actually selected.
3. Drawer → "Continue to checkout" → **BookingExperience: reconnects to
   real data** (the fixture is a dead-end cosmetic step, not a data
   corruption) — but this screen explicitly skips payment entirely ("Payment
   is not required in this test version"), so even the "real" checkout path
   never calls the real payment API that exists.
4. Post-booking → **My Trips: BREAK #2**, permanent — the shipped
   navigation routes to a hardcoded five-trip fixture; the one real
   implementation has no way to reach it.
5. **Login is disconnected from end to end** — no code path in the
   consumer app ever establishes a real session, so every backend
   capability that depends on one (ownership, ownership-scoped My Trips,
   CSRF-protected actions) never actually activates for any user going
   through the shipped UI, regardless of how correctly built the backend is.

---

## 24. Capability matrix

| Capability | Required for Beta? | Classification | Real data? | Consumer-connected? | Provider-connected? | Persistence? | Frontend-connected? | Blocking gap | Recommended slice |
|---|---|---|---|---|---|---|---|---|---|
| Origin resolution | Yes | REAL & CONNECTED | Yes | Yes | N/A | N/A | Yes (search form) | — | — |
| Origin autocomplete | Nice-to-have | REAL & CONNECTED (backend) | Yes | No | N/A | N/A | No | UI not built | Origin Intelligence Slice 2 |
| Nearby airports | Nice-to-have | REAL & CONNECTED (backend) | Yes | No | N/A | N/A | No | UI not built | Origin Intelligence Slice 2 |
| Browser geolocation | Nice-to-have | NOT BUILT | — | — | — | — | No | — | Origin Intelligence Slice 2 |
| Destination catalog | Yes | REAL & CONNECTED | Yes (curated) | Yes | N/A | Static | Yes | — | — |
| Market Prior | No (safe when absent) | REAL & CONNECTED (algorithm); data ~0% | No | Indirect | N/A | Yes, sparse | No | Coverage, not code | Market Prior Fill (optional) |
| Price Memory | No (grows over time) | REAL & CONNECTED; 0 rows fresh | No initially | Indirect | N/A | Yes | No | Needs live search volume | — |
| Attractiveness | No (safe when absent) | REAL & CONNECTED (algorithm); 0 rows fresh | No initially | Indirect | N/A | Yes | No | Needs one Ops reseed action | — |
| Candidate Funnel | Yes (for live search) | REAL & CONNECTED | Yes | Yes (when live enabled) | N/A | No | Yes | Gated behind config | — |
| Explore/Exploit | Yes (for live search) | REAL & CONNECTED | Yes | Yes | N/A | No | Yes | — | — |
| Portfolio Diversity | Yes (for live search) | REAL & CONNECTED | Yes | Yes | N/A | No | Yes | — | — |
| Live flight search | **Yes, critical** | REAL & CONNECTED, **gated** | Yes | Yes | Yes (Duffel) | N/A | Yes | No credentials configured | Provision credentials (external) |
| LCC coverage | Depends on product promise | NOT BUILT | — | — | No | — | — | Structural, industry-wide | Future provider slice, unstarted |
| Baggage | Yes | REAL (live) / UNKNOWN (synthetic) | Yes on live path | Yes | Yes | N/A | Yes | — | — |
| Accommodation estimate | Yes | FIXTURE/SYNTHETIC always | No | Yes | No | N/A | Yes | Real hotel provider absent | Future accommodation slice, unstarted |
| Hotel inventory | **Yes, for a purchasable trip** | NOT BUILT | — | — | No | — | — | No real provider exists | Future accommodation slice |
| Ground transfer estimate | Yes | FIXTURE/SYNTHETIC always | No | Yes | No | N/A | Yes | — | — |
| Real transfer inventory | No (not promised explicitly) | NOT BUILT | — | — | No | — | — | — | — |
| Journey persistence | Yes | REAL & CONNECTED (localStorage) | Yes | Yes | N/A | Client-side | Yes | — | — |
| Account (backend) | Yes | REAL & CONNECTED | Yes | Yes (HTTP) | N/A | Yes | **No** | Frontend never calls it | Frontend-Backend Reconnection slice |
| Password recovery | Yes | NOT BUILT | — | — | — | — | UI theatre only | Missing entirely | New slice required |
| My Trips | Yes | REAL & CONNECTED (backend + `SavedTrips.tsx`) | Yes | Yes | N/A | Yes | **No** (fixture shown instead) | Navigation wiring | Frontend-Backend Reconnection slice |
| Checkout (commercial) | Yes | REAL & CONNECTED | Yes | Yes | N/A | Yes | Yes (`BookingExperience.tsx`) | — | — |
| Stripe authorization | **Yes, critical** | REAL & CONNECTED (backend); not called by frontend | Yes | Backend only | Yes (gated, no creds) | Yes | **No** | Frontend never calls payment API | Payment-Booking Coupling slice |
| Payment capture | Yes | REAL & CONNECTED, **Ops-only** | Yes | No (no consumer route) | Yes | Yes | No | Not automatic | Payment-Booking Coupling slice |
| **Payment → Booking** | **Yes, the central blocker** | **BUILT BUT DISCONNECTED** | Yes (each half) | No (not coupled) | N/A | Yes (each half) | No | **Zero callers of the orchestrator under `api/`** | **Payment-Booking Coupling slice** |
| Revalidation | Yes | REAL & CONNECTED | Yes | Yes | Yes | N/A | Yes | — | — |
| Multi-ticket issuance | Yes | REAL & CONNECTED | Yes | Yes | Yes (gated) | Yes | Partial (via `BookingExperience`) | No credentials | Provision credentials (external) |
| Recovery (booking) | Yes | REAL & CONNECTED (code + Ops) | Yes | Indirect | N/A | Yes | No (Ops only) | — | — |
| Cancellation/change | Yes | REAL & CONNECTED (Ops); consumer path unverified this audit | Yes | Partial | Yes | Yes | Unclear | Needs its own trace | Post-Booking UX slice |
| Refund | Yes | REAL & CONNECTED | Yes | Yes (consumer + Ops) | Yes | Yes | Unclear (frontend not traced for this) | — | — |
| Travel Pass | Yes | REAL & CONNECTED | Yes | Yes | N/A | Built on request | Yes | — | — |
| Invoice | Yes | REAL & CONNECTED (generation); BUILT BUT DISCONNECTED (download) | Yes | Metadata only | N/A | Yes, immutable | Partial | No PDF-serving route | Document Download slice |
| Credit note | Yes | Same as invoice | Yes | Metadata only | N/A | Yes | Partial | Same | Document Download slice |
| Transactional email | **Yes, critical** | **NOT BUILT** (no live provider code) | N/A | N/A | No | N/A | N/A | No adapter exists at all | Production Email slice |
| Account deletion/export | Yes (compliance) | NOT BUILT | — | — | — | — | — | — | Account Lifecycle slice |
| Retention | Yes (compliance) | NOT BUILT | — | — | — | — | — | — | Account Lifecycle slice |
| Observability | Yes (operational readiness) | NOT BUILT | N/A | N/A | N/A | N/A | N/A | No logging/metrics/alerts | Observability slice |
| Ops recovery | Yes | REAL & CONNECTED | Yes | N/A | Yes | Yes | Ops console only | No `DETOURA_OPS_TOKEN` here | Provision token (external, trivial) |

---

## 25. Critical path to Limited Beta (derived from evidence, not assumed)

The dependency order that actually falls out of the findings above:

```
1. Frontend↔Backend reconnection
   (Login must call the real auth API; "Your Journey" must render the real
   selected trip, not a fixture; My Trips navigation must point at the real
   SavedTrips implementation or a real-data-fed equivalent)
        ↓ (without this, nothing downstream is reachable by a real user
           regardless of how real the backend is)
2. Payment↔Booking coupling
   (wire run_paid_booking — or an equivalent real caller — into the actual
   confirm route; make the consumer checkout flow actually call the payment
   API that already exists)
        ↓ (without this, "real payment" and "real booking" remain two
           demos, not one product)
3. Document download
   (serve the PDF bytes that are already generated and persisted)
        ↓
4. Production email
   (write an actual live provider adapter — this is new code, not config)
        ↓
5. Real provider E2E
   (provision Duffel + Stripe test credentials; run the first genuine
   end-to-end paid booking)
        ↓
6. Account lifecycle / compliance readiness
   (deletion, export, retention, session purge — a real product requirement
   before real user data accumulates at any scale)
        ↓
7. Observability
   (at minimum: structured logging across the payment/booking path, a real
   health check, and some form of failure alerting — otherwise Beta support
   is reactive-only, dependent on a user reporting a problem)
        ↓
8. Release gate
   (EU legal/tax/package-travel review — external, and already correctly
   flagged as unstarted in every prior slice)
```

**Search data density (Market Prior/Attractiveness/LCC coverage)** and
**real accommodation/ground-transfer inventory** are deliberately placed
*outside* this critical path — the audit's own instruction (§26) not to
block on incomplete prior coverage applies here too: none of these gaps
prevent a Limited Beta launch on their own; they limit its quality, not its
existence, and can be improved after launch.

---

## 26. Work estimate (bounded slices, not calendar time)

| Blocker | Size | Why |
|---|---|---|
| Frontend↔Backend reconnection (Login, Your Journey, My Trips) | **MEDIUM** | Not new backend work — the APIs exist. Real frontend state-management/data-flow work across 3-4 screens, careful enough not to regress the parts that do work. |
| Payment→Booking coupling | **MEDIUM** | The orchestrator (`run_paid_booking`) already exists and is tested; this is wiring it to the real confirm route and the real frontend checkout flow, plus the adversarial testing this whole program's own history shows this class of change needs. |
| Document PDF download route | **SMALL** | The PDF bytes and the ownership-scoped read function already exist; this is one new route. |
| Production email provider | **MEDIUM** | Genuinely new code — a real adapter (SES/SendGrid/etc.) plus its own fail-closed test/live posture, following the exact pattern Stripe/Duffel already establish. |
| Real Duffel + Stripe Test Mode E2E | **EXTERNAL** | Blocked on credential provisioning, not code. |
| Account deletion/export/retention/session purge | **MEDIUM** | Real cross-cutting work touching every persistence module that holds user data; needs its own careful scope like every other Phase 6 slice. |
| Observability baseline | **MEDIUM** | Structured logging + one real health check + minimal alerting is achievable in one bounded slice; full production-grade observability is larger but not required to launch. |
| Market Prior coverage fill | **LARGE** (optional, not blocking) | Real acquisition at scale, plus its own cost/rate-limit budget decisions — a genuine, separate program, not a quick fill. |
| Real hotel/ground-transfer inventory | **LARGE** (optional, not blocking) | New provider integrations from scratch, no existing scaffolding beyond the deliberate placeholders. |
| LCC coverage | **LARGE / EXTERNAL** (optional, not blocking) | Structural, industry-wide; likely requires a different provider relationship, not just code. |
| EU legal/tax/package-travel/privacy review | **EXTERNAL** | Not an engineering task. |

---

## 27. Progress estimate

**Overall Limited Beta readiness: 25–35%.**

This is deliberately lower than "how much code exists" would suggest,
because the blockers found are not additive — they're **multiplicative** at
the point a real user actually tries to complete a trip. A user who can't
log in through the shipped UI gets zero value from a well-built backend
account system; a confirmed booking that collects no payment isn't a sale
regardless of how correct the multi-ticket issuance logic is.

| Category | Estimate | Why |
|---|---|---|
| Search | **55–65%** | Architecture and wiring are genuinely strong (Slice 1 + 1.5 closed this HEAD); held back by near-zero production data density and permanently-synthetic accommodation/transfer economics. |
| Consumer UX | **20–30%** | Individual screens are often real; the shipped *funnel* breaks at "Your Journey" and "My Trips," and login never connects at all — a real user cannot complete the intended path today regardless of backend quality. |
| Accounts | **15–25%** | Backend is close to complete; **zero** of it is reachable by a real user through the shipped app. |
| Commercial/Pricing | **55–65%** | Real, tamper-proof, correctly computed — held back by the "not commercial truth" placeholder economics and non-purchasable accommodation/extras. |
| Payment | **50–60%** as a standalone system; **effectively 0% as a coupled capability** | The payment system alone is mature; it does nothing for booking today. |
| Booking | **40–50%** | The orchestration engine is excellent; decoupled from payment, which is most of what "booking" needs to mean for a paid product. |
| Post-booking | **20–30%** | Real generation/persistence of documents and Travel Pass; undeliverable (no download, no real email) and unviewable (My Trips fixture). |
| Operations | **30–40%** | Real, well-scoped recovery tooling; almost no proactive observability to know when to use it. |
| Production/Compliance | **15–25%** | Sound deployment architecture at Beta scale; account lifecycle/compliance work essentially unstarted; external legal review unstarted. |

**Largest source of uncertainty in this estimate**: whether "Frontend↔Backend
reconnection" and "Payment↔Booking coupling" turn out to be as
contained as their current code suggests once actually attempted — both
areas depend on components (payment security, booking orchestration, the
account system) that have each individually passed multiple rounds of this
program's own adversarial review, which is a good sign, but the *coupling*
between them has never been built or tested at all, so its true size can
only be estimated, not measured, until the work begins.

---

## 28. Proposed roadmap reset (not started)

1. **Frontend↔Backend Reconnection Slice** — Goal: make Login call the
   real `/auth/login`; make the "Your Journey" drawer render the actually-
   selected trip; make "My Trips" navigation reach real data (either
   `SavedTrips.tsx` or a session-backed equivalent). Why required: nothing
   downstream matters to a real user until this exists. Dependencies: none
   beyond existing backend APIs. Expected classification changes: Accounts
   backend REAL & CONNECTED (unreachable) → REAL & CONNECTED (reachable);
   Consumer UX's two fixture walls BUILT BUT DISCONNECTED/FIXTURE → REAL &
   CONNECTED.

2. **Payment-Booking Coupling Slice** — Goal: wire the real checkout flow
   to call the payment API, and wire a successful authorization to actually
   gate/trigger booking confirmation via the existing (tested)
   `run_paid_booking` orchestration or an equivalent real caller. Why
   required: this is the single capability that turns "search + book" into
   "buy." Dependencies: Slice 1 (a session and a real checkout screen must
   exist to authorize payment from). Expected classification change:
   Payment↔Booking BUILT BUT DISCONNECTED → REAL & CONNECTED.

3. **Document Download Slice** — Goal: serve the already-generated,
   already-persisted PDF bytes via a real, ownership-checked route. Why
   required: a customer needs their receipt/invoice. Dependencies: none.
   Expected classification change: Invoice/credit-note download BUILT BUT
   DISCONNECTED → REAL & CONNECTED.

4. **Production Email Slice** — Goal: implement one real transactional
   email provider adapter, following the existing fail-closed test/live
   pattern already established for Stripe/Duffel. Why required: booking
   confirmation, payment, and recovery emails are part of the basic
   post-booking promise. Dependencies: none technical; needs a chosen
   provider + credentials (external). Expected classification change:
   Email NOT BUILT → REAL & CONNECTED (config-gated, like payment/search).

5. **Provision real credentials + first real E2E** — Goal: configure real
   Duffel Test Mode and Stripe Test Mode credentials, and run the first
   genuine paid, provider-issued booking end to end. Why required: nothing
   in this program has ever been verified against a real provider. External
   dependency: credential provisioning. Expected classification change:
   "Real Duffel/Stripe Test Mode E2E: NOT VERIFIED" → VERIFIED.

6. **Account Lifecycle Slice** — Goal: build deletion, export,
   retention-expiry, session purge. Why required: pre-Beta product/
   compliance readiness, independent of Phase 6 security (not reopened).
   Dependencies: none technical. Expected classification change: NOT BUILT
   → REAL & CONNECTED.

7. **Observability Baseline Slice** — Goal: structured logging across the
   payment/booking/provider path, one real dependency-checking health
   endpoint, minimal failure alerting. Why required: Ops recovery tooling
   is only useful if someone knows to use it. Dependencies: none. Expected
   classification change: NOT BUILT → REAL & CONNECTED (baseline, not
   full-featured).

8. **Release Gate** — Germany/EU legal/tax/package-travel/privacy
   specialist review. External; not an engineering slice.

*(Market Prior Coverage Fill, real hotel/ground-transfer inventory, and LCC
provider coverage are deliberately left off this critical-path list per the
audit's own instruction not to block Beta launch on data/inventory breadth
— they are real, larger, separate future programs.)*

---

## 29. Independent review

A fresh, skeptical reviewer (separate from this audit's author) attacked
every §29-required item, specifically instructed to lower estimates
wherever evidence didn't support them and to raise one only with concrete
executable evidence. Method: independent re-reading of the actual code (not
this document's citations), plus one independent read-only SQL query
against the real local database.

**Every load-bearing claim checked was independently confirmed, several to
exact numeric precision**: the payment↔booking decoupling (re-derived via
the reviewer's own grep and route-level reading, zero hits under `api/`);
accommodation/ground-transfer "always synthetic" (both `Real*Provider`
classes confirmed unconditional `NotImplementedError`); the Amadeus
BUILT-BUT-DISCONNECTED classification; all 5 spot-checked frontend claims
(Login's zero fetch calls **and** that `App.tsx:297` renders it with zero
handler props at all — a stronger finding than this document originally
stated; the "Your Journey" fixture; `Checkout.tsx`'s orphaned status
including its literal placeholder text; My Trips routing to the fixture
with `SavedTrips.tsx` confirmed unreachable via any nav element); Market
Prior coverage (independently recomputed against the real database: 10/0/0
rows across `market_priors`/`price_observations`/`destination_attractiveness`,
matching this document and the generated JSON artifact exactly); the fresh-
install bootstrap claim; every account-lifecycle NOT BUILT claim (via the
reviewer's own fresh grep); the commercial-policy "not commercial truth"
label and exact percentages; the email provider's both-branches-return-
sandbox code; the exact 20-entry/183-gap airport table count; the stale PDF-
serving TODO comment; and the 203/203 destination-image claim.

**One calibration nuance, not a credibility problem**, raised by the
reviewer and incorporated here: tracing `App.tsx` shows an anonymous user
*can* walk Discover → Results → "Your Journey" (fixture shown, but
`continueCheckout` passes the real `journeyTrip` state, not the fixture,
to what happens next) → `BookingExperience` (real, explicitly skips payment
by design) → `confirm_booking` (real, issues an actual Duffel Test Mode
order) — entirely without ever touching Login/My Trips, since anonymous
booking is the default real path (§6). So "breaks the funnel at its two
most trust-critical moments" is accurate about the *trust/visible*
experience a user has, but not literally "no one can complete an unpaid
booking" through the shipped UI — a nuance this document's own narrative
already gestured at ("a dead-end cosmetic step, not a data corruption")
without stating it this explicitly. Noted here for precision; it does not
change any classification, since payment itself is never invoked either
way (§10's verdict is unaffected).

**On the overall 25–35% estimate and its "multiplicative, not additive"
reasoning**: the reviewer computed a naive average of this document's own
per-category midpoints (≈38%) and confirmed the audit's actual 25–35%
range is a deliberate, defensible downward adjustment beyond simple
averaging for an end-to-end "can a real user complete a paid trip" metric
(a serial-reliability chain multiplies its stage probabilities; it does not
average them) — not a double-count or arithmetic error.

The reviewer explicitly searched for, and found no, place this audit
understated a real, reachable capability (specifically checked: Duffel
cancel/change wiring, the destination-image claim, and the Ops recovery
routes) — no estimate was raised.

**VERDICT: AUDIT CREDIBLE.**
