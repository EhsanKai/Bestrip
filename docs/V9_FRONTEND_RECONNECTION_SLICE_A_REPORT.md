# V9 Frontend Reconnection Slice A Report

Starting HEAD: `e8e8d95`

## Scope

Slice A connected the consumer frontend account/session path and the first real selected-journey path without backend architecture changes and without checkout/payment wiring.

## Frontend Files Changed

- `frontend/src/api/client.ts`
- `frontend/src/api/types.ts`
- `frontend/src/state/useAccount.ts`
- `frontend/src/state/journeyDraft.ts`
- `frontend/src/App.tsx`
- `frontend/src/screens/Login.tsx`
- `frontend/src/components/journey/JourneyDrawer.tsx`
- `frontend/src/screens/Results.tsx`
- `frontend/src/screens/Results.css`
- `frontend/src/components/shell/Header.tsx`
- `frontend/src/components/shell/MobileNav.tsx`

Several of these files already had local/prototype edits before this slice. No commit was created to avoid accidentally absorbing unrelated work in the same files.

## Backend Files Changed

None.

## Real Account Endpoints Used

- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/logout`
- `GET /api/v1/auth/me`

The frontend client now sends `credentials: "include"` and echoes the JS-readable `detoura_csrf` cookie as `X-CSRF-Token` for mutating auth calls when present. It does not store bearer tokens or raw credentials in browser storage.

## Real Search Endpoint Used

- `POST /api/v1/search`

The existing `useSearch` path was already using the real endpoint through `frontend/src/api/client.ts`. Slice A extended the typed contract for `diagnostics.supply_source`, top-level baggage, and total-with-known-baggage fields, then surfaced supply provenance in Results.

## Journey Model

`frontend/src/state/journeyDraft.ts` derives the Your Journey drawer model directly from the selected `TripRecommendation` plus non-sensitive search context:

- route/cities/stays from `trip.stays` and `trip.cities`
- dates from recommendation departure/arrival/stays
- travelers from the search request
- recommendation estimate totals from `total_price` or `total_with_known_baggage`
- baggage as backend note/display total when available, otherwise unknown
- supply/truth language from recommendation freshness/availability

No payable-now or booked-state truth is fabricated. Slice A renders checkout as not started and payable amount as not established.

## Journey Persistence

Mechanism: bounded browser persistence in `localStorage` under `detoura-journey-draft-v1`.

Stored data is a versioned pre-checkout journey draft containing the selected recommendation and minimal search context. It stores no auth token, no payment data, and no traveler PII. Restore validates version and required recommendation fields, removes corrupt/stale-shape data safely, and treats restored price as recommendation truth only. Checkout Slice B must revalidate server-side truth before purchase.

## Fixture Paths Removed From Production Path

The production Your Journey selection path no longer uses `journeyDrawerFixture`. Selecting a recommendation now creates a draft from that exact recommendation, opens the drawer, and preserves it across new searches.

## Remaining Fixture/Demo-Only Paths

- `frontend/src/components/journey/journeyFixture.ts` remains in the tree but is no longer used by the production selected-journey path.
- `frontend/src/screens/MyTrips.tsx` still contains presentation/demo trips. My Trips integration was explicitly out of Slice A except navigation compatibility.
- Other prototype visual work was already present in the working tree and was preserved.

## Backend Contract Findings

Backend supports:

- registration
- login
- logout
- session restoration/current user
- CSRF-protected authenticated mutation
- real `/api/v1/search`
- truthful `LIVE`/`SYNTHETIC` supply source on search diagnostics

Backend does not expose a pre-checkout selected-journey persistence endpoint. Slice A therefore uses bounded non-sensitive frontend draft persistence only.

## Closure Browser Flow Exercised

Closure pass runtime:

- Backend: `.venv/bin/uvicorn detoura.api.app:app --host 127.0.0.1 --port 8000`
- Frontend: `npm run dev -- --host 127.0.0.1`
- Browser: Codex in-app browser against `http://127.0.0.1:5173/`

Desktop viewport `1280x900`:

- Logged in through the real UI using the local throwaway account.
- Header changed to `Account ✓`.
- Ran Discover search against real `POST /api/v1/search`.
- Results rendered five real backend recommendations and the `SYNTHETIC` provenance note.
- Explored the first recommendation and verified the detail screen showed `Munich + Vienna`, matching the selected recommendation.
- Selected `Munich + Vienna` into Your Journey.
- Drawer showed `Munich to Vienna`, the matching dates/stays/costs, unknown baggage, and `Payable now: Not established`.
- Escape closed the drawer, focus returned to the triggering `Select journey` button, and body scroll was restored.
- Header Your Journey reopened the drawer; backdrop close worked.
- Performed another search; Your Journey remained `Munich to Vienna`.
- Explicitly selected `Vienna + Budapest`; Your Journey changed to `Vienna to Budapest`.
- Hard navigation/reload restored the `Vienna to Budapest` journey and the authenticated account state.

Mobile viewport `360x800`:

- Found and fixed a closure-pass mobile navigation regression: Account was missing from mobile bottom nav.
- Verified bottom nav now exposes `Discover`, `Journey`, `My Trips`, and `Account`.
- Logged out and logged back in through the 360px mobile UI.
- Ran Discover/search/results at 360px.
- Results remained usable and recommendation cards/actions were reachable.
- Opened Your Journey as a 360px sheet; it rendered full-width, near-full-height (`360px` wide, `736px` high in an `800px` viewport), scrollable, with no horizontal page overflow (`scrollWidth === clientWidth === 360`).
- Sticky primary `View journey` action was visible.
- Close button closed the sheet and restored body scroll.

Earlier local API smoke was also exercised against `.venv/bin/uvicorn detoura.api.app:app`.

- `GET /api/v1/auth/me` anonymous returned `200 null`.
- Invalid login returned `401` with generic invalid credentials.
- Register throwaway local account succeeded.
- Login set `detoura_session` HttpOnly cookie and `detoura_csrf` cookie.
- `GET /api/v1/auth/me` with cookies returned authenticated profile.
- CSRF logout returned `{"ok": true}` and cleared cookies.
- `/api/v1/search` zero-result path returned `no_results` and `supply_source: "SYNTHETIC"`.
- Documented synthetic scenario returned recommendations with real IDs such as `1-cgn-munich-vienna-cgn`, unknown baggage, and `supply_source: "SYNTHETIC"`.

## Runtime Accessibility Closure

Browser-verified:

- Keyboard focus can reach form fields and Journey controls.
- Drawer focus moves to the close control on open.
- Escape closes the drawer.
- Focus returns to the triggering `Select journey` button after Escape.
- Header/bottom-nav Journey controls open the drawer/sheet.
- Backdrop and close-button behavior do not trap the user.
- Body scroll is restored after close.
- Login invalid-email error is associated with the field by `aria-describedby="account-email-error"` and `aria-invalid="true"`.
- Buttons exposed meaningful accessible names in the browser tree, including `Open Your Journey`, `Select journey`, `View journey`, `Log in`, and `Log out`.

## Console / Network Closure

- Browser console warnings/errors captured during the flow: none.
- Backend logs during browser flow showed expected `200` responses for `/api/v1/auth/me`, `/api/v1/auth/login`, `/api/v1/auth/logout`, `/api/v1/origins/K%C3%B6ln`, `/api/v1/search`, and `/api/v1/events`.
- No unexpected browser-visible network failure, React runtime error, or 4xx/5xx regression surfaced. The only 4xx observed in this closure work was the intentional invalid-login smoke (`401`) from the prior API check.

## Journey LocalStorage Safety Closure

Runtime:

- Hard navigation restored the selected `Vienna to Budapest` journey, proving the browser draft restoration path works.
- The browser automation environment did not expose `window.localStorage`/`document.defaultView.localStorage` for direct inspection, and blocked `javascript:` URL injection for corrupt-payload testing. No workaround was attempted.

Code re-inspection:

- Persistence key is `detoura-journey-draft-v1`.
- Stored shape is versioned: `{ version, selectedAt, trip, searchContext }`.
- `searchContext` includes only `travelers`, `origin`, `dateFrom`, and `dateTo`.
- No auth token, session token, password, traveler party, payment, card, or Stripe data is written by the persistence code.
- `loadJourneyDraft()` parses JSON, validates version and required recommendation fields, removes invalid/corrupt payloads, and returns `null` safely.
- Restored price is rendered as recommendation/estimated trip truth only; checkout/payable amount remains `Not established`.

## Build / Lint / Test

- Build: `npm run build` PASS.
- Lint: `npm run lint` PASS with warnings.
- Tests: `npm test` FAILS because `frontend/package.json` has no `test` script.
- Frontend automated test infrastructure: NOT PRESENT.

Investigation found no frontend test runner/config/files (`vitest`, `jest`, `playwright`, `*.test.*`, `*.spec.*`). Per the closure brief, this does not block Slice A closure because build, lint, browser verification, and final review passed.

Observed lint warnings include existing fast-refresh/style warnings plus `react(set-state-in-effect)` warnings in `App.tsx`, `SearchProgress.tsx`, and `useAccount.ts` session restoration.

## Accessibility Result

Runtime browser checks passed for the Slice A surface. The existing Journey drawer dialog semantics, Escape close, focus return, backdrop close, and body-scroll restoration were preserved. The production prototype state switcher was removed from the drawer to avoid falsely activating checkout/payment states.

Desktop and 360px mobile browser verification are complete.

## Working Tree Provenance

Requested commands run:

- `git status --short`
- `git diff --stat`
- `git diff`

Classification:

- Pre-existing/prototype work: `frontend/index.html`, `frontend/public/favicon.svg`, `frontend/src/App.css`, `frontend/src/components/booking/TravelPass.css`, `frontend/src/components/booking/TravelPass.tsx`, `frontend/src/components/shell/Header.css`, `frontend/src/components/trip/JourneyPoster.css`, `frontend/src/components/trip/JourneyPoster.tsx`, `frontend/src/components/trip/RecommendationCard.css`, `frontend/src/components/trip/RecommendationCard.tsx`, `frontend/src/components/ui/Button.css`, `frontend/src/components/ui/Icon.tsx`, `frontend/src/design/base.css`, `frontend/src/design/tokens.css`, `frontend/src/main.tsx`, `frontend/src/screens/Discover.tsx`, `frontend/src/screens/Landing.tsx`, `frontend/src/screens/TripDetail.tsx`, `frontend/public/brand/`, `frontend/src/components/trip/CityGallery.css`, `frontend/src/components/trip/CityGallery.tsx`, `frontend/src/components/ui/BrandLogo.css`, `frontend/src/components/ui/BrandLogo.tsx`, `frontend/src/design/luxury.css`, `frontend/src/screens/MyTrips.css`, `frontend/src/screens/MyTrips.tsx`.
- Slice A integration work: `frontend/src/api/client.ts`, `frontend/src/api/types.ts`, `frontend/src/state/useAccount.ts`, `frontend/src/state/journeyDraft.ts`, `frontend/src/screens/Results.tsx`, `frontend/src/screens/Results.css`, `docs/V9_FRONTEND_RECONNECTION_SLICE_A_REPORT.md`.
- Mixed files containing both pre-existing/prototype work and Slice A integration/closure work: `frontend/src/App.tsx`, `frontend/src/components/shell/Header.tsx`, `frontend/src/components/shell/MobileNav.tsx`, `frontend/src/components/journey/JourneyDrawer.tsx`, `frontend/src/components/journey/JourneyDrawer.css`, `frontend/src/components/journey/journeyFixture.ts`, `frontend/src/screens/Login.tsx`, `frontend/src/screens/Login.css`.
- Excluded local/tooling files: `AGENTS.md`, `CLAUDE.md`.

Checkpoint disposition:

- A frontend baseline checkpoint is being created with the explicit intent to include the broader intentional frontend/prototype set plus Slice A integration as one coherent baseline.
- The checkpoint intentionally includes the current consumer frontend product state rather than trying to split Slice A hunks out of preserved prototype files.
- Category C local/tooling files remain excluded.
- No files were discarded, cleaned, reset, or stashed.

## Independent Review

Verdict: APPROVED.

Review checks:

- Fixture leakage: Your Journey production selection no longer uses `journeyDrawerFixture`.
- Fake prices: drawer shows estimated recommendation totals and does not show unknown baggage as zero.
- Fake journey: selected drawer uses the selected `TripRecommendation`.
- Auth token exposure: no session token storage; cookie session remains server-owned.
- PII persistence: journey draft stores no traveler party or payment data.
- Stale journey: restored draft is treated as pre-checkout recommendation truth, not revalidated fare.
- Navigation consistency: desktop and mobile use `myTrips`; mobile Account route was fixed and verified.
- Search preservation: new search clears transient result selection but does not clear Your Journey.
- Replacement: selecting another recommendation explicitly replaces the draft.
- Desktop behavior verified.
- 360px behavior verified.
- Runtime accessibility checks completed.

## Missing Backend Contracts

No blocker for Slice A.

Known missing future capability: server-side pre-checkout selected-journey persistence. Slice A does not require it because a bounded draft is acceptable for this non-sensitive pre-checkout state.

## Remaining Blockers For Slice B

- Checkout must revalidate selected recommendation server-side before payment.
- Payable-now/CheckoutSnapshot truth must come from backend checkout/payment contracts.
- My Trips remains fixture/demo UI until post-booking integration work.
