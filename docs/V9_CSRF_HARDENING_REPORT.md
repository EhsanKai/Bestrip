# V9 — Booking / Payment CSRF Hardening Slice

**Starting HEAD:** `37fc7064a5f2fc4e3ec4a5222c170008882c6960` ("V9 harden consumer frontend for Limited Beta")
**Final HEAD (parent commit worked from):** `8e11b8058401b8d847ef1ed4f64067e38ce0f5c1` ("V9 establish technical SEO foundation" — an independent, concurrent frontend/SEO slice; touched only `frontend/{index.html,vite.config.ts,src/App.tsx,src/main.tsx,src/lib/seo.ts}`, `frontend/.env.example`, and `docs/V9_TECHNICAL_SEO_FOUNDATION_REPORT.md`. No overlap with this slice's backend security files; verified via `git show --stat`.)
**This slice's checkpoint commit:** created on top of `8e11b805`, see "Checkpoint Commit" below.

## Scope

Narrow security-hardening slice. Closed the one deferred Medium finding from
the Real Provider E2E audit: *"CSRF enforcement is inconsistent across
authenticated booking/payment mutation routes."* Did not reopen the broader
V9 security audit, redesign authentication, touch payment/booking business
semantics, or change the consumer UI.

## Root cause

Detoura's CSRF mechanism (`api/auth.py::require_csrf`, §A6 double-submit:
`X-CSRF-Token` header must match a JS-readable `detoura_csrf` cookie *and*
hash to the session's stored token) is not global middleware — each mutating
route must call it explicitly. Two cookie-authenticated, state-mutating
routes never did:

1. **`POST /api/v1/payments`** (`payments.py::create_payment`) — read an
   optional session (`get_optional_session`) to attribute a new payment to a
   signed-in user, but never called `require_csrf`, unlike its siblings
   `confirm_payment` and `refund_payment` in the same file, which already
   did.
2. **`POST /api/v1/booking-intents`** (`v1.py::create_booking_intent`) —
   read an optional session to attribute the new trip's ownership
   (`owner_user_id`), but never called `require_csrf` either.

Both are real CSRF exposure: a malicious page can auto-submit a cross-site
`POST` and the victim's browser attaches their ambient session cookie
automatically. Without the CSRF check, that forged request would have
succeeded and attached its side effect (a new payment, or a newly-owned
trip) to the victim's account — the attacker cannot read the JSON response
(no CORS grant), but the durable state mutation still happens.

Existing test code had already, unintentionally, documented the gap:
`tests/conftest.py::authorize_payment_for_booking` created payments with no
CSRF header and only added the header for the `confirm` call; several
`test_v9_phase4_api_security.py` / `test_v9_phase6_*.py` tests created
payments/booking-intents on an authenticated `TestClient` with no CSRF
header at all — which only worked because the gap existed.

## Route audit

Every route accepting the consumer session cookie (`api/auth.py`,
`api/payments.py`, `api/me_trips.py`, and the one route in `api/v1.py` that
touches session) was enumerated, plus every `POST` route across
`api/{v1,payments,me_trips,routes}.py`. Ops routes (`api/ops*.py`) use
`Authorization: Bearer <token>` exclusively (verified: no
`get_optional_session`/cookie usage anywhere in those files) — not
cookie-authenticated, so CSRF does not apply to them by design; unaffected.

| Method | Route | Auth mechanism | Cookie auth accepted? | Mutates state? | CSRF required? | Enforcement before this slice | Enforcement after | Fix |
|---|---|---|---|---|---|---|---|---|
| POST | `/api/v1/auth/register` | none | no | yes (creates account) | no — no session exists yet | n/a | n/a | none needed |
| POST | `/api/v1/auth/login` | none (mints session) | no | yes (mints session) | no — mints the CSRF token itself; login-CSRF is a distinct, accepted-risk class not in scope | n/a | n/a | none needed |
| POST | `/api/v1/auth/logout` | session (optional) | yes | yes (clears session) | yes, when a session exists | ✅ enforced | ✅ enforced | none needed |
| GET | `/api/v1/auth/me` | session (optional) | yes | no (read) | no | n/a | n/a | none needed |
| **POST** | **`/api/v1/payments`** | **session (optional)** | **yes** | **yes (creates payment, attributes to user)** | **yes, when a session exists** | ❌ **missing** | ✅ **enforced** | **`require_csrf` added** |
| GET | `/api/v1/payments/{id}` | session (optional) | yes | no (read) | no | n/a | n/a | none needed |
| POST | `/api/v1/payments/{id}/confirm` | session (optional) | yes | yes (authorizes payment — real money path) | yes, when a session exists | ✅ enforced | ✅ enforced | none needed |
| POST | `/api/v1/payments/{id}/refund` | session (required) | yes | yes (refunds payment) | yes | ✅ enforced | ✅ enforced | none needed |
| POST | `/api/v1/payments/webhook/{provider}` | provider signature | no (not cookie auth) | yes | no — not cookie-authenticated | n/a | n/a | none needed |
| GET | `/api/v1/me/trips*`, documents, confirmation | session (required) | yes | no (read) | no | n/a | n/a | none needed |
| POST | `/api/v1/me/trips/{id}/confirmation/resend` | session (required) | yes | yes (triggers resend) | yes | ✅ enforced | ✅ enforced | none needed |
| **POST** | **`/api/v1/booking-intents`** | **session (optional)** | **yes** | **yes (creates booking, attributes ownership)** | **yes, when a session exists** | ❌ **missing** | ✅ **enforced** | **`require_csrf` added** |
| POST | `/api/v1/booking-intents/{id}/commercial` | none (`_booking_or_404` only) | no | yes | no — not cookie-authenticated, capability-by-`booking_id` (deliberate design) | n/a | n/a | none needed |
| POST | `/api/v1/booking-intents/{id}/travelers` | none | no | yes | no — same | n/a | n/a | none needed |
| **POST** | **`/api/v1/booking-intents/{id}/confirm`** | **none** | **no** | **yes (booking execution / payment-gated issuance)** | **no — not cookie-authenticated, capability-by-`booking_id` (deliberate design)** | n/a | n/a | **none — see below** |
| POST | `/api/v1/booking-intents/{id}/tickets/{seq}/start-booking`, `/mark` | none | no | yes | no — same | n/a | n/a | none needed |
| POST | `/api/v1/search`, `/trips/recheck`, `/trips/revalidate`, `/trips/reoptimize`, `/events`, `/commercial/preview`, `/feedback/{id}`, `/budget-sensitivity`, `/plan-trip` | none | no | varies (mostly stateless compute) | no — none of these read the session cookie at all (verified: only one `get_optional_session` call exists in the entire `v1.py`, inside `create_booking_intent`) | n/a | n/a | none needed |
| POST/GET | `/api/v1/ops/**` | `Authorization: Bearer` | no | yes | no — not cookie-authenticated | n/a | n/a | none needed |

### Why `confirm_booking` and the rest of the booking-intent flow are intentionally untouched

`_booking_or_404` (v1.py) looks a run up purely by `booking_id` — it never
calls `get_optional_session`/`require_session`. `resolve_eligible_payment_for_booking`
likewise resolves the ALL_IN_ONE payment gate purely from `run.booking_id`,
never from any session/user_id. This is an intentional, existing design
(explicitly documented in-code as the booking-claim/capability model, and
directly asserted by
`test_v9_phase6_booking_security.py::test_booking_id_capability_authorizes_execution_by_design_ownership_is_unaffected`).
Because the session cookie has **zero effect** on these routes' outcome,
they are not "cookie-authenticated" in the sense CSRF defends against —
a request with no cookies at all succeeds identically to one with a forged
session cookie. Adding a CSRF check here would require first adding a new
session-authorization dependency these routes were deliberately built
without, which is an authentication redesign explicitly out of scope for
this slice ("Do NOT weaken... booking claim semantics", "Do NOT redesign
authentication"). `create_booking_intent` is the one exception in this
family: it does read the session, specifically to decide who owns the
resulting trip, so it is the one route in this family that needed the fix.

## Fix

Two additive, minimal changes — both place the `require_csrf` call
immediately after the session lookup and before any mutation/side effect,
matching the existing pattern in `confirm_payment`/`refund_payment`/
`resend_confirmation`, and both are no-ops for the anonymous flow (no
session ⇒ never called):

- `src/detoura/api/payments.py::create_payment` — `require_csrf(request, session)` when `session is not None`, before `freeze_checkout_snapshot`/`ps.create_payment`.
- `src/detoura/api/v1.py::create_booking_intent` — `require_csrf(request, session)` when `session is not None`, before any booking-run creation (`create_run_from_selection`/`create_run_demo`) or `booking_store().put(run)`.

## Security invariant re-confirmed

- **Payment confirmation:** CSRF-protected when signed in (pre-existing, unchanged, re-verified).
- **Booking confirmation:** not cookie-session-gated at all (capability-by-`booking_id`, deliberate design, unchanged); CSRF is not the applicable control here and none was added, per scope constraints above.
- **Missing token:** fails closed — 403, verified for both `create_payment` and `create_booking_intent` (new tests).
- **Invalid token:** fails closed — 403, verified for both routes (new tests, forged/garbage token value).
- **Valid token:** succeeds — 200/201, verified for both routes.
- **Side effects before CSRF:** none. In both fixed routes the CSRF check runs before any persistence write; new tests assert zero payment rows exist after a rejected `create_payment`, and zero ownership rows exist after a rejected `create_booking_intent`.
- **No Stripe/Duffel/booking-execution side effect from a rejected request:** confirmed — `create_payment` never reaches `payment_config`/provider resolution on rejection; `create_booking_intent` never reaches `booking_store().put()` on rejection. This slice uses the sandbox payment provider throughout (no real Stripe/Duffel objects created).
- **Ownership regression:** none — cross-user IDOR/ownership tests (`test_v9_phase6_ownership_wiring.py`, `test_v9_phase6_pii_security.py`, `test_v9_phase4_api_security.py`'s cross-user tests) all still pass unchanged.
- **Unauthenticated behavior:** unchanged and re-verified — anonymous callers never had a CSRF cookie, so `session is None` and `require_csrf` is never invoked; the entire legacy anonymous booking/payment flow remains CSRF-free by design and still passes.
- **Frontend compatibility:** no frontend changes needed. `frontend/src/api/client.ts`'s shared `request()` helper already attaches `X-CSRF-Token` (read from the `detoura_csrf` cookie) on **every** non-GET request whenever that cookie is present — this was already true for `createPayment()` and `createBookingIntent()` before this slice; the backend was simply not checking it yet. Verified via `git diff`/`git log` that this file is untouched both by this slice and by the concurrent SEO commit.

## Tests added

New file `tests/test_v9_csrf_hardening.py` (9 tests): for both
`create_payment` and `create_booking_intent` — signed-in + missing CSRF
(403, no side effect), signed-in + invalid/forged CSRF (403, no side
effect), signed-in + valid CSRF (success, side effect recorded correctly),
anonymous (success, no CSRF needed), and one named cross-site-forgery
scenario test per route confirming a request carrying only the victim's
ambient session cookie (no valid CSRF token) cannot plant a trip on the
victim's account.

### Existing tests updated (to keep exercising the *intended* authenticated
path now that it is enforced — not to weaken the new check)

- `tests/conftest.py::authorize_payment_for_booking` — now sends the CSRF header on the `create` call too (previously only on `confirm`).
- `tests/test_v9_phase4_api_security.py` — 6 call sites (`test_cross_user_cannot_read_or_confirm_or_refund_someone_elses_payment`, `test_owner_can_read_and_confirm_their_own_payment`, `test_confirm_by_signed_in_user_without_csrf_header_rejected`, `test_refund_requires_sign_in_and_csrf`, `test_consumer_refund_is_full_remaining_only_client_amount_ignored`, `test_concurrent_refund_race_is_a_clean_409_never_an_unhandled_500`) — added the CSRF header to the authenticated `create_payment` call; the specific confirm/refund calls each test is actually exercising for missing-CSRF behavior were left deliberately unheadered.
- `tests/test_v9_phase6_ownership_wiring.py` — added a `_csrf_headers` helper and applied it to the 6 authenticated `create_booking_intent` calls; anonymous-flow calls (4 of them) left untouched.
- `tests/test_v9_phase6_booking_security.py` — 1 authenticated call site fixed inline.
- `tests/test_v9_phase6_pii_security.py` — fixed at the shared `_new_booking` helper (1 change covers all its callers).
- `tests/test_v9_payment_booking_coupling.py` — fixed at the shared `_create_all_in_one_intent` helper (1 change covers all its callers; no-op for the anonymous callers).

No test was changed to make a *rejected* request instead succeed, or to
stop asserting a 403/404 it previously asserted — every change either adds
a correct CSRF header to an authenticated *creation* call so the test can
reach the behavior it actually means to exercise, or leaves a deliberately
CSRF-less call exactly as it was to keep testing that specific rejection.

## Test results

**Focused (CSRF + payment/booking security, 6 files, 72 tests):**
`tests/test_v9_csrf_hardening.py`, `test_v9_phase4_api_security.py`,
`test_v9_phase6_ownership_wiring.py`, `test_v9_phase6_booking_security.py`,
`test_v9_phase6_pii_security.py`, `test_v9_payment_booking_coupling.py` —
**72 passed, 0 failed.**

**Full backend regression suite:** **2,518 collected, 2,493 passed, 25
skipped, 0 failed, 0 errors.** (Previous checkpoint's stated baseline was
2,149 collected / 2,124 passed / 25 skipped / 0 failed; skip count is
identical, pass count is higher — consistent with "count may increase,
zero failures required.")

## Provider objects created

**None.** All tests use the sandbox payment provider
(`SandboxPaymentProvider`) and the existing synthetic/demo booking path.
No real Stripe or Duffel objects were created for this slice.

## Findings

- **Critical:** 0
- **High:** 0
- **Medium:** 1 found and fixed (the inconsistent CSRF enforcement itself, split across the two routes above — both closed).
- Independent adversarial review (below) found no further Critical/High/Medium issues.

## Known deferred CSRF Medium: **CLOSED**

Both cookie-authenticated, state-mutating consumer routes that were missing
`require_csrf` (`create_payment`, `create_booking_intent`) now enforce it
consistently with every other CSRF-protected route in the codebase. The
route audit above accounts for every `POST` route in the consumer API and
explains, for each one not protected, why CSRF does not apply (not
cookie-authenticated) rather than leaving it unexamined.

## Remaining CSRF gaps

None identified within this slice's scope. The booking-intent execution
family (`travelers`, `commercial`, `confirm`, ticket start/mark) remains
unauthenticated-by-cookie by deliberate design (capability-by-`booking_id`)
and is therefore out of CSRF's threat model as currently architected; if
that design ever changes to gate those routes on the session cookie, CSRF
enforcement must be added to them at that time.

## Independent adversarial review

Performed as a separate, read-only pass after implementation.

- **Forgotten cookie-authenticated mutation routes:** none found. Every route in `api/{auth,payments,me_trips,v1}.py` was enumerated by decorator (`grep '@router\.'`) and cross-checked against `get_optional_session`/`require_session` usage; `ops_*.py` confirmed Bearer-only (no cookie code path exists there at all).
- **Route-specific CSRF bypass:** none introduced — both fixes call the same, unmodified `require_csrf` used elsewhere; no new code path re-implements the check.
- **Alternate HTTP method bypass:** none possible — `grep '@router\.'` across `v1.py`/`payments.py`/`me_trips.py`/`routes.py` shows only `GET`/`POST` are ever registered; the two fixed actions have no `PUT`/`PATCH`/`DELETE` sibling route.
- **CSRF checked after dangerous side effect:** verified by reading both diffs directly — in each case `require_csrf` runs immediately after `get_optional_session` and before the first persistence write or provider call; only prior statements are read-only lookups/validation.
- **Ownership regression:** none — full IDOR/ownership test suites re-run and pass unchanged.
- **Auth regression:** none — `require_csrf`/`require_session`/`get_optional_session` themselves were not modified.
- **Payment/booking semantic regression:** none — no amount, currency, idempotency, capture, or booking-phase logic was touched.
- **Frontend incompatibility:** none — confirmed the frontend already sends the header unconditionally for non-GET requests; `client.ts` untouched by this slice and by the concurrent SEO commit.
- **Test-only protection the production route doesn't actually use:** not applicable — both fixes are in production route handlers (`payments.py`, `v1.py`), exercised through the real HTTP app via `TestClient(create_app())`, not mocked.

No further Critical/High/Medium issues found; no additional fixes required.

## Checkpoint commit

See git history for the commit created immediately after this report
(`V9 close booking and payment CSRF gap`), built on top of `8e11b805` (the
concurrent SEO slice, preserved untouched). Not pushed.
