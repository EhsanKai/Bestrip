# V9 Limited Beta Analytics Foundation Report

## Starting HEAD

`1bebc95432e0d68fb4700c87d76ef4cbd984784c`

## Final HEAD

The checkpoint commit containing this report. The exact hash is recorded in the final handoff because embedding a commit's own hash would change the commit.

## Existing Analytics Before Slice

- `frontend/src/lib/analytics.ts` exposed a vendor-neutral-looking `track()` helper but could lazy-load GA4 or Plausible from environment configuration.
- `frontend/src/lib/funnel.ts` batched anonymous first-party funnel events to `/api/v1/events`.
- Product call sites emitted a mix of lowercase marketing-style events and uppercase first-party funnel events.
- Some payloads included unsafe or unnecessary analytics fields such as trip ids, total price, provider name, promo code, and free-form support summary text.
- No cookie banner, consent UI, CMP, GA/GTM tag in `index.html`, Meta Pixel, session replay, Hotjar, FullStory, or advertising SDK was present.

## Analytics Architecture

The frontend now has one Detoura analytics seam in `frontend/src/lib/analytics.ts`:

Product event -> typed Detoura analytics API -> sanitization, attribution, consent, dedupe -> configured adapters.

The old third-party vendor loaders were removed. Product components do not call `gtag`, `dataLayer`, `fbq`, `posthog`, or vendor SDK APIs. The deleted `frontend/src/lib/funnel.ts` behavior is represented as a first-party adapter that can translate a safe subset of the typed taxonomy into the existing `/api/v1/events` contract without backend changes.

## Event Taxonomy

Implemented typed event names:

- `landing_viewed`
- `search_started`
- `search_completed`
- `search_failed`
- `recommendation_selected`
- `journey_viewed`
- `checkout_started`
- `traveler_details_completed`
- `payment_authorization_started`
- `payment_authorized`
- `payment_failed`
- `payment_unknown`
- `booking_confirmation_started`
- `booking_confirmed`
- `booking_recovery_required`
- `booking_failed`
- `my_trips_viewed`
- `document_downloaded`

## Event -> Trigger Matrix

| Event | Trigger |
| --- | --- |
| `landing_viewed` | Landing screen first view |
| `search_started` | `useSearch.run()` or deep search starts |
| `search_completed` | `/search` returns a semantic success response |
| `search_failed` | Search request fails, normalized to a safe category |
| `recommendation_selected` | User selects a recommendation into Your Journey |
| `journey_viewed` | User opens a recommendation detail/journey view |
| `checkout_started` | Booking/checkout flow mounts for a selected journey |
| `traveler_details_completed` | Server accepts traveler party details |
| `payment_authorization_started` | User starts managed payment authorization |
| `payment_authorized` | Server returns payment status `AUTHORIZED` |
| `payment_failed` | Server returns `FAILED`/`CANCELLED`, or payment call fails |
| `payment_unknown` | Server returns `UNKNOWN`/`RECONCILIATION_REQUIRED` |
| `booking_confirmation_started` | User asks backend to confirm booking |
| `booking_confirmed` | Backend-confirmed self-service itinerary or travel pass ready |
| `booking_recovery_required` | Backend travel pass status is `recovery_required` |
| `booking_failed` | Backend booking failure or confirm call failure |
| `my_trips_viewed` | Authenticated My Trips list reaches ready state |
| `document_downloaded` | Financial document blob is successfully returned |

## Allowed Properties

Typed payloads allow only small, enumerated or numeric-safe fields such as search mode, recommendation source classification, bookable boolean, leg count, traveler count, currency, service tier, result count, backend semantic booking/payment state, normalized error category, document type category, viewport class, and normalized campaign/referrer fields.

## Forbidden Properties

The frontend analytics API does not accept arbitrary payloads at call sites. Removed or avoided fields include traveler/passenger names, emails, phones, dates of birth, passport data, trip ids, selected journey ids, booking ids, payment ids, provider references/order ids, Stripe/provider details, promo code text, raw API responses, raw error strings, support summaries, document numbers, document ids, auth/session/CSRF tokens, and financial document identifiers.

## Backend-Truth Events

`payment_authorized`, `payment_failed`, `payment_unknown`, `booking_confirmed`, `booking_recovery_required`, and `booking_failed` are emitted only from server-returned payment, booking intent, itinerary, or travel pass semantics. Button clicks emit only intent/attempt events such as `booking_confirmation_started` and `payment_authorization_started`.

## Attribution Strategy

The seam captures only an allowlisted standard campaign set:

- `utm_source`
- `utm_medium`
- `utm_campaign`
- `utm_content`
- `utm_term`

Values are trimmed, length-limited, stripped of unsafe characters, rejected when PII-shaped, and never executed. Referrer is reduced to origin only, and landing context is reduced to path only.

## Attribution Storage

Attribution uses `sessionStorage` under `detoura.attribution.v1`, with a 30-day max-age check and graceful no-storage fallback. It contains only normalized marketing attribution, referrer origin, landing path, and capture timestamp. It does not mix with journey, auth, traveler, payment, or booking state.

## Consent Architecture

The seam distinguishes `essential`, `analytics`, and `marketing` purposes. Optional analytics and marketing are gated by internal consent state. Because no consent UI or CMP policy is finalized in this slice, production optional analytics is off by default. Development diagnostics are essential-purpose, dev-only, and do not load or transmit to vendors.

## Third-Party Vendors

No third-party analytics, advertising, session replay, or CMP vendor was added. GA4 and Plausible lazy loaders were removed from the frontend analytics module.

## SEO Measurement Readiness

`landing_viewed` carries a landing context and the attribution architecture can distinguish future landing paths such as `/destinations/{slug}` without vendor-specific page code. No sitemap or robots changes were made.

## Funnel Coverage

The taxonomy can measure:

- visitor -> search
- search -> recommendation selection
- recommendation selection -> checkout
- checkout -> payment authorization
- payment authorization -> confirmation attempt
- confirmation -> confirmed booking
- recovery-required rate
- payment-unknown rate
- search failure rate
- booking failure rate
- My Trips usage
- document-download usage

Business KPIs are not computed in frontend code.

## Error Taxonomy

Frontend analytics normalizes errors to:

- `network`
- `validation`
- `authentication`
- `selection_expired`
- `payment_failed`
- `payment_unknown`
- `booking_failed`
- `recovery_required`
- `unknown`

Raw exception strings and backend error messages are not sent to analytics.

## Development Verification

Dev-only diagnostics logged `landing_viewed` in the local browser smoke. The live document included only local Vite scripts and no third-party tracker scripts.

## Performance Impact

The no-vendor production path does not load third-party scripts and does not block rendering, search, checkout, payment, booking, or My Trips. Analytics adapter failures are caught and ignored.

## Frontend Build

`npm run build` passed.

## Frontend Lint

`npm run lint` passed with existing warnings unrelated to this slice.

## Frontend Tests

No frontend test script exists in `frontend/package.json`.

## Browser Verification

Local Vite smoke at `http://127.0.0.1:5173/?utm_source=beta&utm_medium=test&utm_campaign=v9` rendered the landing page. Browser inspection showed only local Vite scripts and a dev analytics diagnostic for `landing_viewed`. The browser automation sandbox did not expose Web Storage to Playwright, and the app handled storage denial without crashing.

## Remaining Marketing/Analytics Work

- Decide Limited Beta consent policy and UX.
- Decide whether/when first-party `/events` should be enabled in production.
- If first-party backend reporting should use the new lowercase taxonomy directly, update the backend whitelist in a separate backend-owned slice.
- Select any future vendor only after privacy, consent, hosting/data residency, acquisition attribution, product analytics, and operational complexity review.
- Add focused frontend tests if a frontend test runner is introduced.

## Independent follow-up — narrow gap closure (2026-09-21)

This section supplements, and corrects where noted, the historical report above.
Original audit baseline: `1bebc95432e0d68fb4700c87d76ef4cbd984784c`.
Reviewed / follow-up starting HEAD: `b249ae8e71bb50ac7e6f02cf93290d2103571dd8`.
Final HEAD: the follow-up checkpoint containing this section; exact hash in handoff.
Concurrent backend Google Auth / Account Lifecycle changes were left untouched and excluded from staging.

### Original finding classification at b249ae8

| Finding | Classification at reviewed commit | Code evidence and follow-up |
| --- | --- | --- |
| A: GA4/Plausible before consent | FIXED | Loader paths removed from `lib/analytics.ts`; provider env alone cannot load a vendor. Retained. |
| B: immediate first-party identifiers | FIXED | `ensureKeys()` is reached only from a permitted first-party event. Follow-up additionally clears identifiers on denial/revocation and handles storage access failure. |
| C: feedback free text | PARTIALLY FIXED | `ReportIssueButton.tsx` removed analytics summary, but `errorTracking.reportIssue()` still logged summary/raw context. Follow-up removes that telemetry path. User-requested support report stays intact. |
| D: raw promo code | FIXED | Unsafe funnel call removed from `BookingExperience.tsx`. Follow-up restores only `promo_applied {tier, applied:true}` after server acceptance. |
| E: full search error request | NOT FIXED | `useSearch.ts:130,178` still supplied `{request}`. Now supplies only allowlisted operation; `captureException()` reduces errors to categories before any adapter sees them. Saved recheck IDs also removed. |
| F: UTM/referrer attribution | PARTIALLY FIXED | Added, but persisted before grant; accepted arbitrary paths, URL-shaped labels and invalid expiry. Follow-up gates storage, validates readback and uses route/referrer categories. |
| G: purpose gate | PARTIALLY FIXED | Event creation gated, but queued delivery survived revocation and attribution was unconditional. Follow-up drops queue/storage on denial and checks consent again at flush. |
| H: backend truth | PARTIALLY FIXED | Payment/booking catches fabricated FAILED; self-service readiness became BOOKED; recovery mapped to FAILED. Follow-up removes those mappings and uses explicit non-terminal events. |
| I: SPA measurement | FIXED | Explicit screen/actions rather than synthetic pageviews. Follow-up fixes globally rank-based suppression and mount-effect dedupe. |
| J: future SEO routes | PARTIALLY FIXED | `landing_context` allowed only home and arbitrary paths could leak identifiers. Follow-up adds destination/inspiration context enums and path templates without creating routes. |

### Architecture and production delivery truth

`funnel.ts` was deleted by b249ae8. This follow-up preserves the existing single
`analytics.ts` seam and its first-party adapter; it does not restore a competing
transport or install any vendor. `analyticsPayloads.ts` is solely a runtime
schema validator used by that seam, with per-event property and enum allowlists.

`/api/v1/events` remains the batched first-party destination (or the configured
API base). Delivery requires BOTH `VITE_ANALYTICS_FIRST_PARTY === "true"` at build
time AND an explicit literal `analytics: true` decision through `init({consent})`
or `setAnalyticsConsent()`. `main.tsx` calls `init()` without such a decision;
there is no production consent UI/caller. **Current production measurement is
DISABLED and not reachable through the shipped UI, even with the env flag.**
This remains an explicit Limited Beta Consent/UI integration gap, not permission
to enable tracking. The infrastructure can accept a future approved decision
without vendor knowledge in components. No grant is inferred or persisted.

Essential diagnostics are development-only and receive sanitized payloads.
Analytics and marketing are distinct boolean purposes, default false. Marketing
has no delivery adapter. This technical implementation makes no legal
classification or approval claim for first-party or external processing.

### Corrected event/trigger and legacy mappings

| Normalized event | Exact trigger | First-party legacy delivery |
| --- | --- | --- |
| search_started | Search/deepen action | SEARCH |
| search_completed | Successful backend search response, including no results | RESULT_VIEW |
| journey_viewed | Explicit detail-open action, no rank-global suppression | TRIP_OPEN |
| service_tier_selected | Continue from service selection; carries selected tier | TIER_SELECTED |
| promo_applied | Backend accepts requested promo; only applied=true and tier | PROMO_APPLIED with outcome=applied |
| traveler_details_completed | Backend accepts traveler submission | REVIEW |
| booking_confirmation_started | Confirm action | CONFIRM |
| payment_authorization_started | Authorize action | None |
| payment_authorized | Backend payment AUTHORIZED | None |
| payment_unknown | Backend UNKNOWN or RECONCILIATION_REQUIRED | None |
| payment_failed | Backend FAILED or CANCELLED | None |
| payment_request_failed | Payment request throws; no semantic payment state | None |
| booking_confirmation_failed | Confirmation/itinerary request throws; no semantic booking state | None |
| self_service_ready | Successful backend itinerary response | None; NEVER BOOKED |
| booking_confirmed | Backend TravelPass.status is ready | BOOKED |
| booking_recovery_required | Backend TravelPass.status is recovery_required | None; NEVER FAILED |
| booking_failed | Backend TravelPass.status is explicitly failed | FAILED |
| document_downloaded | Blob received and browser download initiated | None |

A browser download event does not prove the file was saved by the OS. Managed
TravelPass.ready is server-grounded (backend checks confirmed journey and
captured payment); this is not a separate capture event or proof of live versus
sandbox ticket issuance. Unknown future pass statuses do not emit failure.
Authorization is not booking, and request failure is not a terminal outcome.

Landing, recommendation selection, checkout mount, My Trips, search failure,
attribution and the other unmapped events remain available at the typed seam /
development diagnostics only. They are not delivered to the legacy backend.
The backend event/property allowlists cannot represent all newer semantics;
changing those is a separate backend-owned task. Discover-start and individual
recommendation-impression coverage are still absent. The report's earlier broad
funnel-coverage claim must not be read as complete production measurement.

### Data minimization and error boundary

`analyticsPayloads.ts` rejects unknown events, strips non-allowlisted keys,
validates enum values and bounded integer counts, and rejects malformed terminal
state fields. Extra strings/objects cannot bypass privacy merely through a TS
cast. Only selected currency codes are accepted; unsupported codes are omitted.
No prices, raw errors, request/response objects, passenger details, promo text,
auth/CSRF/session tokens, or provider/financial-document identifiers are sent.

`useSearch` and `useSaved` now pass only safe operation categories into
`captureException`. The error adapter receives an error category and a freshly
allowlisted operation object, never the original Error or its message/stack.
Console diagnostics are dev-only. Explicit user-visible support report content
is separate from telemetry and is still available for copying/emailing.
Booking/payment IDs appear only in internal dedupe keys: never serialized,
persisted, logged, or attached to delivered props.

### Attribution, identifiers, permission and revocation

Attribution parsing/persistence occurs only when analytics becomes permitted.
Only the five standard UTM keys are considered; parsing is capped at 4096 query
characters. Labels must be <=80 characters, lowercase normalized ASCII labels
starting with a letter, containing letters/digits/underscore/hyphen. URLs,
encoded payloads, HTML, email-shaped text and long digit strings are rejected.
These syntactic rules cannot certify arbitrary advertiser labels as nonpersonal;
future campaign governance must exclude personal labels/codes.

Referrer is reduced to internal/external for HTTP(S), never host, full URL,
query or fragment. Public paths are reduced to `/`, `/destinations`,
`/destinations/:slug` or `/inspiration/:slug`; private/unknown paths are omitted.
Future SEO components can use enum landing contexts through the same seam.
No SEO route, sitemap, robots, or product navigation change was made.

Attribution is stored in sessionStorage `detoura.attribution.v1`, bounded to
2048 serialized characters on read with finite, non-future, <=30-day age. Values
are revalidated at delivery. Storage lives with the tab, expires after that max
age, and is cleared when analytics is denied/revoked. With an explicit allowed
initial policy, a valid existing campaign is retained across a reload without
new campaign fields. With the shipped default-denied init, prior optional state
is cleared. No cookies or multi-touch engine were added.

First-party identifiers `detoura.fk.s` (sessionStorage) and `detoura.fk.v`
(localStorage) are created lazily at the first permitted, mapped event. Existing
keys must match generated 24-character hex format. The visitor key remains
persistent while permitted, with no separate expiry. Denial/revocation clears
both keys and attribution where browser storage permits; denied storage cannot
be forcibly cleared, but cached identifiers and pending events are discarded.

Revocation clears the queue/timer and cached keys immediately. Every flush also
rechecks analytics permission. Requests already handed to the browser cannot be
unsent; no queued optional event is intentionally retained for later replay.
Beacon rejection/throw falls back to caught fetch; denied storage and telemetry
exceptions do not change product state or break handlers.

### Deduplication and remaining limits

Deduplication is in-memory, per adapter, after purpose eligibility. Development
logging does not consume the first-party delivery key. Checkout/My Trips use
mount-scoped weak references, resilient to rerenders/StrictMode effect repeats;
journey-open action events no longer collide across different rank-1 results.
Terminal keys use backend entity + semantic state locally. Starts are action
counts. Document downloads suppress concurrent attempts while allowing later
intentional downloads. No booking truth is persisted for measurement.

Dedupe sets cap at 4096 entries per scope/adapter; further keyed events are
silently dropped until scope/session reset, rather than emitting duplicates.
Consent revocation resets observational state. Reloads and later grants may
count a newly observed backend state again: this is frontend-session measurement,
not an authoritative cross-session transaction ledger.

Remaining Medium limitations: no consent UI/approved policy; unmapped events and
attribution have no backend delivery; already-mounted views are not replayed on
a future grant; incomplete discover/impression coverage. Do not create fictional
legacy success/failure mappings to conceal these limits.

### Verification and independent review

- `npm run build --prefix frontend`: PASS.
- `npm run lint --prefix frontend`: PASS with nine existing warnings (React effects/exports and an unused ops catch binding); no new warnings.
- No npm frontend test script/test runner exists. Added a dependency-free focused verification command using the already-installed TypeScript compiler: `node frontend/scripts/verify-analytics.mjs`.
- Focused checks PASS: no full search context/raw error/support text; production default denial even with configured vendor/first-party flags; distinct marketing permission; literal true grants; no pre-grant attribution/identifiers; revoked queue/timer/pagehide/visibility flush; storage denial and beacon fallback; runtime extra-property rejection; invalid/bounded attribution; dedupe; actual checkout handler execution with mocked authoritative responses and request failures.
- The checkout harness executes production handler source with mocked React state/backend APIs; only the render return is replaced to expose handlers. FAILED/CANCELLED, UNKNOWN/RECONCILIATION_REQUIRED, AUTHORIZED, ready/recovery/failed and request exceptions are tested separately. No Stripe or Duffel object, order, payment or real backend mutation is created.
- Browser smoke at localhost:5174 with UTM parameters rendered landing and Discover while optional analytics was denied. City lookup was unavailable in this frontend-only environment; full search/checkout/provider E2E was not performed. Storage/delivery edge cases above were verified in isolated runtime mocks, not claimed as browser network capture.
- Independent read-only review found the original gaps and reviewed the follow-up. No remaining Critical/High code defect found; the deliberate production activation gap remains unresolved, as requested.
- No new dependency or tracker. Existing adapter architecture retained. No numerical performance speedup claim; no comparative benchmark was run for this correctness follow-up.

**Analytics Infrastructure: READY** for purpose-gated use within the documented
legacy delivery limits. **Production Measurement: DISABLED. Consent UI: NOT
BUILT. Consent Policy: NOT APPROVED.** Technically ready is not legal/consent
policy approved.

## Handoff verification (this session, 2026-09-21)

Codex #2 hit its usage limit with the section above already written but
uncommitted, alongside its `frontend/scripts/verify-analytics.mjs`. This
session took over the interrupted, uncommitted work without discarding or
redoing it, classified every dirty hunk against `b249ae8`, and independently
re-verified the claims above before committing.

**Classification of the interrupted diff** (`b249ae8` → handoff): every hunk
in `analytics.ts`, `analyticsPayloads.ts` (new), `errorTracking.ts`,
`BookingExperience.tsx`, `MyTrips.tsx`, `useSearch.ts`, `useSaved.ts` was
**CORRECT AND COMPLETE** for the five defects this follow-up targets. One
`App.tsx` hunk (`journey_viewed`'s `dedupeKey` removed) was checked
specifically because it looked like a possible dedup regression: it is not —
`openTrip`/`acceptReoptimized` are click handlers, never effects, so no
re-render/poll can re-invoke them, and per-rank-forever suppression in the
prior code was itself the defect (it silently dropped a second, legitimate
view of the same trip within one session). No hunk was classified INCORRECT
or UNRELATED; nothing was rewritten.

**Independently reproduced, not just re-read:**
- `npm run build` (`frontend/`): PASS, clean `tsc -b && vite build`.
- `npm run lint` (`frontend/`): PASS, the identical nine pre-existing warnings
  (`useAccount.ts`, `SearchProgress.tsx`, `Icon.tsx`, `opsApi.ts`,
  `opsFormat.tsx` ×3, `main.tsx`, `App.tsx:82` — the last is the pre-existing,
  untouched screen-transition effect, not anything in this follow-up's diff),
  zero new warnings.
- `node frontend/scripts/verify-analytics.mjs`: inspected for safety first
  (transpiles real source through the TypeScript compiler API into an
  isolated `vm` context with in-memory storage/fetch/beacon mocks; makes no
  real network call, writes no file, creates no provider object) and then
  run: **PASS**.
- Full `git diff b249ae8` read line by line for every file listed above, plus
  every `track(`/`captureException(` call site in the current tree (not only
  the ones the diff touched), specifically hunting for: attribution written
  before consent (none found — `captureAttribution()` has exactly one call
  site, inside `setAnalyticsConsent`'s just-granted branch), a stale queue
  surviving revocation (none — `setAnalyticsConsent` clears queue/timer/keys
  synchronously, and `flushFirstParty` independently re-checks consent),
  request failures mapped to `payment_failed`/`booking_failed` (none — the
  poll loop's own network-exception `catch` emits nothing, and the narrowed
  `tp.status === "failed"` check replaced the old catch-all `else`), booking
  or payment IDs reaching a delivered payload (none — every ID lives only in
  a `dedupeKey` string, never in `props`), and any reachable path to
  auto-grant consent or load a third-party tracker (none — `main.tsx` calls
  `initAnalytics()` with no consent argument and no other call site in the
  tree calls `setAnalyticsConsent`; grepped the whole frontend for
  GA4/Plausible/Hotjar/FullStory/Sentry/Mixpanel/Segment/Amplitude/PostHog/
  pixel identifiers — the only hits are `errorTracking.ts`'s pre-existing,
  unwired `VITE_SENTRY_DSN` dev-warning, unchanged by this follow-up).
- Confirmed zero backend files touched (`git status` shows only the
  `frontend/**` and this report path; no `src/detoura/**` or `tests/**`
  entry).

**No additional Critical/High/Medium defect found.** The independent review
requested by this handoff did not surface anything beyond what the
interrupted session's own section above already discloses (the production
consent-UI gap, unmapped-event legacy-delivery gap, and no-replay-on-later-grant
limitation) — none of which are defects in this slice's own claimed scope.

**Original Analytics Baseline:** `b249ae8e71bb50ac7e6f02cf93290d2103571dd8`
**Handoff HEAD:** `1e193414f664beb753917354a68bca090ea0110a` (Google Auth backend, unrelated, already committed)
**Final HEAD:** recorded in this repository's commit log immediately following this report update.
