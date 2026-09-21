# V9 — Google Sign-In Consumer UI Integration

**Starting HEAD:** `3606f55` ("V9 close analytics foundation review gaps")
**Final HEAD:** recorded in this repository's commit log immediately following this report.

This slice connects the existing consumer Login/Signup screen to the already-complete Google Sign-In backend (`1e19341`, `docs/V9_GOOGLE_AUTH_ACCOUNT_LIFECYCLE_REPORT.md`). It is an integration task: the backend's OAuth/OIDC architecture, account-linking policy, and session model were not touched, and no new visual system was introduced. Zero backend files were modified.

---

## 1. Backend contract used (ground truth, not the report's summary)

Read directly from source before writing any frontend code:

- `src/detoura/api/auth_google.py` — the actual routes, exactly:
  - `GET /api/v1/auth/google/start` — 302 redirect to Google. No JSON, no request body. Rate-limited per IP.
  - `GET /api/v1/auth/google/callback?code=&state=` — Google's own redirect target. Always redirects the browser back to `GoogleAuthConfig.post_login_redirect_url` (a single, fixed, operator-configured URL — default `/`, **no per-request return-to parameter exists**), with query parameters:
    - `?google_auth=success` — cookies already set on this same redirect response.
    - `?google_auth=error&reason=missing_parameters|failed|link_conflict`
    - `?google_link_required=1&link_id=<opaque>` — Case C (§9/§10 below).
  - `POST /api/v1/auth/google/link/start` — authenticated + CSRF, returns `{"authorization_url": "..."}"` for Case F (an already-logged-in user proactively connecting Google). **Not wired to any UI in this slice** — see §"Out of scope" below.
  - `POST /api/v1/auth/google/link/confirm` — authenticated + CSRF, body `{"link_id": "..."}"`, returns `{"ok": true}` (400 on invalid/expired, 409 on `GoogleLinkConflict`).
- `src/detoura/google_auth_config.py` — `post_login_redirect_url` defaults to `"/"` and has no per-attempt variant; confirms the backend genuinely has no return-to protocol, so none was invented client-side (§8 of the brief: "if backend does not support safe return-to semantics, do not invent a client-side redirect protocol").
- `src/detoura/api/auth.py` — the pre-existing cookie/CSRF contract Google auth reuses unchanged: `detoura_session` (`HttpOnly`), `detoura_csrf` (JS-readable, double-submit), header `X-CSRF-Token`, `GET /api/v1/auth/me` for session restoration.
- `docs/V9_GOOGLE_AUTH_ACCOUNT_LIFECYCLE_REPORT.md` §4 "Frontend contract required" — cross-checked against the source above; matched exactly, no discrepancy found.

No endpoint name, field, or redirect behavior in this integration was invented or guessed.

---

## 2. Existing frontend inspected before changing anything

- `frontend/src/screens/Login.tsx` — the single auth surface (login/signup/forgot/reset-confirmation views), cookie-session driven, no client-side auth state persistence.
- `frontend/src/state/useAccount.ts` — session restoration: a mount-time `GET /auth/me` via `api.me()`. Unmodified. Google-authenticated sessions are picked up by this exact same mechanism with zero special-casing, because the backend issues the identical cookie pair either way (§6 of the brief).
- `frontend/src/api/client.ts` — the one place the frontend talks to the backend; `request()` already auto-attaches `X-CSRF-Token` from the `detoura_csrf` cookie on every non-GET call. Reused as-is for the one new call this slice adds.
- Observed, unrelated, **left untouched**: `Login.tsx`'s "forgot password" UI (`onForgotPassword`) has no handler wired in `App.tsx` today — clicking "Send reset email" shows a fake success state without calling the backend's (already-built, `docs/V9_GOOGLE_AUTH_ACCOUNT_LIFECYCLE_REPORT.md` §10) password-reset endpoint. This is a pre-existing gap, unrelated to Google Sign-In, out of this task's scope, and is called out here rather than silently left for someone to discover later.

---

## 3. What was built

| File | Change |
|---|---|
| `frontend/src/lib/googleAuthReturn.ts` (new) | Pure parsing of the backend's plain, non-secret return query parameters into a typed `GoogleReturnOutcome`; `clearGoogleReturnParams()` strips them from the address bar via `history.replaceState` (no reload). |
| `frontend/src/api/client.ts` | `googleAuthStartUrl()` — a fixed, same-origin path, never built from request-controlled input. `api.googleLinkConfirm({link_id})` — one `POST` through the existing `request()` helper (CSRF auto-attached, nothing new invented). |
| `frontend/src/screens/Login.tsx` | "Continue with Google" button + divider (login/signup modes only), a notice banner for the Case-C/error outcomes (login mode only — its copy is specifically about logging in), and two one-line acknowledgements ("Signed in with Google." / "Your Google account is now connected.") in the existing "Signed in" confirmation view. Inline Google "G" mark SVG (no external asset fetch). |
| `frontend/src/App.tsx` | One mount-effect reads the return outcome once and routes to the login screen; `continueWithGoogle()` does the full-page navigation; `loginWithPassword()` wraps the existing password-login call to complete a pending Case-C link exactly once, only on that one successful login. |
| `frontend/src/screens/Login.css` | New classes only (`.account-google`, `.account-divider`, `.account-google-notice`); no existing rule changed. |
| `frontend/scripts/verify-google-signin.mjs` (new) | Dependency-free runtime verification (see §"Frontend Tests"). |

---

## 4. Auth entry UX

Matches the approved hierarchy in §3 of the brief exactly: "Continue with Google" button, an "or" divider, then the unchanged email/password form. The button is a bordered secondary control (`var(--border-strong)`, `var(--bg)`), never filled with the Detoura accent color the primary submit button uses — Google is additional, not dominant. One button, one label ("Continue with Google"), covering both new-account creation and returning-identity login per the backend's own Case A/B policy — no separate "Sign in"/"Sign up with Google" was added, and no account-linking logic was duplicated client-side; the frontend only ever calls the backend's own endpoints and displays whichever outcome comes back.

**Login UI: REAL & CONNECTED. Signup UI: REAL & CONNECTED** (the same button/flow serves both, per backend policy — no separate signup-specific Google path exists or was needed).

---

## 5. Callback / return behavior & session restoration

The browser is sent to `googleAuthStartUrl()` via a real `window.location.href` assignment — a full navigation, never a `fetch` (Google only ever answers a browser redirect, and the backend explicitly owns the whole exchange: client secret, PKCE, state, nonce, token validation, and session minting all stay server-side; none of it exists in frontend code — verified both by code review and by the static checks in `verify-google-signin.mjs`, which assert no `client_secret`, `id_token`, `access_token`, or `accounts.google.com` string appears anywhere in the changed files).

On return, `App.tsx`'s mount effect classifies the outcome once, clears the query string, and switches to the login screen. For `google_auth=success`, the backend has already set the same session/CSRF cookie pair a password login sets — `useAccount()`'s existing mount-time `GET /auth/me` call (unmodified) picks this up with **no second, Google-specific auth state**. A Google-authenticated user is, from that point on, indistinguishable in frontend state from a password-authenticated one.

**Google Auth Initiation: REAL & CONNECTED. Google Callback: REAL & CONNECTED. Session Restoration: REAL & CONNECTED** (reuses the existing mechanism unchanged).

---

## 6. Return-to context (§8)

**NOT APPLICABLE, by design.** The backend has exactly one, fixed, operator-configured post-login URL (`GoogleAuthConfig.post_login_redirect_url`, default `/`) with no per-attempt parameter. Inventing a client-side "remember where I was and redirect there" protocol was explicitly disallowed by the brief when the backend doesn't support it, so none exists. The user always lands on `/`; the login screen shows the outcome there. This is a backend capability gap, not a frontend defect — documented, not worked around.

---

## 7. Same-email collision & explicit linking UX (§9/§10)

Approved language used verbatim in spirit: *"An account with this email already exists. Log in with your password below to verify it's you, and Detoura will connect your Google account."* Never claims an automatic merge, never says "we merged your accounts," never retries a different endpoint, never creates a second account (the backend already refuses that — the frontend just doesn't second-guess it).

The explicit link confirmation is real, not a frontend-only approximation: the `link_id` the backend's redirect provides is held in memory only (component state, not any storage) until the user's very next successful password login, at which point `App.tsx` calls the backend's actual `POST /auth/google/link/confirm` exactly once. A wrong password attempt does not consume it (the link id is untouched unless `account.login` itself succeeds first). No `provider_subject` or other internal detail is ever shown — the pending state carries only the opaque `link_id` the backend issued, format-validated client-side (`^[A-Za-z0-9_-]{1,64}$`, matching `persistence/accounts.py::new_link_id()`'s shape) before it is trusted into any UI state at all.

**Same-Email Collision: REAL & CONNECTED. Explicit Account Linking: REAL & CONNECTED** (Case F's proactive "connect Google from account settings" endpoint, `/link/start`, is not wired to any UI — see "Out of scope").

---

## 8. Google-only account UX (§11)

No frontend code assumes an account has a password. `MeResponse`/`AccountProfile` never carries a `has_password`-style field, and this slice adds nothing that reads or infers one. The pre-existing Login screen has no "change password" affordance at all (see §2's out-of-scope note on the unwired forgot-password flow), so there is no existing behavior to mislead a Google-only user, and none was added. No random/default password is invented anywhere in the frontend.

**Google-Only Account: NOT APPLICABLE** — there is no password-management UI in this codebase yet for either kind of account, so nothing needed correcting; the constraint was verified rather than assumed satisfied.

---

## 9. Password auth regression (§14)

`Login.tsx`'s `handleSubmit`, validation, and all existing modes (login/signup/forgot/resetRequested/passwordUpdated) are byte-for-byte unchanged except for new, additive JSX blocks gated on `isLogin`/`isSignup`. The only behavioral change to the login path is that `App.tsx` now passes `loginWithPassword` (which calls the exact same `account.login(email, password)` first, then does nothing further unless a Google link is pending) instead of an inline arrow calling `account.login` directly — identical behavior whenever no Google flow is in progress, confirmed by re-running the full build and reading the diff line by line.

**Password Auth Regression: NONE FOUND.**

---

## 10. Error UX & retry behavior (§12/§13)

Every backend outcome this integration can receive is mapped to a fixed, safe, understandable message — never the raw `reason` string, never `error.message` from a failed link-confirm call (only `error.status` is inspected, to distinguish 409/conflict from a generic failure):

| Backend outcome | User-facing message |
|---|---|
| `reason=missing_parameters` (no `code`/`state` — covers the user cancelling on Google's own consent screen) | "Google sign-in was cancelled." |
| `reason=failed` (expired state, invalid token, disabled account, unverified email, ... — the backend deliberately buckets these) | "We couldn't complete Google sign-in. Please try again." |
| `reason=link_conflict` | "This Google account is already connected to a different Detoura account." |
| any future/unrecognized `reason` | generic fallback, same as `failed` |
| `google_link_required=1` | the §9 explanation, above |
| link-confirm request throws | mapped by `status` (409 → conflict message, else → generic) |

No OAuth code, state, nonce, Google token, `provider_subject`, stack trace, or internal configuration name is ever rendered — confirmed both by code review and by `verify-google-signin.mjs`'s static checks. Retry is simply "click the button again" (no auto-retry, no redirect loop — the mount effect runs exactly once and never re-fires on subsequent renders).

**Error UX: REAL & CONNECTED. Retry UX: REAL & CONNECTED** (no automatic retry exists, by design).

---

## 11. CSRF & browser token storage (§15)

No new token storage mechanism was introduced. `api.googleLinkConfirm` goes through the exact same `request()` helper every other mutating call uses, which reads the pre-existing `detoura_csrf` cookie and attaches `X-CSRF-Token` automatically — nothing Google-specific was built or could be forgotten. Verified directly (not just read) via `verify-google-signin.mjs`, which asserts the outgoing request carries the header, the POST method, `credentials: "include"`, and that the `link_id` travels only in the JSON body, never a URL/query parameter.

Grepped every file this slice touches for `localStorage`/`sessionStorage`/token/secret patterns (also asserted programmatically in the verification script): none found. All Google-flow-transient state (`googleLinkId`, `googleNotice`, `googleRedirecting`, `googleJustSignedIn`, `googleLinked`) lives in ordinary React component state — gone on refresh, never serialized, never touching browser storage or the URL beyond the one-time query parameters the backend itself put there (which are stripped immediately).

**CSRF: REAL & CONNECTED, unchanged. Browser Token Storage: NONE** (verified).

---

## 12. Analytics interaction (§16)

**Zero analytics calls were added.** The existing typed taxonomy (`AnalyticsEventPayloads` in `lib/analytics.ts`, closed at `3606f55`) has no event for an auth interaction, and extending it would be redesigning infrastructure this task explicitly forbids touching. No `track()` call appears anywhere in this diff (confirmed via `git diff | grep track`). No consent is granted, checked, or referenced by any new code. Google-auth measurement is deferred to a future, explicit event-taxonomy slice, as instructed.

**Analytics Interaction: NOT BUILT (deliberately, per instruction) — no regression to existing analytics infrastructure.**

---

## 13. Accessibility (§17)

- The Google control is a real `<button type="button">` (not a link or div), with visible `:focus-visible` styling added to the same shared selector list every other interactive control in this screen already uses.
- `disabled={googleRedirecting}` — native disabled semantics (keyboard and pointer both blocked), no custom ARIA needed.
- The decorative "G" mark SVG is `aria-hidden="true"` / `focusable="false"`; the button's own visible text already says what it does.
- The notice banner has `role="status"` + `aria-live="polite"` and is linked to the button via `aria-describedby` (only when both are actually present in the DOM — fixed during review, see §"Independent review" finding below).
- The divider uses `role="separator"` with an `aria-label`, not a bare decorative `<hr>`-alike with no accessible name.
- No status is communicated by color alone: the error notice changes both border/text color **and** shows distinct text; the loading state changes both the button's `disabled` attribute **and** its visible label ("Connecting to Google…").

**Accessibility: VERIFIED** (static/semantic review; no screen-reader hardware pass was performed in this environment).

---

## 14. Responsive verification (§18)

No fixed widths were introduced. `.account-google` and `.account-divider` are ordinary full-width block elements inside the existing `.account-entry__panel`, which already collapses to a single column under the pre-existing `900px`/`560px` breakpoints — no new media query was needed or added, and none of the existing breakpoints were changed.

**Responsive: VERIFIED** via source/CSS review and `vite build`'s successful CSS bundling; see "Browser Verification" below for the honest limit of what was checked interactively.

---

## 15. Configuration / unavailable state (§19) & Real Google Provider E2E (§20)

`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`GOOGLE_REDIRECT_URI` remain unset in this environment (checked directly: `env | grep GOOGLE_CLIENT` returns nothing) — unchanged since the backend report's own classification. No credential was embedded, invented, or faked anywhere in this slice. The frontend does not (and, without a new backend capability-check endpoint, cannot) pre-detect Google's availability before navigating — clicking the button when unconfigured reaches the backend's own fail-closed `503` directly, which is safe (no security issue, no fabricated success) even though it is not a polished error page. Adding a dedicated "is Google configured" endpoint was considered and rejected as unnecessary backend scope expansion for this slice; if a nicer unavailable-state experience is wanted, that is a backend-contract addition for a future task, not something the frontend should approximate by guessing.

**Frontend integration: VERIFIED. Backend contract integration: VERIFIED. Real Google Provider E2E: BLOCKED BY CREDENTIALS** (unchanged from the backend report; no attempt was made to reach Google's real servers).

---

## 16. Frontend build / lint / tests / browser verification

- `npm run build` (`tsc -b && vite build`): **PASS**, no new errors or warnings; `Login` remains its own lazy-loaded chunk.
- `npm run lint` (`oxlint`): **PASS**, the same 8 pre-existing warnings as at `3606f55` (none in any file this slice touched), zero new warnings.
- No frontend test runner exists (confirmed: `package.json` has only `dev`/`build`/`lint`/`preview`). Added `frontend/scripts/verify-google-signin.mjs`, a dependency-free script using the same technique `verify-analytics.mjs` already established (TypeScript-compiler transpilation into an isolated `vm` context, no network, no real Google endpoint, no provider object): **PASS**. It exercises `googleAuthReturn.ts`'s outcome parsing and query-clearing against real inputs including malformed/injected `link_id` values and planted secret-shaped query parameters (none leak), `api.googleAuthStartUrl()`'s same-origin safety, `api.googleLinkConfirm()`'s actual HTTP method/CSRF header/credentials/body shape and its 409-vs-generic error mapping, and static source assertions (no `localStorage`/`sessionStorage`, no client-built Google URL, no token/secret reference, no polling/auto-retry near the link-confirm call).
- `verify-analytics.mjs` (from the prior slice) re-run and still **PASS** — confirms no analytics regression.
- **Browser verification**: the Vite dev server was started and every changed module (`App.tsx`, `screens/Login.tsx`, `lib/googleAuthReturn.ts`, `api/client.ts`) was confirmed to transform cleanly (HTTP 200, not 500) through Vite's dev transform pipeline, including a request to `/?google_link_required=1&link_id=test123` to confirm the server layer never chokes on the return query parameters. **No interactive/visual browser session (real click-through, screen-reader pass, or viewport-resize check) was performed** — no reliable browser-automation tool was available in this environment. This is an honest limit of verification, not a claim of interactive testing that did not happen; the functional claims above rest on TypeScript's structural checks, the runtime-logic verification script, and direct source review, not on having watched the button render in a real window.

**Frontend Build: PASS. Frontend Lint: PASS. Frontend Tests: PASS (lightweight, dependency-free). Browser Verification: PARTIAL** (server/transform-level only, no interactive/visual pass).

---

## 17. Independent review (§25)

A separate, adversarial read-only pass over the full diff, checked against every item in the brief's list:

- Silent email linking — none; Case C always requires the explicit backend-authorized confirm call, gated on a successful password login.
- Account takeover — the frontend never authorizes anything itself; every linking decision is re-validated server-side (`confirm_pending_link` re-checks the session's own email against the pending identity).
- Open redirect — `googleAuthStartUrl()` returns a fixed, same-origin string built only from `VITE_API_BASE` (an operator-configured build-time value), never from `location.search`, a referrer, or any other request-controlled input; the only other navigation call, `history.replaceState`, never changes what page is shown. Verified both by code review and by the verification script.
- OAuth/session token leakage — none found; no code path reads or stores `code`, `state`, `nonce`, `id_token`, or `access_token`.
- localStorage/sessionStorage auth leakage — none; verified both by review and by an automated grep-equivalent assertion in the verification script.
- Raw backend/OAuth errors — none surfaced; every message is a fixed, mapped string (see §10's table).
- Redirect loop — none; the return-outcome effect has an empty dependency array and runs exactly once per page load.
- Duplicate account creation — entirely backend-owned; the frontend adds no path that could influence it.
- Password auth regression — none (§9 above).
- Google-only/password confusion — not applicable; no password-management UI exists to be confused (§8 above).
- CSRF regression — none; the one new mutating call reuses the existing, unmodified CSRF mechanism.
- PII analytics leakage / accidental analytics enablement — impossible by construction; zero analytics calls were added (§12 above).
- Accessibility regression — one genuine, minor finding, fixed during this review (below).
- Mobile layout regression — none; no fixed widths, no new breakpoints, existing responsive grid inherited.

**Finding (fixed) — Low: `aria-describedby` could reference a non-existent element.** The Google button's `aria-describedby="google-notice"` was originally set whenever `googleNotice` was truthy, but the notice `<p id="google-notice">` itself was correctly scoped to render only in login mode (not signup) — so in signup mode with a lingering notice, the button would reference an id that doesn't exist in the DOM, an accessibility-tree dangling reference (assistive technology would silently find nothing, not a crash, but not correct either). **Fix**: `aria-describedby={isLogin && googleNotice ? "google-notice" : undefined}`, matching the notice's own render condition exactly. Verified via `npm run build` (no runtime error either way, this is a semantic-correctness fix, not a crash fix) and by re-reading both conditions side by side to confirm they now match exactly.

**No other Critical/High/Medium finding.**

---

## Classification summary

| Area | Classification |
|---|---|
| Backend Contract | VERIFIED (read from source, matched exactly) |
| Google Auth Initiation | REAL & CONNECTED |
| Google Callback | REAL & CONNECTED |
| Session Restoration | REAL & CONNECTED (existing mechanism, unmodified) |
| Login UI | REAL & CONNECTED |
| Signup UI | REAL & CONNECTED |
| Same-Email Collision | REAL & CONNECTED |
| Explicit Account Linking | REAL & CONNECTED (Case C only; Case F's `/link/start` not wired — out of scope) |
| Google-Only Account | NOT APPLICABLE (no password-management UI exists) |
| Password Auth Regression | NONE FOUND |
| Error UX | REAL & CONNECTED |
| Retry UX | REAL & CONNECTED (manual only, no auto-retry) |
| Return-To Safety | NOT APPLICABLE (backend has no return-to protocol; none invented) |
| CSRF | REAL & CONNECTED, unchanged |
| Browser Token Storage | NONE (verified) |
| Analytics Interaction | NOT BUILT (deliberate) |
| Accessibility | VERIFIED (one minor finding fixed) |
| Responsive | VERIFIED (source/CSS-level) |
| Frontend Build | PASS |
| Frontend Lint | PASS |
| Frontend Tests | PASS (lightweight) |
| Browser Verification | PARTIAL (server/transform-level only) |
| Real Google Provider E2E | BLOCKED BY CREDENTIALS |
| Backend Changes | NONE |

---

## Out of scope (documented, not built)

- **Case F ("Connect Google" from account settings)**: the backend's `POST /auth/google/link/start` is complete and correct, but this task's approved design (§3) scoped the work to the login/signup auth surface, not an account-settings screen (which does not exist yet in this codebase). Wiring `/link/start` into a future account-settings screen is a separate, small addition once that screen exists.
- **The pre-existing unwired "forgot password" UI** (§2) — a real gap, but unrelated to Google Sign-In and outside this task's ownership boundary; flagged rather than silently fixed or silently left undocumented.
