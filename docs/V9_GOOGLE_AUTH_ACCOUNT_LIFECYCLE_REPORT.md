# V9 — Google Sign-In + Account Lifecycle Backend

**Starting HEAD:** `1bebc95` ("V9 close booking and payment CSRF gap")
**Inherited concurrent commit:** `b249ae8` ("V9 add Limited Beta analytics foundation") — a concurrent, frontend-only Codex session's work, committed mid-task and preserved unmodified. `git show --stat b249ae8` touches only `frontend/**` and its own report doc; no overlap with anything in this report. A second round of frontend-only uncommitted changes from that same concurrent session was also observed in `git status` while this task was in progress and was left untouched, per this task's ownership boundary (backend/auth/security only).
**Final HEAD (pre-commit):** `b249ae8` + this task's own commit, backend/test/doc paths only.

This slice's scope: Google Sign-In backend architecture, minimum account/session lifecycle hardening for Limited Beta, and a truthful audit of remaining gaps. No consumer frontend UI was built or redesigned — see "Frontend Contract Required" below for what the frontend owner still needs to implement against this backend.

---

## 1. Baseline audit (§1)

The existing email/password system (`src/detoura/{models/account.py, services/auth_service.py, services/password_hashing.py, persistence/accounts.py}`, `src/detoura/api/auth.py`) was already thoroughly audited in `docs/V9_ACCOUNT_AUTH_DB_SECURITY_AUDIT.md` (verdict: **APPROVED**, one Medium finding fixed there). This task re-verified that baseline still holds (54/54 pre-existing `test_v9_phase26_*` tests pass unmodified) before adding anything. Summary of what existed:

- **UserAccount**: `user_id`, `email_normalized`, `password_hash` (was `NOT NULL`), `status` (`ACTIVE`/`DISABLED`), timestamps. No traveler PII.
- **Registration/login/logout**: `auth_service.py`, Argon2id hashing, enumeration-resistant generic login failures, two independent rate-limit budgets (per-IP, per-(IP,email)).
- **Sessions**: opaque `secrets.token_urlsafe(32)` tokens, SHA-256 hashed at rest, `HttpOnly`+`SameSite=lax`+environment-gated `Secure` cookie, 14-day TTL.
- **CSRF**: double-submit cookie, session-bound token hash comparison.
- **Ownership**: `trip_ownership` table, cross-user isolation tested and enforced.
- **No** existing Google/OAuth code, **no** password change endpoint, **no** password reset, **no** account deletion/export endpoint, **no** identity/provider table.

---

## 2. Google OIDC architecture (§2, §16)

Standard Authorization Code flow + PKCE (RFC 7636), per Google's own documented web-server flow — no custom protocol.

**Implemented** (`services/google_oauth.py`, `services/google_auth_service.py`, `api/auth_google.py`):
- State (CSRF-equivalent for the OAuth hop) + PKCE `code_challenge`/`code_verifier` + nonce, generated per attempt, persisted server-side (`google_oauth_pending`, SHA-256-hashed `state`), single-use (atomically consumed), TTL-bounded (10 min default).
- Code exchange happens server-to-server (`POST https://oauth2.googleapis.com/token`) with the confidential `client_secret` (env-only, never on any dataclass, never logged).
- Redirect URI is backend-owned and operator-configured (`GOOGLE_REDIRECT_URI`) — never derived from request input.
- Fail-closed: `GoogleAuthConfig.enabled` requires `client_id` + `redirect_uri` + `client_secret` all present; `/google/start` and `/google/callback` return 503 otherwise. No placeholder/fake credentials were ever added.

**ID token (OIDC) validation — an honest, documented tradeoff (§16):** the textbook-correct approach is local JWKS/RS256 verification. This sandbox's Python 3.14 interpreter has **no prebuilt `cryptography` wheel on PyPI and no Rust toolchain** to build one from source — verified directly:

```
$ pip install pyjwt cryptography
...
💥 maturin failed — Cargo build finished with "exit status: 101"
ERROR: Failed building wheel for cryptography
$ which rustc cargo   → not found
```

Adding `cryptography`/`PyJWT` as a dependency that cannot be installed *or tested* in this environment was rejected as worse than the alternative actually implemented: **ID token validation is delegated to Google's own `tokeninfo` endpoint** (`GET https://oauth2.googleapis.com/tokeninfo?id_token=...`), reached server-to-server over TLS. This is not "trust because HTTPS" — `verify_id_token()` independently re-checks every claim Google's endpoint returns (`iss` against an explicit allow-list, `aud` against the configured `client_id`, `exp` against the caller's clock, `nonce` against the one this exact flow generated) and raises on any mismatch. Every one of these checks has a dedicated, passing test that proves it actually rejects a forged/wrong claim (`test_verify_id_token_rejects_wrong_issuer/audience/expired/nonce_mismatch/missing_subject`). `requires-python` is 3.11+, where `cryptography` has prebuilt wheels and this constraint does not exist — swapping to local JWKS verification later only ever touches `verify_id_token()`, documented in that function's own module docstring.

**Classification: architecture/integration = VERIFIED (unit + integration tests, real logic, no network). Real Google provider E2E = BLOCKED BY CREDENTIALS** — no Google OAuth client exists for this sandbox; nothing here has been exercised against Google's real servers.

---

## 3. Identity model (§3)

`AuthIdentity` (`models/account.py`, table `auth_identities`): `identity_id`, `user_id`, `provider`, `provider_subject`, `provider_email`, `created_at`, `updated_at`. `UNIQUE(provider, provider_subject)` enforced at the DB level. No Google access/refresh token is ever persisted — the token endpoint's `access_token` is read out of the exchange response and immediately discarded (`google_oauth.exchange_code_for_tokens` returns only the `id_token`).

---

## 4. Account-linking policy (§4) — the critical section

Bound to `sub` (provider subject), never email alone. All seven cases from the brief, each with a passing test:

| Case | Behavior | Test |
|---|---|---|
| A — brand-new identity, no account | New passwordless `UserAccount` + `AuthIdentity` created **atomically in one transaction** (see §Independent Review finding #1) | `test_case_a_brand_new_identity_creates_passwordless_account` |
| B — returning identity | Logs into the identity's own `user_id` unconditionally; `provider_email` metadata refreshed, never re-keyed | `test_case_b_returning_identity_logs_into_same_account_and_refreshes_email` |
| C — existing account shares email, identity unlinked, caller unauthenticated | **Never auto-linked.** A short-lived `pending_google_links` row is created; caller gets `GoogleLinkRequired(link_id)`; no session, no identity created | `test_case_c_existing_password_account_same_email_is_never_silently_linked` |
| D — Google email changes | Transparent under B — metadata-only refresh | covered by the Case B test (second login uses a different email) |
| E — multiple Google identities | Each is its own `AuthIdentity` row; a second identity sharing an email still goes through C/F | implied by uniqueness constraint + Case C/G tests |
| F — authenticated user deliberately connects Google | `build_start_url(link_user_id=...)`; callback links straight to that account, **no email check** (an authenticated, deliberate action) | `test_case_f_authenticated_user_connects_google_directly_no_email_check` |
| G — identity already bound to a different account | Always `GoogleLinkConflict`, never a silent transfer; original binding provably unchanged | `test_case_g_conflict_never_transfers_ownership` |

Case C's completion path (`confirm_pending_link`) **re-checks** that the authenticated caller's own account email matches the pending Google identity's email — never trusts the pending row alone (`test_case_c_confirm_link_requires_matching_authenticated_email`). Link tickets are single-use (`test_case_c_confirm_link_succeeds_...` asserts a second confirm fails).

**Frontend contract required** (not built here, per this task's ownership boundary): the Login/Signup screen needs a "Sign in with Google" button that navigates (full page load, not fetch) to `GET /api/v1/auth/google/start`; a page that reads `?google_auth=success|error` / `?google_link_required=1&link_id=...` off the post-login redirect URL and reacts accordingly (on `link_required`, prompt the user to log in with their existing password, then call `POST /api/v1/auth/google/link/confirm` with that `link_id`); and an account-settings "Connect Google" action that calls `POST /api/v1/auth/google/link/start` (authenticated + CSRF) and navigates to the returned `authorization_url`.

---

## 5. Same-email collision (§5)

Never uses email as the primary key — `normalize_email()` (pre-existing) is applied identically to Google's `email` claim before any comparison, so normalization is consistent between the two auth methods. `email_verified` is required (`"true"` claim, checked explicitly) — an unverified Google email is rejected outright (`test_unverified_email_is_rejected`), never treated as proof of ownership. No enumeration signal is added: reaching Case C at all requires the caller to have already authenticated *as that email* with Google itself, a materially stronger bar than typing an email into a form.

---

## 6. Google session integration (§6)

`services/auth_service.py` gained one shared `mint_session()`, and both `login()` (refactored) and every Google-auth success path call it — **the literal same code path** mints a session either way: same opaque-token generation, same SHA-256 hash-at-rest, same TTL/cookie contract (`api/auth_google.py::_set_auth_cookies` reuses `api/auth.py`'s exact cookie-setting helper). No parallel session system was created. Google's own tokens never become a Detoura session token.

---

## 7. Configuration / fail-closed (§7)

`google_auth_config.py`: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (read fresh from env at call time, never cached on the config object — mirrors `communication_config.py`'s `RESEND_API_KEY` discipline), `GOOGLE_REDIRECT_URI`, `GOOGLE_POST_LOGIN_REDIRECT_URL`. `.enabled` requires all three of client id/secret/redirect URI; both `/google/start` and `/google/callback` return **503**, never proceed with an empty client id. No fake/placeholder production credentials were added anywhere.

---

## 8. Password-account coexistence (§8)

All 54 pre-existing `test_v9_phase26_accounts.py` + `test_v9_phase26_auth_api.py` tests pass unmodified. Argon2id parameters and password policy (min 8 / max 256 UTF-8 bytes) are untouched. The one required internal change — `password_hash` becoming nullable — is backward-compatible: a pre-existing database is migrated in place (`Database._relax_password_hash_nullable`, a standard SQLite rebuild-and-swap, since SQLite has no `ALTER COLUMN ... DROP NOT NULL`), verified against a hand-built legacy-schema database with a real row that survives the upgrade unchanged (`test_existing_database_with_not_null_password_hash_migrates_cleanly`).

---

## 9. Password change (§9)

`POST /api/v1/auth/password/change` (`api/auth_account.py`, `services/password_service.py::change_password`) — authenticated session + CSRF, current-password re-verification, Argon2id, existing password policy. **Session revocation, explicitly decided**: every session for the account is revoked, then one fresh session is minted for the caller — so the person who just re-proved their password is not logged out of their own request, but a stolen session anywhere else dies immediately (`test_change_password_success_revokes_other_sessions_and_reissues_current`). Rate-limited per `user_id` (`test_change_password_rate_limited`). A Google-only account gets a distinct, non-enumerating error directing it to "forgot password" (§11).

---

## 10. Password reset (§10)

`POST /api/v1/auth/password/reset/request` + `.../confirm` (`services/password_service.py`). Built on the existing `CommunicationProvider` abstraction (`resolve_communication_provider()`) rather than the booking-shaped `customer_communications` orchestration layer, which requires a `booking_id` this flow has none of.

- Opaque `secrets.token_urlsafe(32)` token, **SHA-256 hashed at rest** (`password_reset_tokens.token_hash`), never persisted in plaintext.
- 30-minute default expiry, **single-use** (atomic `consume_reset_token` UPDATE — a race between two confirms with the same token can only ever let one through, `test_reset_token_is_single_use`).
- **No user enumeration**: `request_password_reset` always completes with the identical generic response regardless of whether the email exists, is disabled, or is rate-limited (`test_password_reset_request_api_always_200_same_shape`); an email is only ever actually sent for a real ACTIVE account (`test_reset_request_unknown_email_sends_nothing_and_creates_no_token`, `test_reset_request_disabled_account_sends_nothing`).
- **Rate-limited**: per-(IP, email) and per-IP request budgets, per-IP confirm budget (defense-in-depth; the token itself is not brute-forceable).
- **Session revocation, explicitly decided**: every session is revoked and **none is re-issued** — a reset assumes possible compromise, so the caller proves the new password by logging in with it afterward, same as anyone else (`test_reset_confirm_success_updates_password_and_revokes_sessions_without_new_login`).
- Never logs the token, the request body, or the email content (verified: `providers/resend_email.py` and `sandbox_email.py` log only status/timing/outcome, never body — checked directly).

---

## 11. Google-only account password semantics (§11)

`password_hash` is genuinely `NULL` for a Google-only account — never an empty string, never a random hidden value. Explicit, tested behavior:

- **Login**: fails with the exact same generic message as any wrong-password case, verified against a fixed dummy hash for constant timing (`test_password_login_never_reveals_google_only_account_exists`) — which auth method an account uses is not a fact login may reveal.
- **Password change**: a distinct, clear error ("no password set yet, use forgot password") — the caller is already authenticated, so this isn't an enumeration leak, just accurate guidance.
- **Password reset doubles as "set a first password"**: since a reset token already proves control of the registered email — the same bar as at registration — `confirm_password_reset` sets `password_hash` regardless of whether one existed before (`test_reset_confirm_sets_first_password_for_google_only_account`). This is a deliberate, minimal, safe implementation of the "set password" mechanism the brief allows deferring — no separate mechanism was built because none was needed.

---

## 12. Session lifecycle (§12)

- Logout invalidates correctly (pre-existing, re-verified).
- Expired sessions fail closed (pre-existing, re-verified).
- Google login creates a normal Detoura session (§6).
- Password change/reset revocation policies are explicitly decided and tested (§9/§10).
- Session tokens are never stored plaintext (pre-existing SHA-256-at-rest, unchanged).
- Cross-user isolation: no change to the ownership/session-validation path; all pre-existing ownership tests still pass.
- No devices dashboard was built (not requested, not already present).

---

## 13. Account deletion / export / retention (§13)

**No legal retention policy exists in this repository, and none was invented.** What was built is the subset that is safely correct without one:

- **`POST /api/v1/auth/account/delete`** (authenticated + CSRF; current-password re-verification for accounts that have one, nothing to re-verify for Google-only accounts): sets `status=DELETED` (new `AccountStatus` value), scrubs `email_normalized` to a `user_id`-derived tombstone, nulls `password_hash`, revokes every session, deletes every `AuthIdentity` row. **Never touches** `trip_ownership`, `checkout_snapshots`, `payment_transactions`, `financial_documents`, or any other financial/booking record — all keyed on `user_id`, never cascaded (`test_delete_account_scrubs_email_and_revokes_sessions_but_keeps_trip_ownership` asserts owned booking ids survive deletion unchanged). A deleted account's email is freed for re-registration (tombstone is unique per `user_id`), and the deleted account itself can never log in again.
- **`GET /api/v1/auth/account/export`** (authenticated): returns account identity fields, linked-provider metadata (provider + email + link timestamp — never the internal `provider_subject`), and the list of owned booking ids. **Classification: PARTIAL.** A full export of booking/payment/communication history was deliberately not built — deciding what portion of Detoura's own transaction records constitutes "this person's data" versus "Detoura's own commercial/legal record" is a product/legal scoping question this slice has no authority to answer by guessing, and guessing wrong in either direction (over-exporting financial detail, or under-exporting what a real DSAR would require) is worse than stating the gap plainly.
- **Retention dependency**: explicitly **BLOCKED** on a product/legal decision about booking/financial/communication record retention. The infrastructure that is clearly safe (credential/session/identity erasure, email scrubbing) is built; nothing that requires guessing a retention period was.

---

## 14. Rate limiting (§14)

Reuses the existing process-local `RateLimiter` (`services/rate_limit.py`) exclusively — no new subsystem. New scopes: `google_oauth_start`/`google_oauth_callback` (per IP), `google_link_start`/`google_link_confirm` (per user_id, authenticated), `password_reset_ip`/`password_reset_pair`/`password_reset_confirm`, `password_change` (per user_id). Same documented limitation as the existing login/register limiters: process-local, would need a shared backend for a multi-worker deployment (not new, not this slice's scope to change).

---

## 15. PII / secret logging (§15)

Audited directly, line by line, every new call site:
- `audit.record()` calls carry only `actor` (a `user_id`), `action`, `target_type`/`target_id` — never an email, token, or Google claim.
- `log_event()` calls in `api/auth_google.py` carry only a fixed outcome label (`"failed"`, `"link_conflict"`) — never Google's response body, the code, or the id_token.
- `google_oauth.py` never calls `log_event`/`_logger` at all — every failure is a fixed, generic `GoogleOAuthError` message, never Google's own error detail (which could echo the token/code).
- The password-reset email body (which does contain the raw token) is passed only to the injected `CommunicationProvider.send()` — the sandbox provider stores it in-process, never logs it; the Resend adapter's own logging (`resend_email.py::_log`) is verified to log only status/timing/outcome, never the request body, confirmed by reading its source directly.

## 15a. Independent security review findings (§20)

A separate, adversarial read-only pass over every new file, checked against: account takeover, blind linking, OAuth state/nonce bypass, redirect manipulation, token confusion, issuer/audience mistakes, session fixation, enumeration, reset-token leakage/reuse, CSRF regression, cross-user ownership regression, secret/PII logging, accidental financial-record deletion.

**Finding 1 (fixed) — Medium: password value echoed in a 422 response for the new endpoints.** `api/app.py`'s existing `RequestValidationError` handler (added in the prior `V9_ACCOUNT_AUTH_DB_SECURITY_AUDIT.md` slice to stop exactly this leak) matched field names by **exact equality** against `{"password", "token", ...}`. This slice's new endpoints use `current_password`/`new_password` — names that do not equal `"password"` — so an oversized value on either field was echoed back verbatim in the `422` body, reproducing the identical leak class the prior fix targeted. Reproduced live:
  ```
  POST /api/v1/auth/password/change  {"current_password": "...", "new_password": "X"*2000}
  → 422 {"detail":[{... ,"input":"XXXX...2000 chars..."}]}
  ```
  **Fix**: `_SENSITIVE_VALIDATION_FIELDS` matching changed from exact-equality to substring, so any field name containing `password` (or `token`/`secret`/`csrf`/etc.) is covered regardless of prefix — closes this instance and forecloses the same class recurring under a future field name. Re-verified live (`input` key absent for both `current_password` and `token` fields); the pre-existing `test_non_sensitive_field_validation_error_still_echoes_input` guard (asserting `email` still echoes) still passes, confirming the fix did not over-broaden.

**Finding 2 (fixed) — Low/robustness: narrow race in Case A.** Two concurrent callback completions for the exact same brand-new Google identity could, in the window between `create_user`'s commit and a separate `create_identity`'s commit, cause the second caller to misread "account exists, no identity yet" as Case C (existing-account collision) instead of Case B (returning identity) — safe (no auth bypass, no data corruption) but confusing. **Fix**: `persistence/accounts.py::create_google_user_with_identity` creates both rows in one `db.write()` transaction; `google_auth_service._complete_signin` now uses it. Covered by the existing Case A/B tests (behavior for a single caller is unchanged; the fix removes the window a concurrent second caller could observe).

**Reviewed, no defect found:**
- OAuth `state`/nonce/PKCE: hashed at rest, single-use (atomic consume), TTL-bounded, tied to a specific `code_verifier`; all four rejection paths (issuer/audience/expiry/nonce) have a dedicated passing test.
- Redirect manipulation: `post_login_redirect_url` is operator-configured only; `_redirect_with()` never reflects attacker-supplied `code`/`state` into the Location header, only fixed literal outcome strings.
- Token confusion: only `id_token` is ever inspected for identity; `access_token` is discarded immediately, never persisted or logged.
- Session fixation: every session token is freshly random; no path adopts a client-supplied identifier.
- CSRF: every new mutating, cookie-authenticated endpoint (`password/change`, `account/delete`, `google/link/start`, `google/link/confirm`) goes through the exact same `require_session`+`require_csrf` pair `logout` already used; the two Google GET endpoints (`start`/`callback`) are full-page browser navigations with no custom-header capability, defended by `state` instead — standard for this flow, not a gap.
- Cross-user ownership: untouched code path; all pre-existing ownership tests pass unmodified.
- Accidental financial-record deletion: `delete_account` provably never touches `trip_ownership`/payment/financial tables (tested).

**Not fixed, documented (Low, out of scope for this slice):** `google_oauth_pending`/`pending_google_links`/`password_reset_tokens` rows are never proactively pruned after expiry (each row is tiny — a hash plus a few timestamps — and this mirrors how several other short-lived tables in this codebase already behave); a future retention sweep would be a reasonable, separate addition, not a security defect today. The Case-C `link_id` travels in a redirect URL query parameter (browser history/referrer exposure) — mitigated by being single-use and short-TTL and only exploitable by someone who can already authenticate as the exact account whose email matches the pending identity; a stricter design would avoid the URL entirely at the cost of more frontend complexity, judged not worth it for this slice.

**Verdict: no unresolved Critical/High/Medium finding.** Both findings above were fixed and re-verified against the full targeted test suite.

---

## 16. External blockers

- **Real Google OAuth credentials**: none exist in this sandbox. Nothing here has been exercised against `accounts.google.com`/`oauth2.googleapis.com` for real — every test uses an injected fake `HttpClient`. Classified per §16: architecture/integration VERIFIED, real-provider E2E BLOCKED BY CREDENTIALS.
- **`cryptography` build toolchain**: unavailable in this sandbox (no Rust); drove the tokeninfo-endpoint validation decision in §2. Not a blocker for a real deployment target (Python 3.11/3.12, prebuilt wheels available).
- **Retention/legal policy** for booking/financial/communication records: not established anywhere in this repository; blocks a full data-export/full-erasure implementation (§13).
- **Frontend UI**: out of this task's ownership boundary; the contract in §4 is documented, not implemented.

---

## 17. Focused tests

`tests/test_v9_google_auth_account_lifecycle.py` — 50 new tests, all passing: PKCE/authorization-URL construction, token-exchange failure, ID-token issuer/audience/expiry/nonce/subject validation (both accept and reject paths), Cases A–G of the linking policy, disabled-account handling, OAuth state single-use/expiry, password-login non-enumeration of Google-only accounts, password change (success/wrong-password/Google-only/rate-limited), password reset (unknown/known/disabled email, single-use, expiry, garbage token, sets-first-password), account export/delete (including that a second delete/export on an already-deleted account is refused), the schema migration path, and the full HTTP API surface (cookies, CSRF, 401/403/429 boundaries) for every new endpoint.

Plus: all 54 pre-existing `test_v9_phase26_accounts.py`/`test_v9_phase26_auth_api.py` tests re-run and still pass unmodified.

---

## 18. Full regression

Full suite run on the final code: **0 failures, 0 errors**, exit code 0. Dot-row tally from the run: 2542 passed + 25 skipped (skips are pre-existing, unrelated to this slice — e.g. optional-dependency-gated tests). Baseline given for this task was 2,518 collected/2,493 passed/25 skipped/0 failed; this run adds this slice's 50 new tests on top with no regression anywhere else. (Note: this pytest install's `-q` mode did not print its usual trailing "`N passed in Xs`" summary line in this sandbox terminal — counts above are from the dot-row output directly; exit code 0 and the complete absence of any `F`/`E` marker across the entire run are the authoritative pass/fail signal.)

---

## Classification summary

| Area | Classification |
|---|---|
| Existing password auth | REAL & CONNECTED (re-verified, unmodified) |
| Google OIDC architecture | BUILT, VERIFIED (integration-level, no network) |
| Google ID-token signature check | BUILT, VERIFIED — via Google's tokeninfo endpoint, not local JWKS (documented tradeoff, §2/§16) |
| Google provider real E2E | BLOCKED BY CREDENTIALS |
| Identity model | REAL & CONNECTED |
| Account linking policy (A–G) | REAL & CONNECTED, tested |
| Google session integration | REAL & CONNECTED (same code path as password login) |
| Password change | REAL & CONNECTED |
| Password reset | REAL & CONNECTED (sandbox provider only — no live email credentials in this sandbox, same status as the rest of this repo's transactional email) |
| Session revocation policy | REAL & CONNECTED, explicitly decided both flows |
| Account deletion | PARTIAL — credential/session/identity erasure real; financial/booking retention BLOCKED on policy |
| Data export | PARTIAL — account metadata real; full transaction history NOT BUILT (scoping blocker) |
| Retention | BLOCKED (no policy exists) |
| CSRF | REAL & CONNECTED on every new mutating endpoint |
| Rate limiting | REAL & CONNECTED, reuses existing infrastructure |
| PII/secret logging | NONE FOUND (audited directly) |
| Frontend | NOT BUILT (out of scope) — contract documented |

---

## Concurrency

**Original Task Baseline:** `1bebc95`
**Inherited Analytics Commit:** `b249ae8` ("V9 add Limited Beta analytics foundation") — frontend/docs-only, verified via `git show --stat`, no overlap with any file this task touched.
**Analytics Commit Preserved:** YES — never modified, staged, or reverted.
A further round of uncommitted frontend changes from the same concurrent session was observed in `git status` during this task and was likewise left untouched.
