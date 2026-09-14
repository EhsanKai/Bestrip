# V9 Phase 6 — Production Security & Ops Hardening (Slice 1 report)

Starting checkpoint: `2fbf4a8` (V9 Phase 5, APPROVED).

## Follow-up (commit `11f9eb1`): account → booking ownership — CLOSED

The item below ("Account → booking ownership: VERIFIED, NOT FIXED
(deferred)") is now fixed. `BookingRun` carries an `owner_user_id`, set
once at booking-intent creation from the server-resolved session (never
the request body); the existing `persist_run` choke point claims the trip
for that owner on every call, idempotently. `persistence.accounts.claim_trip`
itself was hardened in the same change - its existence-check-then-insert
was two separate lock acquisitions (a real TOCTOU race), now one
transaction. The `xfail(strict=True)` spec test in
`tests/test_v9_phase6_ownership_wiring.py` passes for real now and the
marker is removed; 15 more tests were added (IDOR, anonymous flow,
traveler-email-is-not-account-identity, idempotent retry, cross-user
denial, two concurrency scenarios, recovery-state visibility). Independently
adversarially reviewed (separate agent, read-only) - one Low finding (no
log line on a silent `claim_trip` failure), fixed. Full detail in the
commit message; release-blocker ledger item 4 below is updated to reflect
this closure.

## Verdict

**V9 PHASE 6 VERDICT: INCOMPLETE** (partial — real, tested, independently
reviewed progress on a bounded subset; the full 9-slice program this phase
is scoped to was not completed in one sitting, and is not claimed to be).

Two slices got full evidence-first treatment with a fix and a durable test
suite; several others got a real but bounded spot-check (confirmed existing
controls intact, no new fresh adversarial pass); the rest were not reached
this session. Nothing here is marked APPROVED/complete that wasn't actually
exercised — see the per-slice breakdown below.

## What this report covers

- **Slice 1 (auth abuse / login limiter): DONE.** Implemented, tested,
  independently adversarially reviewed by a separate reviewing agent (not
  self-approved), one real finding from that review fixed, full regression
  green.
- **Slice 2 (authorization / API security): PARTIAL.** One release-debt
  item verified precisely (account→booking ownership: confirmed NOT wired,
  with hard evidence and a durable spec test) and deliberately not fixed in
  this same slice (see rationale below). One new gap found and fixed
  (request body size cap). Existing IDOR/CSRF/ownership controls
  spot-checked, not freshly re-attacked from scratch.
- **Slice 9 (ops): one item.** Found and fixed an unrelated brute-force gap
  on the Ops shared-token exchange while investigating authorization
  patterns for Slice 2.
- **Slices 3 (payment), 4 (booking/provider), 5 (network/SSRF), 6
  (PII/logging), 7 (secrets/config), 8 (dependencies): spot-checked, not a
  full fresh adversarial pass.** Slice 8 is complete (dependency scan is a
  point-in-time check, genuinely finished). Slices 3-7 had existing
  controls read and spot-verified sound, and slice 3's real blocker (Stripe
  Test Mode E2E) was not run, as expected without credentials - see ledger.

## Slice 1 — Auth abuse / login limiter hardening

### The problem

The pre-existing login rate limiter keyed its window purely on the
normalized email (`services/rate_limit.py` + `auth_service.login`). An
unauthenticated attacker who merely knew a victim's email address could
submit enough failed attempts to trip that email's limiter window and deny
the victim's own logins for the remainder of the window - repeatable
indefinitely. The same limiter's backing dict also had no eviction, so a
high-cardinality flood of distinct keys was an unbounded-memory vector.

### The fix

- **`services/rate_limit.py`**: `RateLimiter` is now bounded (LRU eviction
  at `max_entries`, default 50,000) and self-sweeping (opportunistic
  removal of naturally-expired windows every 30s of caller-supplied `now`).
- **`services/client_ip.py`** (new): resolves a trustworthy client IP.
  Ignores `X-Forwarded-For` entirely unless `AUTH_TRUSTED_PROXY_HOPS` is
  explicitly configured (default 0 = don't trust it), in which case exactly
  the N-th-from-the-right entry is trusted - the one a real, N-hop-deep
  trusted proxy chain would have appended itself, immune to a client
  forging its own prefix entries. Documented for this project's actual
  target (Render, one edge hop).
- **`auth_service.login`**: two independent budgets replace the single
  email-only one - a coarse per-IP volume control (`login_ip` scope,
  catches one source cycling through many identifiers) and a short, soft
  per-`(client_ip, email)` throttle (`login_pair` scope, never resettable
  by an attacker who lacks the victim's IP). Only the pair budget resets on
  success; the IP budget does not (so a lucky guess against one of many
  accounts can't reset an attacker's own room to keep guessing).
- **`api/auth.py`**: resolves and threads `client_ip` into both `login` and
  `register`.
- **`api/app.py`+`api/body_limit.py`**: unrelated at first glance but
  landed in this same slice - see below.

### Independent adversarial review (not self-approved)

A separate reviewing agent (read-only, no write access) was given the diff
and asked to try to break it: lock out a victim, bypass throttling, exhaust
limiter memory, spoof the source IP (at both `hops=0` and `hops=1`), enable
account enumeration, leak exceptions, or cause pathological state growth.

**Result: clean on all of those, but it found one real, valid gap the
diff itself had missed** - `auth_service.register()` was left on the exact
same email-only scope the whole slice was about removing. An attacker who
knows only a victim's email can burn that email's entire *registration*
budget with throwaway requests, denying the real person the ability to sign
up with it for as long as the attacker cares to repeat it. Lower severity
than the login bug (it can't be used against an *existing* account/session,
only to block a brand-new signup), but a real gap in the same class this
slice exists to close. **Fixed**: `register()` now uses the identical
`(client_ip, email)` pair scoping as `login()`.

### Tests

`tests/test_v9_phase6_login_limiter.py` (22 tests): victim-cannot-be-
locked-out-by-email-alone (login and, after the fix, register too),
different-IP-not-blocked, per-IP volume control, success resets only the
targeted budget, enumeration safety, expired-entry sweeping, bounded memory
under a 10,000-key flood, LRU eviction order, 500-thread concurrency
(exact count, no corruption), malformed/oversized/weird key text, IPv4/IPv6
handling, trusted-proxy-hop arithmetic (including the "attacker forges a
prefix, trusted hop still wins" case), and an end-to-end HTTP-level test
through the real app with `X-Forwarded-For`. Plus two tests for the new
Ops login rate limit (see Slice 9).

Full regression: **2153 tests collected, all passing** (up from 2119 at the
Phase 5 checkpoint; only environmental skips, unchanged from Phase 5's
report). Targeted files re-run individually and via the full suite.

## Slice 2 — Authorization / API security

### Account → booking ownership: VERIFIED, NOT FIXED (deferred)

The pre-Beta debt ledger flagged this as needing verification. It does:
`persistence/accounts.claim_trip` - the only thing that ever populates the
`trip_ownership` table - **has no caller anywhere in `src/detoura` outside
its own definition.** Confirmed two ways: (1) an AST-based static scan of
every production source file for any reference to `claim_trip`, (2) an
end-to-end test that registers a user, logs them in, creates a real booking
intent through the actual HTTP booking-creation flow, and checks
`get_trip_owner`/`list_trip_ids_for_user` - neither ever reflects the
booking. This is a **functional completeness gap** (My Trips can never be
populated for any real account today), not an authorization hole - nothing
is over-exposed; the failure mode is that ownership is never recorded, not
that it leaks.

Both are captured as permanent tests in
`tests/test_v9_phase6_ownership_wiring.py`:
`test_claim_trip_has_no_production_caller_yet` (plain, stays green until
someone wires a caller) and
`test_a_signed_in_users_booking_intent_is_claimed_as_theirs`
(`xfail(strict=True)` - specifies the *desired* behavior; starts failing
the run, on purpose, the moment someone wires it, so the marker has to be
removed and the test becomes a real regression test at that point).

**Why not fixed in this same slice**: the only correct hook point found
(`services/booking_persistence.persist_run`, called from every state-
changing booking-intent endpoint) operates on `BookingRun` - the central
orchestrator object shared across Phase 4/7/8's payment, ticketing and
recovery logic. Wiring an optional session through booking-intent creation
and into that object is a real, cross-cutting change to the single most
sensitive data model in the codebase, not a security-hardening change with
a small, containable diff. Rushing it under this phase's "harden, don't
rewrite" mandate risked exactly the kind of change this project's own
Phase 5 discipline warns against. Recommended as its own dedicated,
carefully-tested follow-up slice, not folded into Phase 6.

### Request body size limit: FOUND AND FIXED

No layer of the app capped request body size - FastAPI/Starlette buffer an
entire body into memory to parse it before any Pydantic `max_length` field
validation ever runs, so a single oversized body was an easy way to spend
memory on any route, including ones whose eventual validated fields are
tiny (e.g. login). `api/body_limit.py` (new): a pure ASGI middleware
(deliberately not Starlette's `BaseHTTPMiddleware`, which would defeat the
point by buffering the whole body itself first) that refuses via
`Content-Length` before reading a byte when possible, and otherwise counts
bytes as they stream in and refuses mid-stream - catching a missing or
dishonest `Content-Length` too. Default 2 MiB (generous: this API has no
file-upload endpoint, and its own tightest structural limits, e.g. 64
recheck legs, top out far below that in practice).

Caught one real, legitimate pre-existing test in the process
(`tests/test_v65_request_limits.py::test_a_flood_of_recheck_legs_is_refused`,
a deliberately-oversized 37,500-leg adversarial payload) that expected a
422 from the endpoint's own semantic `max_length` validator specifically;
updated it to accept either 422 or the new, earlier, cheaper 413 - both are
a correct refusal of the same adversarial input, and the byte-cap path is
strictly less expensive (rejects before Pydantic ever allocates 37,500
objects).

### IDOR / CSRF / session / ownership: spot-checked, not re-audited from scratch

Read (not rewritten): the double-submit CSRF check (`api/auth.py`), cookie
flags (`HttpOnly`+`SameSite=Lax`+`Secure`-in-production on the session
cookie), and confirmed the existing green test suite still covers IDOR
safety (`test_ownership_checked_reads_are_idor_safe`), cross-route
ownership (`test_route_ownership_checks_both_levels`), and anti-
enumeration response shaping in `me_trips.py`. No new fresh adversarial
attack was mounted against these this session - they were not touched by
this slice's diff and their existing test coverage still passes.

## Slice 9 — Ops brute-force gap (found while reviewing Slice 2)

`POST /api/v1/ops/session` (the shared-secret `DETOURA_OPS_TOKEN` exchange
that gates the entire admin console - payment reconciliation, refunds,
ticket ops) had **no rate limiting at all**. `secrets.compare_digest`
defeats a timing attack, not a volume one; an operator-chosen weak or short
token was one unthrottled guessing loop away from being crackable at
network speed. Fixed: `api/ops_auth.check_ops_login_rate_limit`, a plain
per-IP volume cap (`OPS_LOGIN_MAX_ATTEMPTS`/`OPS_LOGIN_WINDOW_SECONDS`,
default 10/300s) reusing the same generic `RateLimiter`, checked before
`ops_enabled()`'s 503 is ever reached... actually checked *after* - the
"is this deployment even configured" answer must not itself require
spending part of an IP's guess budget to learn. Tests confirm the ordering
(`503` when unconfigured, regardless of rate-limit state) and that the cap
is a pure volume control (fires even on the eventually-correct token, once
an IP's budget for that window is spent).

## Slices not reached this session

**Slice 3 (payment)**: no fresh adversarial pass; existing Phase 4 tests
(webhook signature + replay-tolerance verification, `claim_provider_event`
idempotency, three-gate live-key fail-closed design in `payment_config.py`)
were read and spot-confirmed sound, not re-attacked. **Real Stripe Test
Mode server E2E: NOT RUN** - no credentials available in this environment;
this was expected and is not a new blocker, it's the same one carried from
Phase 4/5.

**Slice 4 (booking/provider security)**: not audited this session beyond
what the full regression already exercises.

**Slice 5 (network/SSRF)**: Phase 2.5's existing network-safety test suite
(`test_v9_phase25_network_safety.py`) confirmed still green; not rewritten,
per instruction to reuse rather than redo, but also not freshly attacked.

**Slice 6 (PII/logging)**: a targeted grep for password/token/secret
patterns near logging/print calls found nothing (the one hit,
`tools/duffel_probe.py`, already redacts before printing); the broad
`except Exception` / `str(error)` pattern used across `api/*.py` was spot-
checked in `v1.py`'s search handler and confirmed the raw exception detail
stays server-side (only a generic message reaches the client) - not
exhaustively checked across all ~30 similar call sites.

**Slice 7 (secrets/config)**: `.dockerignore` excludes `.env*`; no tracked
`.env` files; manual grep for live-looking key patterns in tracked source
found none (Phase 5 report has the detail); Stripe's three-gate fail-closed
live-mode design (`payment_config.py`) read and looks sound.

**Slice 8 (dependencies): complete.** `npm audit` (frontend): 0
vulnerabilities. `pip-audit` (backend): 0 known vulnerabilities. No updates
needed.

## Release-blocker ledger

Carried from Phase 4/5, still open (none of these are Phase 6
implementation items unless noted):

1. Consumer `/api/v1/search` still needs connecting to the real Phase 3
   intelligence pipeline.
2. Real Stripe Test Mode server E2E - **not run, no credentials**.
3. Fresh independent Phase 4 payment adversarial QA - **not done this
   session** (Slice 3 was spot-checked, not re-attacked).
4. Account→booking ownership wiring - **CLOSED** (commit `11f9eb1`, see the
   follow-up section at the top of this report).
5. EU legal/tax/payment/package-travel specialist review - external,
   unstarted.
6. Destination-image manual review backlog - unchanged.
7. Login limiter targeted-DoS / memory-growth - **CLOSED** this slice.

New items opened this session:

8. `register()` email-only-scope DoS - **CLOSED** this slice (same fix as
   login).
9. No request body size cap - **CLOSED** this slice.
10. Ops shared-token exchange had no brute-force throttle - **CLOSED**
    this slice.
11. Slices 3-6 need a genuine fresh adversarial pass (not just a spot-
    check) before Phase 6 can be declared APPROVED.
12. Slice 4 (booking/provider security) and the rest of Slice 9 (ops
    recovery/reconciliation visibility beyond the login fix) not yet
    started.

## Tests / build / secret scan

- Full regression: 2153 tests, all passing (0 failures), only the same
  environmental skips as Phase 5 plus one intentional `xfail`.
- Secret scan: manual, as in Phase 5 - clean.
- `npm audit` / `pip-audit`: clean.
- Frontend build/typecheck: unaffected (backend-only diff).

## Commits

One Phase 6 commit on top of `2fbf4a8`, exact-path staged:
`src/detoura/services/rate_limit.py`, `src/detoura/services/client_ip.py`,
`src/detoura/services/auth_service.py`, `src/detoura/api/auth.py`,
`src/detoura/api/ops.py`, `src/detoura/api/ops_auth.py`,
`src/detoura/api/app.py`, `src/detoura/api/body_limit.py`,
`src/detoura/auth_config.py`, `tests/conftest.py`,
`tests/test_v65_request_limits.py`, `tests/test_v9_phase6_login_limiter.py`,
`tests/test_v9_phase6_body_limit.py`,
`tests/test_v9_phase6_ownership_wiring.py`, this report.

No frontend files, no `AGENTS.md`/`CLAUDE.md` staged - all preserved
untouched.
