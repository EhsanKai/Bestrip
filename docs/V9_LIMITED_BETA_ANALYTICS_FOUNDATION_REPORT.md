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
