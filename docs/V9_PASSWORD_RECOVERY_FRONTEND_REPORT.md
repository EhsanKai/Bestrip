# V9 — Consumer Password Recovery Frontend

**Starting HEAD:** `cca5fe2` ("V9 connect Google Sign-In consumer flow")
**Final HEAD (pre-commit):** `cca5fe2` + this task's own commit, frontend-only paths.

This slice connects the existing, already-verified password-reset backend
(`docs/V9_GOOGLE_AUTH_ACCOUNT_LIFECYCLE_REPORT.md` §10) to the existing
consumer auth UI (`frontend/src/screens/Login.tsx`). No backend file was
read for editing purposes beyond inspection, and none was changed. No
authentication redesign, no Google Sign-In change, no account-settings
surface was built — this is exactly the narrow account-recovery slice
described.

---

## Backend Contract

Inspected directly (`src/detoura/api/auth_account.py`,
`src/detoura/services/password_service.py`,
`src/detoura/services/password_hashing.py`) rather than assumed from the
report doc alone:

- `POST /api/v1/auth/password/reset/request` — anonymous, body `{ email }`,
  always returns `200 { message: GENERIC_RESET_REQUEST_RESPONSE }` regardless
  of whether the account exists, is disabled, or is rate-limited.
- `POST /api/v1/auth/password/reset/confirm` — anonymous, body
  `{ token, new_password }`, returns `200 { ok: true }` on success or `400`/
  `429` on failure. No new session is issued; every existing session for the
  account is revoked.
- **The reset "token" is not a URL.** `request_password_reset` composes a
  plain-text email body containing `"Reset code: {raw_token}"` — an opaque
  `secrets.token_urlsafe(32)` string — and nothing else. There is no reset
  link, no query parameter, no clickable URL anywhere in the backend's own
  contract. This is a real, load-bearing deviation from the brief's assumed
  "reset link" shape (§5/§6 of the task), not an invented one: the frontend
  cannot construct a link the backend never emits without changing backend
  email content, which was out of scope. See "Reset Link Contract" below for
  what was built instead.
- Password policy: min 8 characters, max 256 UTF-8 bytes
  (`password_hashing.py::MIN_PASSWORD_LENGTH`/`MAX_PASSWORD_BYTES`).
- Rate limits, single-use token consumption, and non-enumeration are all
  enforced server-side and were not touched.

## Forgot Password Entry

`Login.tsx` already had a "Forgot password?" link and a `forgot` mode wired
into its local state machine from a prior slice, but `onForgotPassword` was
never passed from `App.tsx` — the request silently no-op'd. Fixed by wiring
`onForgotPassword` in `App.tsx` to `api.requestPasswordReset({ email })`.
The control sits below the password field, never visually promoted above
"Log in" or "Continue with Google".

## Reset Request

`POST /auth/password/reset/request` with only the email field, matching the
real backend body. Primary action reads "Send reset email" (accurate: the
backend does send an email — it just contains a code rather than a link, see
below). Loading state "Please wait..." (shared with other modes' loading
copy, already established in this component). `canSubmit` returns `false`
whenever `submitState === "loading"`, and the submit button is `disabled`
whenever `canSubmit` is false — duplicate submission during an active
request is structurally prevented, not just discouraged.

## Enumeration Safety

The post-submit confirmation screen reads: *"If an account exists for that
email, we've sent a reset code."* — never reveals account existence,
Google-only status, or any identity detail. Verified statically (the new
`verify-password-recovery.mjs` asserts this exact non-enumerating phrasing
is present and that no "account does not exist" phrasing exists anywhere in
`Login.tsx`).

## Reset Link Contract

There is no reset *link* to implement — the backend contract is a manually
copy-pasted **reset code**, not a URL-borne token. Consequently:

- No URL parameter is ever read for this flow (unlike Google Sign-In's
  `google_auth`/`link_id` params in `lib/googleAuthReturn.ts`, which are
  untouched).
- No `window.history.replaceState` scrubbing was needed, because nothing
  sensitive ever reaches the address bar in the first place — a stronger
  position than a URL-token design would have been in, not a weaker one.
- The new `resetConfirm` screen (reachable via "I have a reset code" from
  the post-request confirmation screen, or directly if the user already has
  a code) asks for the code as a plain form field alongside the new
  password, then `POST`s both to `/auth/password/reset/confirm`.

This is a deliberate implementation of the actual backend contract, not an
invented shortcut — documented here per §5/§18's "do not invent, and stop
and document a genuine contract mismatch rather than build around it."

## Token Handling

- The reset code lives only in a single React `useState` string
  (`resetCode`) for the lifetime of that screen.
- Never written to `localStorage`/`sessionStorage`/any browser storage
  (verified statically).
- Never sent to analytics or error instrumentation (verified statically —
  `lib/analytics.ts` and `lib/errorTracking.ts` were not modified and
  contain no password-recovery-shaped taxonomy; `Login.tsx` never calls
  `captureException`).
- Never appears in a URL, so there is nothing to remove from browser
  history.
- Cleared from state (`setResetCode("")`) on `switchMode`, e.g. when the
  user backs out to login or requests a new code.

## New Password UX

Fields: **New password** / **Confirm new password**, added to the existing
`PasswordField` component (same show/hide toggle, same styling, same
`autoComplete="new-password"` convention already used at signup — no new
component was built). Client-side checks (min 8 chars, match confirmation)
mirror the signup form's existing checks exactly; the backend remains
authoritative (a password that passes client checks but somehow fails
server policy still surfaces the server's rejection through the normal error
path).

## Password Policy

Min 8 characters — matches `password_hashing.py::MIN_PASSWORD_LENGTH`
exactly (the same constant already used by the signup form; no new number
was invented). The backend's byte-length ceiling (256 UTF-8 bytes) is not
separately enforced client-side, consistent with how the pre-existing
signup form already handles it (no client-side max at all) — this task
did not add a new gap.

## Success State

`Login.tsx`'s existing `ConfirmationView` now shows *"Your password has been
updated."* with a **"Sign in"** button back to the login form, shown only
after `onConfirmResetPassword` resolves without throwing — i.e., only after
the backend's `200 { ok: true }`. No credentials are auto-submitted; the
backend explicitly issues no new session on reset (§12), so there is nothing
to log the user into automatically even if that were desired.

## Expired/Invalid/Used Token

The backend deliberately never distinguishes *why* a token failed (`This
reset link is invalid or has expired.` covers not-found, expired, and
already-consumed alike — §10 "single use"). The frontend mirrors this
exactly rather than inventing a finer-grained distinction: one generic
message, *"This reset code is invalid or has expired. Request a new one."*,
for every `400` from confirm. A `429` gets "Too many attempts. Try again
later."; a network failure (`status 0`) gets the same generic
connectivity message every other screen uses. A safe route back
("Request a new code") returns to the request-reset form. No automatic
retry loop exists anywhere in this flow.

## Session Semantics

Not touched. `password/reset/confirm` clears any stale auth cookie
(`_clear_auth_cookies`, pre-existing backend behavior, unchanged) and issues
no session — the frontend does not call `account.refresh()` or otherwise
assume a session exists after reset; the user reaches the ordinary login
form and authenticates fresh, exercising the pre-existing login path
unmodified.

## Google Auth Regression

`lib/googleAuthReturn.ts` was not touched. The Google button and its notice
banner remain gated to `isLogin || isSignup` exactly as before — the new
`forgot`/`resetConfirm` modes never render it. Re-ran
`scripts/verify-google-signin.mjs` after this change: **PASS**, unchanged.

## Password Auth Regression

`onLogin`/`onSignup`/`onLogout` wiring in `App.tsx` is untouched. The only
shared code touched in `Login.tsx` is `accountErrorMessage`, which gained a
`mode` parameter — its behavior for every existing mode (`login`, `signup`)
is byte-for-byte identical to before (verified by reading the diff: the
non-`resetConfirm` branch is the original function body, unindented).

## CSRF

No change. Both reset endpoints are anonymous on the backend (no
`require_csrf` call in `auth_account.py`) and the frontend does not attempt
to attach one — `request()` in `api/client.ts` only ever sends
`X-CSRF-Token` when a CSRF cookie already exists, which it won't for an
unauthenticated visitor on this flow. No CSRF requirement was added or
removed anywhere else.

## Browser Storage

No `localStorage`/`sessionStorage` write was added anywhere in this diff.
Verified statically for `Login.tsx`, `App.tsx`, and `api/client.ts`.

## Analytics

No analytics event was added. `lib/analytics.ts`'s typed
`AnalyticsEventPayloads` has no password-recovery-shaped event, and per the
task brief's own instruction, none was invented to force a fit. Verified
statically that `analytics.ts` was not modified and contains no
`password`-related string.

## PII/Secret Safety

- Email, password, and reset code are never logged (no `console.*` calls
  were added anywhere in this diff).
- Neither value is passed to `captureException` (`lib/errorTracking.ts` was
  not modified; `Login.tsx` does not import or call it for this flow).
- Error messages shown to the user are frontend-owned fixed strings for the
  reset-confirm path (not `error.message` from the backend), so a future
  backend wording change cannot introduce a new leak class through this
  screen.

## Accessibility

- New fields reuse the existing `Field`/`PasswordField` components
  unchanged — same `label htmlFor`, `aria-invalid`, `aria-describedby`
  wiring as every other field on this screen.
- New buttons are all real `<button type="button">` elements with their own
  visible text as the accessible name (no icon-only controls were added).
- Status text uses the existing `aria-live="polite"` region
  (`StatusMessage`); no new live region was introduced.
- No color-only signal: every state (invalid/error/success) pairs a color
  class with distinct text, exactly as the pre-existing modes already do.

## Responsive

No new CSS was written — every new element reuses existing classes
(`account-field`, `account-form__submit`, `account-entry__switch`,
`account-confirm`) that already have mobile rules in `Login.css`. Verified
the production build compiles the same `Login.css` bundle
(`dist/assets/Login-*.css`) with no new file needed.

## Email Delivery Truth

Unchanged from the backend's own status: reset request delivery goes
through `resolve_communication_provider()`, sandbox-only in this
environment (no live email credentials). Frontend integration (request →
confirm → success round-trip against the real endpoints) is what this task
verifies; real transactional email delivery remains environment-dependent,
exactly as `V9_GOOGLE_AUTH_ACCOUNT_LIFECYCLE_REPORT.md` §15 already states
for this repository's email in general.

## Build

`cd frontend && npm run build` → **PASS** (`tsc -b && vite build`, 0 errors,
production bundle emitted including `Login-*.js`/`Login-*.css`).

## Lint

`cd frontend && npm run lint` (oxlint) → **PASS**. Pre-existing warnings in
unrelated files (`SearchProgress.tsx`, `main.tsx`, `ops/opsFormat.tsx`,
`ops/opsApi.ts`, `useAccount.ts`, `components/ui/Icon.tsx`) are unchanged by
this diff; oxlint run scoped to the three changed files
(`Login.tsx`/`App.tsx`/`client.ts`) alone produces zero output.

## Tests

No frontend testing framework exists in this repo (consistent with prior
slices) and none was introduced. Added
`frontend/scripts/verify-password-recovery.mjs`, following the exact
technique already established by `scripts/verify-google-signin.mjs`
(transpile the real `.ts`/`.tsx` source with the installed TypeScript
compiler, run it in an isolated `vm` context with mocked `fetch`/`document`,
assert on real behavior — no test framework dependency). It checks:
`requestPasswordReset`/`confirmPasswordReset` produce the correct method,
URL, JSON body, and never leak the email or code into the URL; a `400`
surfaces as a typed error; no browser storage use; no new analytics/
error-tracking taxonomy; the non-enumerating copy is present; the single
generic invalid/expired/used-token message is present (and no "already
used" distinction); a safe "request a new code" route exists; and the
duplicate-submission guard (`canSubmit`/`disabled`) is in place.

Ran: `node scripts/verify-password-recovery.mjs` → **PASS**. Re-ran the two
pre-existing scripts for regression: `verify-google-signin.mjs` → **PASS**,
`verify-analytics.mjs` → **PASS**.

Backend regression: `python -m pytest tests/test_v9_google_auth_account_lifecycle.py -q`
→ **50/50 passed**, unmodified (this task made no backend edits; run purely
to confirm the contract this frontend now depends on is exactly what was
inspected).

## Browser Verification

**PARTIAL.** No interactive browser automation tool was available in this
environment. What was actually verified: `npm run dev` starts cleanly and
serves `HTTP 200` at `http://localhost:5173/` with this diff applied (no
build-time or server-start failure); the production `vite build` output
includes the updated `Login` chunk with no errors. The actual click-through
(forgot → check email → enter code → new password → success → sign in) was
**not** exercised in a real browser and is reported as such rather than
assumed from the static/runtime-source checks above.

## Backend Changes

**NONE.** `git status` confirms only `frontend/src/App.tsx`,
`frontend/src/api/client.ts`, `frontend/src/screens/Login.tsx`, and the new
`frontend/scripts/verify-password-recovery.mjs` + this report were touched.
No genuine backend contract defect was found that would have required
stopping to broaden scope.

## External Blockers

- Same sandbox-only email delivery constraint already documented upstream
  (§15 of the lifecycle report) — not a blocker for this frontend
  integration, which talks to the real endpoints regardless of what backs
  `CommunicationProvider`.
- No interactive browser tool available for a full click-through
  verification in this environment (see "Browser Verification" above).
