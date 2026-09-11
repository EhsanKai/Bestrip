# V9 Phase 2.6 Part A — Account & My Trips Foundation

Branch `claude/travel-planner-mvp-nvb267`. Starting point: V9 Phase 2.5
approved at `da7cc6e`. This is the production-oriented identity and
trip-ownership foundation for My Trips, payments, confirmation emails,
invoices, ticket operations and cross-device access — additive only.
Anonymous search and the existing anonymous/demo booking flows are
untouched and remain fully functional.

## Account model

`models/account.py::UserAccount` — `user_id`, `email_normalized`,
`password_hash`, `status` (`ACTIVE`/`DISABLED`), `created_at`, `updated_at`,
`last_login_at`. Deliberately thin: no date of birth, passport, phone,
address or nationality. `user_id` is an opaque random token
(`usr_<random>`), never derived from the email — two accounts never
collide on id, and an email can be changed later (not implemented this
phase) without an identity change.

**UserAccount is not Traveler.** `models/traveler.py::Traveler` (the person
flying) and `UserAccount` (who is logged in) share no fields and are never
joined — `tests/test_v9_phase26_auth_api.py::test_user_account_model_has_no_traveler_fields`
/ `::test_traveler_model_has_no_account_fields` assert this structurally, not
just by convention.

## Password hashing

Argon2id via `argon2-cffi` (`services/password_hashing.py`) — the
OWASP-recommended default, added as a core dependency
(`pyproject.toml`). Per-password salt and cost parameters live inside the
library's own encoded hash string; nothing here manages a salt itself.
Password length is capped at 256 UTF-8 bytes *before* it reaches the KDF —
Argon2's cost scales with input size, so an unbounded password is a cheap
DoS vector, not a feature. `verify_password` never raises: a malformed
hash, a foreign format, or an oversized password all verify `False`.

## Email normalization

`services/email_normalization.py` — trim, lower-case both the local part
and the domain. Deliberately does **not** apply Gmail's dot-insensitivity
or plus-address stripping; those are one provider's behaviour, not a
property of email addresses, and folding them in would silently merge
addresses a user may consider distinct. A duplicate normalized email is
rejected at `persistence/accounts.py::create_user` via the table's own
`UNIQUE` constraint (`DuplicateEmail`).

## Sessions

Server-side, in `auth_sessions` (schema v8). A raw session token
(`secrets.token_urlsafe(32)`) is generated once at login and handed to the
caller for the cookie; only its **SHA-256 hash** is ever persisted — the
same reasoning as a password hash without the KDF, since an opaque random
token already has full entropy and the only property needed is that
stealing the database does not hand out live sessions. Session rows carry
`created_at`, `expires_at`, `revoked_at`, `last_seen_at`. Default TTL 14
days (`AUTH_SESSION_TTL_SECONDS`).

**Cookies.** `detoura_session` — `HttpOnly`, `SameSite=Lax`, `Secure` when
`DETOURA_ENV=production` (a plain-HTTP local dev server cannot set a
`Secure` cookie the browser will send back — an explicit, documented
development exception). `detoura_csrf` — deliberately **not** `HttpOnly`,
since the double-submit CSRF pattern requires client JS to read it; it
carries no bearer capability on its own.

## CSRF

Double-submit + server-side verification (`api/auth.py::require_csrf`): a
mutating request must carry `X-CSRF-Token` matching both the
`detoura_csrf` cookie value *and* the specific session's stored
`csrf_token_hash` — not merely "some CSRF cookie was present". `SameSite`
alone is not relied on. Tested success/failure/missing-header/wrong-value,
and that a failed CSRF check does not itself invalidate the session.

## Rate limiting / abuse controls

`services/rate_limit.py::RateLimiter` — new, reusable, in-memory
fixed-window limiter (Detoura had no shared abuse-control primitive before
this phase). Applied to login (per normalized email, default 8/5min) and
registration (per normalized email, default 5/hour), both configurable via
env. A successful login resets that email's failed-attempt window, so a
legitimate user who mistyped a password twice is not locked out by their
own eventual correct attempt.

## Enumeration resistance

`services/auth_service.py::login` raises the *same* `AuthError` message
(`GENERIC_LOGIN_FAILURE`) for: no such account, wrong password, and a
disabled account. An unknown-account attempt still calls `verify_password`
against a fixed dummy hash so its timing is shaped like a real verify,
rather than returning instantly and leaking existence via timing.

## Trip ownership

`trip_ownership` (schema v8) — one row per `(booking_id -> user_id)`,
entirely separate from `bookings`. A booking with no row is a valid,
permanent anonymous/historical journey, never backfilled.
`persistence/accounts.py::claim_trip` is idempotent for the same user and
never transfers ownership to a different one. **Deliberately not wired into
`booking_flow.py`** — Part A ships the ownership seam
(`claim_trip`/`get_trip_owner`/`list_trip_ids_for_user`) and the read API;
associating a *new* booking with its authenticated creator is a follow-up
integration point, out of scope for "do not redesign BookingFlow."

## My Trips API

`GET /api/v1/me/trips`, `GET /api/v1/me/trips/{booking_id}` — authenticated
owner only. A nonexistent `booking_id` and someone else's real
`booking_id` return the **identical** `404 {"message": "No such trip."}` —
no response-shape difference that would leak whether a given id exists at
all. Anonymous callers get `401`. Only the booking's already-public summary
fields are returned (label, route, phase, party size, totals) — no new PII
surface.

## Audit

Reuses the existing `audit_events` table (`persistence/audit.py`) —
`account_created`, `login_success`, `login_failure`, `logout`. Never
records a password, password hash, or raw token; a failed-login note names
*why* (wrong password / no such account / disabled), never the attempted
password value itself.

## Tests

`tests/test_v9_phase26_accounts.py` (domain/service layer, 36 tests),
`tests/test_v9_phase26_auth_api.py` (HTTP/cookie/CSRF/ownership, 17 tests)
— see the release-gate report for exact counts.

## Known limitations

* Trip ownership is not wired into `booking_flow.py` — a future phase must
  add the actual "claim this booking for the logged-in user" call at
  booking-creation time.
* No email verification, password reset, OAuth, or 2FA (all out of scope
  per the phase spec).
* Rate limiting is in-memory/per-process, matching the existing
  `InMemorySessionStore` deployment default — a multi-process deployment
  would need a shared backend (not built this phase, consistent with "no
  configuration by default").
* The login rate limiter keys strictly on the normalized email with no
  per-IP/device dimension. This correctly prevents a global brute-force
  sweep and does not lock out unrelated accounts, but it means an attacker
  who already knows a victim's email (e.g. from a breach list) can send a
  handful of wrong-password attempts and lock that one victim out of their
  own account — even with the correct password — for the remainder of the
  rate-limit window. Found during independent adversarial QA; not
  considered blocking (no confidentiality/integrity impact, and the window
  is bounded), but flagged as a follow-up before production launch —
  candidates are CAPTCHA-after-N-failures or adding a per-IP signal
  alongside the per-email one.
