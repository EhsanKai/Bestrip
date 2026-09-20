# V9 Limited Beta — Account Signup / Auth / Database Security Audit

**Task-start HEAD:** `6f03c86` ("V9 add limited beta observability baseline")
**Final HEAD (pre-checkpoint):** `6f03c86` + one uncommitted fix (`src/detoura/api/app.py`, `tests/test_v9_phase26_auth_api.py`)

This is primarily a read-only audit with executable evidence. One genuine security defect was found and fixed under §25 policy; everything else here is verification of existing behavior, not a redesign.

A concurrent Codex session's Frontend Reconnection Slice C work (`frontend/src/App.tsx`, `frontend/src/api/client.ts`, `frontend/src/api/types.ts`, `frontend/src/screens/MyTrips.tsx`, `frontend/src/screens/MyTrips.css`) was present as uncommitted changes at task start and was read but never modified, staged, or reverted.

---

## 1. Architecture map

| Concern | Location |
|---|---|
| `UserAccount` model | `src/detoura/models/account.py` |
| `user_accounts` / `auth_sessions` / `trip_ownership` schema | `src/detoura/persistence/db.py` (lines ~445–477) |
| Registration/login/logout/session-validate logic | `src/detoura/services/auth_service.py` |
| Account/session persistence (SQL) | `src/detoura/persistence/accounts.py` |
| Password hashing | `src/detoura/services/password_hashing.py` |
| Email normalization | `src/detoura/services/email_normalization.py` |
| Rate limiting | `src/detoura/services/rate_limit.py` |
| Client-IP trust boundary | `src/detoura/services/client_ip.py` |
| Auth HTTP API (`/api/v1/auth/*`) | `src/detoura/api/auth.py` |
| My Trips (owner-only) API | `src/detoura/api/me_trips.py` |
| Config (TTLs, cookie names, rate-limit knobs) | `src/detoura/auth_config.py` |
| Frontend login/signup screen | `frontend/src/screens/Login.tsx` |
| Frontend account/session state | `frontend/src/state/useAccount.ts` |
| Frontend API client (CSRF header wiring) | `frontend/src/api/client.ts` |

`session_store.py` (V6.5) is a *different* "session" — search-personalization state, unrelated to auth sessions. It was not in scope and was not touched.

---

## 2. Signup contract

**Frontend:** `Login.tsx` renders a signup form; `useAccount.ts::register()` calls `api.register()` → on success immediately calls `login()` → `api.me()` to refresh state. Mounted live in `App.tsx` at `screen === "login"` with `onSignup={({ email, password }) => account.register(email, password)}`.

**Backend:** `POST /api/v1/auth/register` → `auth_service.register()` → `persistence/accounts.create_user()` → `INSERT INTO user_accounts`.

**Verdict: REAL & CONNECTED**, frontend through to DB commit. Confirmed live (not just by reading code) in §5.

---

## 3. Real database verification (live evidence)

A synthetic account (`audit.synthetic.test+v9@example-detoura-audit.invalid`) was registered through the real `TestClient`-driven HTTP API against a throwaway file-backed SQLite DB (not `:memory:`), then the raw file was opened and inspected directly with `sqlite3`, outside the application.

```
user_id: usr_qoFyxRscgspl9AB01XeJEA
email_normalized: audit.synthetic.test+v9@example-detoura-audit.invalid
status: ACTIVE
created_at / updated_at / last_login_at: present, ISO8601 UTC
password_hash: $argon2id$v=19$m=655... (97 chars)
password_hash contains raw synthetic password? NO
```

- Row exists ✅
- Email persisted as designed ✅
- No plaintext password ✅
- Hash format is Argon2id ✅
- Timestamps behave as expected (created/updated set at registration, last_login_at set at first login) ✅

A full scan of every table in the DB file for the raw synthetic password and the raw session/CSRF token values found **zero matches** in any table.

---

## 4. Password storage — Argon2id verification

`services/password_hashing.py`:

- `PasswordHasher()` from `argon2-cffi`, whose default type is Argon2id (confirmed by the `$argon2id$` prefix on the actual stored hash — not just read from code, but observed live).
- Salt is internal to the library's encoded hash string; this module never touches a salt.
- `MAX_PASSWORD_BYTES = 256` (UTF-8 byte cap, pre-KDF) defends against CPU-burn via oversized input; `MIN_PASSWORD_LENGTH = 8`.
- `verify_password()` fails closed: malformed hash, library error, or oversized password all return `False`, never raise.
- `needs_rehash()` exists for future parameter upgrades (not currently invoked at login — a possible future improvement, not a defect: today's parameters are the library's current defaults, so there is nothing to upgrade from yet).

**Functional proof (live):**
- Correct password → login succeeds (200, session cookie issued).
- Wrong password → login fails (401, generic message).
- Stored hash `$argon2id$...` ≠ plaintext password (byte-for-byte, confirmed by substring search).
- `test_hash_is_not_plaintext_and_is_argon2id` and related unit tests in `tests/test_v9_phase26_auth_api.py` / `tests/test_v9_phase26_accounts.py` pass.

**Password Storage: ARGON2ID HASH** — confirmed both by code and by live inspection of an actual persisted row.

---

## 5. Duplicate registration

Live test: registering the same email twice → second call returns `400 {"detail":{"message":"An account with this email already exists."}}`. No second row created (`UNIQUE` constraint on `email_normalized`, enforced at the DB layer via `persistence/accounts.py::create_user` catching the `IntegrityError` and raising `DuplicateEmail`). Case-normalization (`normalize_email`) lower-cases the domain and local part before the uniqueness check, so `Ada@Example.com` and `ada@example.com` collide as intended.

**Duplicate Registration: PASS**

---

## 6. Session creation & storage model

- `new_session_token()` — `secrets.token_urlsafe(32)`, cryptographically random, never derived from anything guessable.
- Only `hash_token(raw_token)` (SHA-256) is persisted in `auth_sessions.token_hash`; the raw token is generated once in `auth_service.login()`, returned directly to the API layer for the cookie, and never logged or stored (confirmed: full-DB scan for the raw token value found nothing).
- `get_session_by_token_hash()` resolves a presented cookie back to the account by hashing the incoming value and querying by hash — the DB never needs to hold (or leak) a replayable raw token.

**Raw Session Token in DB: NONE. Session Hash-at-Rest: PASS**

---

## 7. Cookie security

Live `Set-Cookie` headers from an actual login response:

```
detoura_session=<raw token>; HttpOnly; Max-Age=1209600; Path=/; SameSite=lax
detoura_csrf=<raw csrf>; Max-Age=1209600; Path=/; SameSite=lax
```

- `HttpOnly` on the session cookie: **PASS** (present; absent on the CSRF cookie, which is correct and intentional — the double-submit pattern requires JS to read it).
- `Secure`: **not present** in this dev/test run because `auth_config().is_production` is `False` — this is by design (`DETOURA_ENV=production` / `DETOURA_ENV_PRODUCTION=1` must be set for a real deployment behind HTTPS, and a plain-HTTP local server cannot usefully set `Secure` anyway). **Classification: ENVIRONMENT-DEPENDENT**, not a defect — verified by reading `auth_config.py` and `api/auth.py::_set_auth_cookies`, both of which pass `secure=cfg.is_production`.
- `SameSite=lax` on both cookies.
- `Path=/`, `Max-Age` matches `session_ttl_seconds` (14 days default).

---

## 8. Session restoration, logout, re-login (live E2E, one continuous flow)

Using one `TestClient` instance (cookie jar persists across calls, simulating a real browser):

1. Register → 200
2. Login (correct password) → 200, cookies set
3. `GET /auth/me` → 200, returns the same `user_id`/`email_normalized` — **session restoration confirmed, sourced from server-side session lookup (`validate_session` → DB), never from any client-supplied claim**
4. `POST /auth/logout` **without** `X-CSRF-Token` → **403** (CSRF correctly enforced even on logout)
5. `POST /auth/logout` **with** correct `X-CSRF-Token` (read from the CSRF cookie) → 200, `{"ok": true}`
6. `GET /auth/me` (same cookie jar, i.e. same "browser") → 200, body `null` — **old session no longer authorizes**, confirmed by re-inspecting the raw DB row: the session's `revoked_at` was set to a real timestamp.
7. Re-login with correct credentials → 200, new `user_id` matches
8. Raw DB inspection shows the re-login created a **second, distinct** `auth_sessions` row (`session_id` differs from the first) — genuine session rotation, not reuse of a revoked session.

**Session Restoration: PASS. Logout Invalidates Session: PASS. Re-login: PASS (new session, rotated).**

---

## 9. CSRF

Double-submit pattern (`api/auth.py::require_csrf`): the mutating request must carry `X-CSRF-Token` matching the *specific session's* stored `csrf_token_hash` — not merely "a CSRF cookie was present". Verified live: logout without the header → 403; logout with the correct header → 200. `tests/test_v9_phase26_auth_api.py::test_logout_without_csrf_header_is_rejected` and `test_logout_with_wrong_csrf_value_is_rejected` cover the negative cases already; both pass.

**CSRF: REAL & CONNECTED**

---

## 10. Rate limiting (live, safe verification — not a load test)

12 consecutive login attempts with a wrong password against one freshly-registered synthetic account, from the same (in-process) client:

```
statuses: [401, 401, 401, 401, 401, 401, 401, 401, 429, 429, 429, 429]
```

Exactly matches the configured `DEFAULT_LOGIN_MAX_ATTEMPTS = 8` (per `(IP, email)` pair) before the limiter engages and returns 429. A second, independent per-IP volume budget (`login_ip`, 30/300s) and a separate per-`(IP, email)` registration budget (`register_pair`, 5/hour) exist alongside it (`auth_config.py`, `auth_service.py`). Neither budget is keyed by email alone, specifically to prevent an attacker from locking out a victim's own login using only the victim's email address (documented rationale in `auth_service.py`'s module docstring, and covered by `tests/test_v9_phase26_auth_api.py::test_login_rate_limited_after_repeated_failures`).

**Auth Rate Limiting: REAL & CONNECTED.** Known, documented limitation: the limiter is in-process (`rate_limit.py`), so a multi-worker deployment would need a shared backing store — this mirrors the same, already-documented limitation in the V6.5 personalization session store, and is out of scope to change here (no evidence the current deployment target runs more than one worker).

---

## 11. User enumeration

- Login: every invalid-credentials path — no such account, wrong password, disabled account — raises the identical `AuthError(GENERIC_LOGIN_FAILURE)` ("Invalid email or password."), and a "no such account" lookup runs `verify_password()` against a fixed dummy Argon2id hash first so its timing is shaped like a real verification (`auth_service.py` `_DUMMY_HASH`). No status-code or message difference is observable between the three cases from the API.
- Registration: **does** reveal "an account with this email already exists" on duplicate registration. This is the one endpoint where the module docstring explicitly accepts this as unavoidable ("a client needs to know whether to move on to login") — a narrower signal than login enumeration, and standard practice; not classified as a defect.

**User Enumeration Review: PASS**

---

## 12. Credentials in URLs

- No password, session token, or CSRF token appears in any query string, path parameter, or redirect. Session/CSRF tokens travel exclusively via cookies (`Set-Cookie` at login, `Cookie` on subsequent requests) and the CSRF token additionally via the `X-CSRF-Token` request header — never as a URL component.
- Frontend `client.ts` never appends any of these to a URL.

**Credentials in URLs: NONE**

---

## 13. Browser storage audit (frontend read, not modified)

`grep` across `frontend/src/**/*.{ts,tsx}` for `localStorage`/`sessionStorage` usage:

| File | Key | Content |
|---|---|---|
| `state/useSaved.ts` | saved-trips key | Saved trip list (product data, not auth) |
| `state/journeyDraft.ts` | journey draft key | In-progress search draft (product data) |
| `components/shell/Header.tsx` | `detoura-theme` | UI theme preference |
| `lib/funnel.ts` | `detoura.fk.s` (sessionStorage), `detoura.fk.v` (localStorage) | Random, anonymous per-tab/per-browser analytics keys — module docstring: "No traveller PII is ever passed here; the server also strips anything PII-shaped" |
| `ops/opsApi.ts` | ops session token | **Separate ops/admin auth system** (`api/ops_auth.py`), not the consumer account system audited here |

**Session state itself is never in browser storage.** `useAccount.ts` holds `status`/`profile` only in React state, refreshed from `GET /auth/me` on mount — authentication is derived from the server-validated cookie every time, never trusted from a client-side claim.

**Sensitive Browser Storage: NONE** (ops token is out of scope — separate staff-facing auth system, not part of the consumer UserAccount flow this audit covers).

---

## 14. Log security

`src/detoura/observability/logging.py` + `middleware.py`:

- `CorrelationMiddleware` logs only `method`, `route`, `status_code`/`duration_ms` per request (`request_completed`/`request_failed`) — never headers or body.
- `log_event()` strips any field named in `FORBIDDEN_FIELDS` (`password`, `authorization`, `cookie`, `csrf`/`csrf_token`, `session`/`session_token`, `email`, etc.) as defense-in-depth; the documented primary control is that call sites simply never pass these.
- Live-exercised: registered, logged in with a wrong password, logged in correctly, logged out, and re-logged-in while capturing all emitted log lines. Every `auth_service` call site (`audit.record`) logs only `actor` (a `user_id`, not an email), `action`, `target_type`/`target_id`, and a `note` that is a fixed enum-like string (`"wrong password"`, `"no such account"`, `"account disabled"`) — never the email, password, or any token. Confirmed against the raw `audit_events` table (§3) and the process's stdout JSON log lines: no password, hash, raw token, or full request body in any of them.
- The generic-500 handler (`_unhandled_exception_handler` in `api/app.py`) logs only `method`/`route`/`exception_type` plus the correlation id — never the exception's own message/args, which could otherwise carry request data incidentally.

**Observability Credential Redaction: PASS**

---

## 15. API response security — genuine defect found and fixed

### Finding: password reflected verbatim in 422 validation-error responses (Medium)

**Where:** FastAPI's default `RequestValidationError` handler (installed automatically by `FastAPI()`, not overridden anywhere in `src/detoura/api/app.py` prior to this fix).

**Reproduction (live, before fix):**

```
POST /api/v1/auth/register
{"email": "leaktest@example.invalid", "password": "<1229-char string>"}

→ 422
{"detail":[{"type":"string_too_long","loc":["body","password"],
  "msg":"String should have at most 1000 characters",
  "input":"<the full 1229-character password, verbatim>",
  "ctx":{"max_length":1000}}]}
```

`RegisterRequest.password` and `LoginRequest.password` both declare `max_length=1000` (`api/auth.py`). Any password exceeding that — or failing any other Pydantic-level field constraint — causes Pydantic/FastAPI's default error formatter to include the raw submitted value under `"input"` in the JSON body FastAPI sends back to the caller, via `jsonable_encoder(exc.errors())` (`fastapi.exception_handlers.request_validation_exception_handler`, confirmed by reading the installed library source directly).

**Why this matters even though it is "the caller's own password":** this response body is exactly the kind of payload that ends up captured somewhere it should never be retained — browser devtools/HAR exports attached to a support ticket, a corporate/ISP transparent proxy's access log, an APM or error-tracking tool's captured request/response pairs, a CDN or load-balancer access log with body logging enabled. Item 15 of this audit's own brief explicitly requires that registration/login responses never expose a password; this did, under an easily reachable condition (submitting an over-length password — not an attack, just an edge case a password manager or a fat-fingered paste could trigger).

**Severity: Medium.** It only ever reflects the submitting caller's own just-typed secret (not another user's, and not anything from the database) and requires a specific validation failure to trigger — but it is a real, reproducible instance of exactly the leak class this audit was commissioned to rule out, in production-reachable code, with no attacker precondition beyond "type or paste an unusually long password."

**Fix applied (minimal, per §25):** `src/detoura/api/app.py` now installs an explicit `@app.exception_handler(RequestValidationError)` that reproduces FastAPI's default shape (`{"detail": [...]}`, same `type`/`loc`/`msg`/`ctx` per error) but strips the `"input"` key from any error whose `loc` names a sensitive field (`password`, `token`, `secret`, `csrf`/`csrf_token`, `authorization`, `cookie`, `card_number`, `cvc`, `cvv`, `client_secret` — mirroring the existing `FORBIDDEN_FIELDS` convention in `observability/logging.py`, but for API responses). Non-sensitive fields (e.g. `email`) continue to echo their submitted value, since there is nothing to protect there and the client benefits from seeing what it sent.

**Verification (live, after fix):**

```
same request → 422
{"detail":[{"type":"string_too_long","loc":["body","password"],
  "msg":"String should have at most 1000 characters","ctx":{"max_length":1000}}]}
```

No `"input"` key; the secret marker used in the test password is confirmed absent from the response body by direct substring search. A parallel check on `email` with an invalid value shows the field's value is still echoed (`{"loc":["body","email"],...,"input":"x"}`) — confirming the fix is scoped to sensitive fields only, not a blanket removal of useful validation feedback.

**Regression tests added** (`tests/test_v9_phase26_auth_api.py`):
- `test_register_oversized_password_422_does_not_echo_password`
- `test_login_oversized_password_422_does_not_echo_password`
- `test_non_sensitive_field_validation_error_still_echoes_input` (guards against over-correction)

All three pass, alongside the full existing 51-test `test_v9_phase26_accounts.py` + `test_v9_phase26_auth_api.py` suite (54/54 after the additions).

### Everything else in API responses

- `MeResponse` exposes exactly `user_id`, `email_normalized`, `status`, `created_at`, `last_login_at` — no `password_hash`, no session/CSRF hash, no internal secret. Confirmed both by reading `api/auth.py::MeResponse` and by inspecting the live `/auth/me` response body in §8.
- `register`/`login` responses return only `{"user_id": ...}` — confirmed live.
- `logout` returns only `{"ok": true}`.
- `tests/test_v9_phase26_auth_api.py::test_session_token_never_appears_in_any_json_response_body` already existed and passes, independently covering this for the happy path.

**Password/Hash in API Responses: NONE (after fix). Password in Logs: NONE.**

---

## 16. Database secret scan

Full scan of every table in the live audit DB (§3) for the raw synthetic password and both raw token values used in the E2E run: **zero matches, in any table, any column.** Manual inspection of `auth_sessions` confirms only SHA-256 hashes (64 hex chars) are stored for `token_hash`/`csrf_token_hash`, never the raw values. `audit_events` rows for `account_created`/`login_failure`/`login_success`/`logout` carry only `actor` (user_id), `action`, and a fixed-vocabulary `note` — never a credential.

**Plaintext Password in DB: NONE. Raw Session Token in DB: NONE. CSRF secret persisted: NONE (not intended to be, and isn't) . Authorization bearer values: N/A (this API does not use bearer tokens).**

---

## 17. Email storage truth

`email_normalized` is stored as a plain `TEXT` column (`persistence/db.py`), written and read back verbatim (lower-cased/trimmed at the application layer only — `email_normalization.py`). No field-level encryption, no hashing.

**Email Application-Level Storage: PLAINTEXT.** This is not a password vulnerability — it is a distinct, correctly-classified fact per this audit's own instructions (§21).

**Infrastructure Encryption-at-Rest: NOT VERIFIED FROM APPLICATION REPOSITORY.** SQLite file-level or disk-level encryption is a deployment/infrastructure decision outside this codebase; nothing in the repository proves or disproves it, so no claim is made either way.

---

## 18. UserAccount persisted-field inventory

From `models/account.py` + `db.py` schema:

| Field | Classification |
|---|---|
| `user_id` | Identity |
| `email_normalized` | Identity (plaintext, see §17) |
| `password_hash` | Security metadata (Argon2id output — the *hash*, never the credential itself) |
| `status` | Security metadata (`ACTIVE`/`DISABLED`) |
| `created_at`, `updated_at`, `last_login_at` | Timestamps |

No preferences/profile fields, no traveler PII (name, DOB, passport, phone, nationality) anywhere on `UserAccount`. Nothing here is unexpectedly sensitive.

`auth_sessions`: `session_id`, `user_id`, `token_hash`, `csrf_token_hash` (both hashed, never raw), `created_at`, `expires_at`, `revoked_at`, `last_seen_at` — all security metadata or timestamps, nothing unexpected.

---

## 19. UserAccount vs Traveler boundary

`tests/test_v9_phase26_auth_api.py::test_user_account_model_has_no_traveler_fields` and `::test_traveler_model_has_no_account_fields` (both pre-existing, both pass) assert this programmatically: `UserAccount` has none of `given_name`/`family_name`/`born_on`/`phone`/`passport_number`/`nationality`/`passport_issuing_country`/`document_type`, and `Traveler` has none of `user_id`/`password_hash`/`email_normalized`. `models/account.py`'s own module docstring states the invariant explicitly. Registration only ever calls `create_user(email_normalized, password_hash)` — no path from account creation into any traveler/booking table.

**UserAccount != Traveler: PASS**

---

## 20. Booking ownership smoke check

`tests/test_v9_phase26_auth_api.py::test_user_a_reads_own_trip_not_user_b_trip` (pre-existing, passes) registers two distinct accounts, gives each a claimed booking via `trip_ownership`, and asserts user A's session cannot read user B's trip via `GET /api/v1/me/trips/{booking_id}` — confirmed passing in this audit's test run, both before and after the app.py fix (the fix touches only the validation-error handler, nothing on the ownership path). `test_anonymous_cannot_list_or_read_trips` and `test_disabled_account_cannot_use_a_live_session_for_my_trips` (also pre-existing, also passing) round out the ownership/session-validity boundary.

**Booking Ownership Smoke Check: PASS**

---

## 21. Tests run

**Targeted (before any code change):**
- `tests/test_v9_phase26_accounts.py` + `tests/test_v9_phase26_auth_api.py` — 51/51 pass.

**Live E2E (this audit, ad hoc, against a real file-backed SQLite DB via the actual HTTP API):**
- Register → duplicate-register → wrong-password login → correct login → `/me` → logout-without-CSRF (403) → logout-with-CSRF (200) → `/me` (null) → re-login (new rotated session) → raw-DB inspection at every step. All behaved as documented above.
- 12-attempt rate-limit probe: 8× 401 then 429s, matching configured `DEFAULT_LOGIN_MAX_ATTEMPTS`.

**After the `app.py` fix (executable code changed → targeted re-verification per §26):**
- `tests/test_v9_phase26_accounts.py` + `tests/test_v9_phase26_auth_api.py` (now 54 tests, +3 new regression tests) — 54/54 pass.
- Broader targeted sweep of every other suite that asserts on a 422 response (`test_api.py`, `test_v2_explainability.py`, `test_v3_budget_sensitivity.py`, `test_v5_product.py`, `test_v6_recheck.py`, `test_v65_request_limits.py`, `test_v6_feedback_qa.py`, `test_v7_reopt_adversarial.py`, `test_v7_reoptimization.py`, `test_v85_commercial_security.py`, `test_v85_release_blockers.py`, `test_v9_search_intelligence_slice_1_5.py`, `test_v9_search_integration.py`, `test_v9_search_selection_booking_contract.py`) — all pass; none of them asserted on the `"input"` key or exact validation-error body shape the fix changed, only on `status_code == 422`.

**Full Regression: NOT REQUIRED.** The change is a scoped, additive exception handler touching only the shape of 422 error bodies for fields matching a small sensitive-name list; every suite that exercises 422 responses anywhere in the app was run and passed, which is a sufficiently broad check for a change of this shape. Re-running the full ~2450-test suite would not exercise anything these targeted runs did not already cover.

---

## 22. Independent read-only security review

A second, read-only pass was made against the evidence above, specifically attacking each item in §27 of the audit brief:

- Plaintext/reversible password persistence — checked, none (Argon2id only, verified live).
- Weak hash configuration — `argon2-cffi` defaults (Argon2id); no evidence of a weakened `PasswordHasher(...)` override anywhere in the codebase (`grep -rn "PasswordHasher(" src/` shows exactly the one, default-parameter instantiation in `password_hashing.py`).
- Password/hash leakage in logs or API — one genuine finding (§15), fixed and regression-tested.
- Raw session token persistence — none; hashed at rest.
- Cookie weakness — `HttpOnly` present, `SameSite=lax` present, `Secure` correctly gated on environment.
- Session fixation — a new session is minted only inside `login()`, with a fresh cryptographically-random token every time; there is no path that accepts a pre-existing client-supplied session identifier and "adopts" it.
- Logout not invalidating session — verified live: `revoked_at` is set, and `validate_session` explicitly checks `revoked_at is not None`.
- Auth state trusted from browser storage — no; `useAccount.ts` derives all state from `/auth/me`, never from `localStorage`/`sessionStorage`.
- CSRF bypass — attempted (missing header, and would-be mismatched header); both rejected with 403.
- Missing rate limiting — present and empirically confirmed (§10).
- User enumeration — none on login; documented, minimal, and accepted disclosure on duplicate registration only.
- Duplicate identity creation — prevented at the DB uniqueness-constraint level, not just application logic.
- Credentials in URLs — none found.
- Observability credential leakage — none found (beyond the now-fixed §15 finding, which was in the API response layer, not observability/logging).
- UserAccount/Traveler boundary violation — none; enforced by passing tests and by the domain model itself carrying no traveler fields.
- Booking ownership regression — none; existing cross-user test still passes unmodified.

No new issue was found beyond §15. That finding has been fixed, tested, and re-verified.

**Verdict: APPROVED**

---

## 23. Findings by severity

| Severity | Count | Detail |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 1 (fixed) | §15 — password reflected verbatim in 422 validation-error response body under an oversized-password condition. Fixed via a scoped `RequestValidationError` handler in `src/detoura/api/app.py`; regression-tested. |
| Low | 0 | — |

---

## 24. Remaining gaps / out-of-scope observations (not defects)

- Rate limiting and the personalization session store are both explicitly process-local; a future multi-worker deployment would need a shared backing store for both (already documented in-repo, not new).
- `needs_rehash()` exists in `password_hashing.py` but is not currently wired into the login path to opportunistically upgrade old hashes on successful login. Not a defect today (no parameter change has happened yet to rehash *from*), but worth wiring before parameters ever do change.
- Infrastructure/disk-level encryption-at-rest for the SQLite file could not be verified from the application repository (per §21/§17) — this is a deployment concern, not a code defect.
- Ops/staff authentication (`api/ops_auth.py`, `ops/opsApi.ts`) is a separate system from the consumer `UserAccount` flow and was out of scope for this audit; it was not exercised.
