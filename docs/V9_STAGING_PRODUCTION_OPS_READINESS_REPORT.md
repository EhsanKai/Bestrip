# V9 — Staging / Production Ops Readiness

Deployment, configuration, security and release-gate audit of the Detoura
repository as it stands at the starting commit below. Scope: can this
application be deployed into a real staging environment safely and
reproducibly, and does the production release path fail closed on unresolved
configuration/legal/security requirements? This is not another application
security audit or privacy audit — see `V9_ACCOUNT_AUTH_DB_SECURITY_AUDIT.md`,
`V9_PHASE6_SECURITY_REPORT.md` and the privacy-slice reports for those.

Starting HEAD: `2421676` ("V9 implement Limited Beta privacy UI").

Classifications used below: **VERIFIED**, **READY**, **PARTIAL**, **BLOCKED**,
**EXTERNAL CONFIG**, **LEGAL GATE**, **OPS GATE**, **NOT APPLICABLE**.

---

## 1. Deployment inventory

| Component | File | Status |
| --- | --- | --- |
| Container build | `Dockerfile` | REAL — two-stage (`node:22-slim` builds the client, `python:3.11-slim` runs it), non-root user (uid 10001), `VOLUME /app/data` for the SQLite DB, single `uvicorn` process |
| Host blueprint | `render.yaml` | REAL, single service only — no staging/production distinction exists at the host-config level |
| ASGI server | `uvicorn` via `pyproject.toml`'s `api` extra | REAL — no gunicorn anywhere in the repo |
| Frontend build/serve | `frontend/` built by Vite, served same-origin by `src/detoura/api/static.py` | REAL |
| Reverse-proxy assumptions | none owned by this app | EXTERNAL PLATFORM RESPONSIBILITY (Render's edge, or whatever host is chosen) |
| Env/secret loading | raw `os.getenv` per module, no `python-dotenv`, no centralized settings object | REAL, by design — see §4 |
| Persistent volume | `VOLUME /app/data` (Dockerfile) → `DETOURA_DB_PATH=/app/data/detoura.db` | REAL |
| Worker/background execution | none — no `BackgroundTasks`, no scheduler, no cron anywhere in `src/` | MISSING (not needed today; see §14) |
| Health endpoint | `GET /health` (root) and `GET /api/v1/health` (product) | REAL |
| Readiness endpoint | `GET /readyz` | REAL |
| Metrics endpoint | `GET /metrics` (Prometheus text) | REAL, opt-in, unauthenticated by design |
| Logging destination | stdout, JSON, one process-wide handler | REAL; retention is EXTERNAL PLATFORM RESPONSIBILITY |
| Scheduled jobs | none | NOT APPLICABLE |
| Static/media serving | `static.py` (SPA + `/assets` caching), `destination_images.py` | REAL |
| SPA fallback | catch-all route, reserved `/api`, `/docs`, `/redoc`, `/openapi.json` prefixes excluded and return JSON 404 | REAL |
| docker-compose | — | NOT FOUND anywhere in the repo |
| CI | `.github/workflows/ci.yml` — tests, frontend build, Docker image + smoke test | REAL; **no deploy step exists** — Render's `autoDeploy: true` is the only thing that ships a build, and it is ungated |

`DEPLOY.md`'s "What is not here" section was stale (claimed no DB, no auth, no
rate limiting, no booking — all false against the current `src/` tree) and has
been corrected in this slice; see §21.

---

## 2. Environment / secret contract

Full table now lives in `DEPLOY.md` (Configuration section) rather than
duplicated here — that is the file an operator actually opens. Summary of the
fail-closed shape, which is consistent across every provider config module
(`payment_config.py`, `communication_config.py`, `google_auth_config.py`,
`auth_config.py`):

- Every sensitive value (`STRIPE_SECRET_KEY`, `RESEND_API_KEY`,
  `GOOGLE_CLIENT_SECRET`, `DETOURA_OPS_TOKEN`, `DUFFEL_ACCESS_TOKEN`) is read
  directly from `os.environ` at the point of use and never stored on a config
  dataclass — a diagnostics dump of any config object cannot leak one.
- Every provider has an explicit master kill-switch
  (`PAYMENT_LIVE_CHARGING_ENABLED`, `COMMUNICATION_LIVE_SENDING_ENABLED`)
  defaulting to `false`, checked *before* the provider name, so a
  misconfigured provider name alone can never cause a live side effect.
  Stripe additionally refuses any key not shaped `sk_test_...`, and the Duffel
  adapter refuses any token not shaped `duffel_test_...`, before opening a
  socket.
- No `python-dotenv` (or equivalent) exists anywhere in this codebase, in
  either `pyproject.toml` or `src/`. This was verified, not assumed — a repo
  grep for `dotenv` returns nothing. A deployment must inject every variable
  explicitly.
- `.env` exists locally, is listed in `.gitignore` (`.env`, `.env.local`,
  `.env.*.local`) and is confirmed **not** git-tracked (`git ls-files .env`
  returns empty). Its contents were not read or printed as part of this audit.

**One real, demonstrated gap fixed in this slice:** `render.yaml` — the only
production-shaped host config in the repository — declared no `DETOURA_ENV`
and no `AUTH_TRUSTED_PROXY_HOPS`. Left as-is, the one deployment this
repository is actually configured to ship to would run with `is_production =
False`: session/CSRF cookies would never get the `Secure` flag despite being
served over Render's HTTPS, and `X-Forwarded-For` would be ignored entirely
for rate-limiting purposes (safe, but coarser than intended — every visitor
behind Render's edge would share one bucket). Both are now declared explicitly
(see the `render.yaml` diff, §20). This is a config fix, not a code defect — the code
already had the right fail-safe default and the right documented Render
guidance in `services/client_ip.py`'s own docstring; the host blueprint simply
never acted on it.

---

## 3. Staging isolation

READY, contingent on the operator following the runbook (`V9_STAGING_RUNBOOK.md`):

- Stripe/Duffel: fail closed to TEST/sandbox by construction (§16) — a
  staging deploy cannot accidentally go live merely by having a
  production-looking key pasted in; the adapters themselves refuse it.
- Analytics/attribution/marketing: OFF by construction, not by staging-specific
  configuration — see §17. No staging-specific action needed.
- Ops endpoints: `DETOURA_OPS_TOKEN` unset ⇒ the entire Ops console 503s.
  Staging that wants Ops reachable must set a token that is **not** the
  production one.
- Public indexability: this was the one real, previously-unaddressed gap
  (`V9_TECHNICAL_SEO_FOUNDATION_REPORT.md` explicitly flagged it as future
  work) — **closed in this slice**, see §9.
- Production customer data: not applicable yet — there is no production
  deployment with real customer data to accidentally copy into staging.

---

## 4. HTTPS / proxy / host security

- Scheme/host/client-IP resolution: this application does **not** run
  `TrustedHostMiddleware` or any `ProxyHeaders` ASGI wrapper. `X-Forwarded-For`
  is read manually, once, in `services/client_ip.py`, and is **ignored by
  default** (`AUTH_TRUSTED_PROXY_HOPS=0`) in favor of the raw TCP peer
  (`request.client.host`, which uvicorn sets from the real socket and which is
  never attacker-suppliable). This is a deliberate, well-documented fail-safe:
  trusting a wrong hop count is a spoofing hole, so the default is "trust
  nothing" rather than "guess."
- Render-specific requirement (now declared in `render.yaml`, §2):
  `AUTH_TRUSTED_PROXY_HOPS=1`, because Render terminates TLS at its edge and
  proxies to the container as the single hop in front of it.
- Secure-cookie behavior: gated on `auth_config().is_production`, which is
  `DETOURA_ENV=production` (or the boolean `DETOURA_ENV_PRODUCTION`) — see §6.
- Responsibility split: scheme/TLS termination is EXTERNAL PLATFORM
  RESPONSIBILITY (Render's edge, or whatever host is chosen); trusted-hop
  count and what to do with the resulting client IP is this application's
  responsibility and is correctly implemented.

---

## 5. Cookie / session deployment verification

VERIFIED against source (`src/detoura/api/auth.py::_set_auth_cookies`,
`auth_config.py`):

| Cookie | HttpOnly | Secure | SameSite | Path | Max-Age |
| --- | --- | --- | --- | --- | --- |
| `detoura_session` | Yes | `auth_config().is_production` | `lax` | `/` | 14 days (`AUTH_SESSION_TTL_SECONDS`, default) |
| `detoura_csrf` | **No** (double-submit requires JS to read it) | `auth_config().is_production` | `lax` | `/` | 14 days |

The `Secure` flag is environment-dependent by design, not a defect — a plain
HTTP local-dev server cannot set a `Secure` cookie the browser will actually
send back. The real risk was that the one production-shaped deployment
(`render.yaml`) never set `DETOURA_ENV=production`, so `Secure` would silently
never have been true in what Render calls production. **Fixed — see §2.**

CSRF is a per-route double-submit check (`require_csrf`, not global
middleware): header `X-CSRF-Token` must be present, must equal the
`detoura_csrf` cookie value, and must hash to the session's stored
`csrf_token_hash`. `POST /api/v1/payments` and `POST /api/v1/booking-intents`
were the last two mutating routes missing this check
(`V9_CSRF_HARDENING_REPORT.md`); both now enforce it and reject with 403
before any side effect. `submit_travelers`/`confirm_booking` remain
intentionally uncovered — they authenticate by unguessable `booking_id`
capability, not by cookie, so CSRF does not apply to them the same way. Ops
routes use Bearer tokens only and are unaffected by any of this.

**VERIFIED**

---

## 6. Security headers

**MISSING**, confirmed by a full-repo grep (`src/`, `frontend/index.html`,
`vercel.json`, `netlify.toml`): no `Content-Security-Policy`,
`Strict-Transport-Security`, `X-Content-Type-Options`, `Permissions-Policy`,
`Referrer-Policy`, or `X-Frame-Options` exists anywhere in this codebase
today.

This slice deliberately does **not** add a CSP. The task instruction to
inspect current Wikimedia media, Google Sign-In navigation, and provider
requests before defining one stands, and a wrong CSP is worse than none — a
too-strict policy silently breaks Google's OAuth redirect flow or the
destination-image CDN fetches in production with no clear error message,
which is a materially worse failure mode than the current absence. **This is
recorded as an explicit, disclosed OPS GATE for a dedicated follow-up slice**,
not something waived silently. The one header this slice *does* add
(`X-Robots-Tag`, §9) is narrowly scoped and was already required by name in
the task brief.

HSTS/TLS termination is EXTERNAL PLATFORM RESPONSIBILITY (Render's edge or
equivalent) — this application never terminates TLS itself.

**OPS GATE** (headers) / **NOT APPLICABLE** (HSTS/TLS — platform-owned)

---

## 7. Staging SEO / privacy — production indexability

**Fixed in this slice.** Before this slice, `V9_TECHNICAL_SEO_FOUNDATION_REPORT.md`
already documented this exact gap: the build always emitted
`<meta name="robots" content="index,follow">` regardless of target
environment, and robots.txt always allowed public routes — there was no way
to produce a non-indexable build at all, staging or otherwise, and the
report's own "Deployment Requirements" section named this as unaddressed
future work.

Two changes close it, matching the instruction not to rely on a single HTML
meta tag alone:

1. **Build-time (`frontend/vite.config.ts`)**: new opt-in `VITE_STAGING=true`
   flag. When set, the build emits `noindex,nofollow`, a blanket
   `Disallow: /` robots.txt, and no sitemap — regardless of whether
   `VITE_PUBLIC_SITE_URL` also happens to be set. Unset (the default),
   behavior is byte-for-byte what it was before this slice — verified by an
   actual `npm run build` with and without the flag and diffing the emitted
   `dist/robots.txt`/`dist/index.html`.
   The Docker image build stage did not itself forward `VITE_STAGING` into
   the client build until this was caught during verification — the
   Dockerfile only declared `ARG`/`ENV` for `VITE_API_BASE`. Fixed by adding
   the identical `ARG VITE_STAGING=""` / `ENV VITE_STAGING=${VITE_STAGING}`
   pair used for `VITE_API_BASE`, one line above it. **Docker itself could
   not be exercised in this environment** (the daemon is not running here —
   `docker info` fails to reach it), so this fix is verified statically
   (identical, already-proven mechanism as `VITE_API_BASE` in the same
   build stage) rather than by an actual `docker build`; the plain
   `npm run build` path (used directly, without Docker) was verified for
   real by building twice and diffing the emitted `dist/robots.txt` and
   `dist/index.html` with and without the flag.
2. **Runtime, server-side (`src/detoura/api/app.py`)**: a new response-header
   middleware sets `X-Robots-Tag: noindex, nofollow` on every response unless
   `auth_config().is_production` is true — the same flag §2/§25 now declares
   in `render.yaml`. This is real HTTP-header-level enforcement that a
   non-JS-executing crawler will also respect, and it costs no new
   environment variable: any deployment that has not explicitly opted into
   `DETOURA_ENV=production` is, by this application's own existing
   definition, not production, and is now correctly non-indexable by default.
   Focused tests: `tests/test_v9_observability_baseline.py`
   (`test_non_production_deployment_is_noindexed_by_default`,
   `test_production_deployment_is_not_noindexed`).

Per-route runtime `noindex` for `/ops` and unknown paths (`applyNoIndexSeo` in
`frontend/src/main.tsx`) was already correct and is unchanged. Static hosting
rewrites (Vercel/Netlify) still return HTTP 200 for unknown paths
(soft-404) — a true 404 needs hosting/server support and remains documented,
disclosed future work, not something this slice invents infrastructure for.

**VERIFIED**

---

## 8. Production legal release gate

**LEGAL GATE — currently and correctly failing closed.**

`frontend/src/privacy/legalConfig.ts` is the single source of truth for
`LEGAL_ENTITY_NAME`, `REGISTERED_ADDRESS`, `PRIVACY_CONTACT_EMAIL`,
`PRIVACY_NOTICE_EFFECTIVE_DATE`, `COMPETENT_SUPERVISORY_AUTHORITY_NAME`,
`COMPETENT_SUPERVISORY_AUTHORITY_URL`, and
`COMPANION_TRAVELLER_LEGAL_NOTICE_APPROVED`. All seven are currently empty or
`false`. `frontend/scripts/verify-legal-readiness.mjs` validates them
(non-empty, non-placeholder, valid email/date/HTTPS-URL shape, and the
companion-traveller flag must be exactly `true`) and exits 1 — confirmed by
running it directly during this audit. It is wired into
`npm run build:release`, which `frontend/vercel.json` and
`frontend/netlify.toml` already call for the split-hosting deployment path.

**Real, demonstrated gap found and fixed:** the root `Dockerfile` and CI's
`image` job both call plain `npm run build`, never `build:release` — the
legal gate was reachable for one of the two documented deployment
arrangements and not the other, which is exactly the "production release
process can silently bypass privacy/legal readiness" failure mode this task
was written to catch.

The fix is **not** to make the Dockerfile/CI call `build:release`
unconditionally — doing so would fail every staging build, every PR build,
and every CI run outright, today, since the legal fields are genuinely empty
by design pre-legal-review (`docs/V9_LIMITED_BETA_PRIVACY_UI_REPORT.md`
explicitly requires staging to "remain deployable... with an explicit
non-production legal-draft state"). Instead: a new, standalone
`scripts/verify_production_release.sh` runs the same
`npm run verify:legal` check as a deliberate, manually-invoked gate
immediately before an actual production release of the single-origin image —
decoupled from the generic (and correctly always-buildable) Docker/CI path.
Run and confirmed to fail closed with the current (empty) legal configuration
during this audit (see §22).

No backend code references any of these seven fields — this gate is entirely
frontend/Node-side. Nothing was set to a placeholder or invented value in
this slice, and `COMPANION_TRAVELLER_LEGAL_NOTICE_APPROVED` remains `false`,
as instructed.

**LEGAL GATE**

---

## 9. Logging / 14-day retention

- Logs go to stdout only, as structured JSON (`observability/logging.py`) —
  no file, no rotation, no shipping/aggregation/alerting backend is wired up
  anywhere in this codebase (`V9_PRODUCTION_OBSERVABILITY_BASELINE_REPORT.md`
  states this explicitly). Retention is entirely **EXTERNAL PLATFORM
  RESPONSIBILITY** — there is no in-app log storage to enforce a duration
  against, so there is nothing to "fake"-delete.
- The 14-day ordinary-operational-log retention policy
  (`V9_LIMITED_BETA_PRIVACY_POLICY_IMPLEMENTATION_REPORT.md` §0/§8,
  `V9_RETENTION_REGISTER.md` §C) is a **deployment/platform configuration
  requirement**, documented in `V9_STAGING_RUNBOOK.md` and the production
  checklist as an explicit action item for whichever host is chosen (Render
  log retention settings, or an external sink's own retention policy) —
  **not implementable in application code** and not attempted here.
- Do not conflate this with the unrelated 14-day *session* TTL
  (`AUTH_SESSION_TTL_SECONDS`, a security/session-lifetime setting in
  `auth_config.py`) — same number, two unrelated policies, confirmed by
  reading both source locations directly.
- Forbidden-field redaction (`FORBIDDEN_FIELDS` in `observability/logging.py`)
  covers passwords, tokens, session/CSRF secrets and card data at the
  application layer, verified by existing tests
  (`test_v9_observability_baseline.py`, `test_v9_phase6_pii_security.py`) and
  re-confirmed still passing in this slice's regression run (§22).

**EXTERNAL CONFIG**

---

## 10. Metrics / health / readiness

- `GET /health` (both mount points): static liveness stub, never touches the
  DB or any provider — verified live and by an existing test that monkeypatches
  `get_db` to raise and confirms `/health` is unaffected.
- `GET /readyz`: checks DB connectivity (`SELECT 1`) and that the *configured*
  communication provider resolves (a pure config check, never a network call
  to Resend) — 503 on either failure, 200 otherwise. Deliberately does not
  call Duffel/Stripe/Resend over the network, so a transient provider outage
  never pulls a healthy process out of a load balancer's rotation.
- `GET /metrics`: Prometheus text, gated behind `DETOURA_METRICS_ENABLED`
  (off by default), and **has no authentication of its own** — the code
  comment and the observability report both say so explicitly. This is
  correctly classified as safe to enable only behind network-level access
  control (internal-only network path, or a reverse-proxy rule), never
  directly on the open internet. Documented as a required deployment
  constraint in the runbook rather than "fixed" — adding auth to a metrics
  endpoint that Prometheus itself would then need credentials for is a
  larger, cross-cutting change outside a narrow ops-readiness fix, and the
  off-by-default posture already makes the safe default the one that ships
  with no configuration at all.

Classification: health = safe to expose publicly; readiness = safe to expose
publicly (no secrets, no internals beyond ready/not-ready); metrics = **must
be network-restricted if enabled at all**.

**VERIFIED** (health/readiness) / **OPS GATE** (metrics network policy — must
be enforced at the network/proxy layer, cannot be enforced by an unauthenticated
Prometheus scrape target)

---

## 11. Database / persistence

- SQLite, stdlib `sqlite3` only, WAL mode, **one connection, every access
  serialized** (`persistence/db.py`) — confirmed by reading the module
  docstring and the `PRAGMA journal_mode=WAL` call site.
- Path: `DETOURA_DB_PATH`, else `<cwd>/detoura.db`. The Dockerfile sets it to
  `/app/data/detoura.db` inside a declared `VOLUME /app/data` — persists
  across container restarts **only if that volume is actually mounted** by
  whatever host runs the image (`docker run -v detoura-data:/app/data ...`,
  or the host's persistent-disk equivalent). This is a real, disclosed
  deployment requirement, not something the Dockerfile alone can guarantee.
- No migration framework (no Alembic, confirmed absent from `pyproject.toml`)
  and no `DATABASE_URL`-style config — this is not Postgres/MySQL, and this
  slice does not introduce one, per instruction.
- **Multi-replica safety: NOT SAFE**, and this is a genuine, load-bearing
  constraint, not a paperwork gap. Two independent single-process resources
  live in-memory per worker/replica and silently diverge across more than
  one: (1) the personalization/session store defaults to `memory` and the
  Dockerfile's own comment states `WEB_CONCURRENCY` must equal `--workers`
  because of it — `DEPLOY.md`'s own benchmark table shows the exact silent
  corruption (`[1,2,3,4,5,1,2,6,3,4,7,8]` instead of `[1..12]`) that results
  from running more than one worker without `DETOURA_SESSION_STORE=redis`;
  (2) **newly confirmed in this audit**, the Ops console's session store
  (`api/ops_auth.py`, a bare in-process `dict`) has no shared-store option at
  all — an operator who logs into Ops on one replica/worker and is then
  routed to another on a subsequent request gets a spurious 401. The current
  safe supported topology is **exactly one process, one worker**
  (`WEB_CONCURRENCY=1`, the Dockerfile default) unless Redis is configured
  for the session store — and Ops sessions have no fix available in this
  slice short of adding a new shared-store dependency, which is out of scope
  for a narrow ops-readiness slice. This is documented as a hard topology
  constraint in the runbook, not silently worked around.
- No backup/restore mechanism exists in code — confirmed absent by search.
  See §12 for the minimum contract this slice defines instead of inventing
  in-app tooling.

**PARTIAL** (persistence itself is correct and durable; multi-replica/backup
are documented constraints and gates, not implemented mechanisms)

---

## 12. Backup / restore (minimum Limited Beta contract)

No sophisticated disaster-recovery platform is introduced, per instruction.
The minimum contract, given the actual architecture (§11):

- **What must be backed up**: the single SQLite file at `DETOURA_DB_PATH`
  (accounts, sessions, bookings, payments, Ops audit trail, commercial
  pricing policy/promo state) and the mounted volume it lives on.
  `destination_images` manifest/assets are build-time artifacts baked into
  the image, not runtime state — they do not need backing up.
- **Consistency**: SQLite in WAL mode is safe to file-copy while the process
  is running only if the copy tool is WAL-aware (e.g. the SQLite `.backup`
  command / `VACUUM INTO`, or a filesystem/volume snapshot taken atomically);
  a naive `cp` of the main DB file without also capturing the `-wal`/`-shm`
  files alongside it can produce an inconsistent copy. The runbook specifies
  `sqlite3 <path> ".backup '<dest>'"` for exactly this reason.
- **Encryption**: the backup contains the same data the live DB does
  (password hashes, not plaintext passwords; session-token hashes, not raw
  tokens; booking/payment metadata) — encrypt the backup artifact at rest
  using whatever the chosen host/storage already provides (most
  block-storage snapshot and object-storage products default to this) rather
  than building bespoke encryption into this slice.
- **Retention ownership**: platform/Ops-owned, same as log retention (§9) —
  not enforced by application code.
- **Restore procedure**: stop the single writer process, replace the DB file
  at `DETOURA_DB_PATH` with the backup, restart. No hot-restore path exists
  or is claimed.
- **Restore testing**: a periodic manual restore-and-boot check (documented
  as a checklist item in `docs/V9_PRODUCTION_RELEASE_CHECKLIST.md`) — this
  slice does not build automated restore verification tooling.
- **Provider truth reconciliation after restore**: a restored DB may disagree
  with Stripe/Duffel about payments/bookings made after the backup was taken.
  The existing Ops reconciliation actions (`ops_payments.py`,
  `ops_confirmations.py` — read visibility + explicit, domain-validated
  recovery actions, not a generic "set status") are the correct tool for this
  and already exist; this slice does not add new reconciliation logic, only
  documents that a restore must be followed by an Ops reconciliation pass.

**OPS GATE** (backup execution and restore testing are operational
procedures to be carried out on whatever host is chosen — this slice defines
the contract, not the automation)

---

## 13. Process / worker lifecycle

No code changes made or needed here — this slice only had to confirm the
deployment assumptions the existing, already-built recovery semantics
require:

- Booking/payment execution is synchronous within the request lifecycle;
  there is no separate background worker process to lose track of. A process
  restart mid-authorization/mid-issuance lands in the same
  `PAYMENT_UNKNOWN`/reconciliation-required states the booking orchestrator
  and payment service already define and that
  `V9_PRODUCTION_OBSERVABILITY_BASELINE_REPORT.md`'s
  `payment_reconciliation_required` event exists to surface — this is an
  existing, tested safe-recovery state (`test_payment_unknown_authorize_emits_reconciliation_required_event`,
  `test_booking_partial_failure_outcome_is_observable`), not a gap this slice
  needed to close.
- The one deployment assumption required to preserve this: **single-writer,
  single-process** (§11) — a restart of that one process is safe by
  construction; a second concurrent writer process is not, because nothing
  in the current SQLite/session-store architecture coordinates across
  processes beyond WAL's own locking.
- Email sending (Resend adapter) is synchronous per-call, not queued — a
  process restart between a successful booking and a not-yet-sent
  confirmation email is a known, disclosed gap in
  `V9_PRODUCTION_TRANSACTIONAL_EMAIL_REPORT.md`'s own scope, not something
  this ops slice re-solves.

**NOT APPLICABLE** (no new deployment risk found beyond the single-writer
constraint already documented in §11)

---

## 14. Provider configuration readiness

| Provider | Code ready | Config ready | Credential present (this env) | TEST/sandbox verified | Real E2E verified | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Stripe | YES | YES | NO (this audit environment) | YES (prior slice, real Stripe TEST mode) | NO (production/live) | LIVE charging is impossible today regardless of config — key must be `sk_test_`-shaped or the adapter refuses |
| Duffel | YES | YES | NO | YES (prior slice, real Duffel TEST mode, both legs issued) | NO — and **BLOCKED by code, not just by config**: the adapter hard-refuses any non-`duffel_test_`-prefixed token before opening a socket | Live order issuance does not exist in this codebase yet, by design |
| Google Sign-In | YES | NO (no client id/secret/redirect URI in this environment) | NO | Integration-tested (no live network) | BLOCKED — credentials unavailable | 503s cleanly when unconfigured |
| Resend | YES | YES (needs `RESEND_API_KEY`+`RESEND_FROM_EMAIL`) | NO | N/A (sandbox adapter used for all current testing) | BLOCKED — no key, and this audit does not send email to any real address | Fails closed (raises) rather than silently using sandbox if misconfigured with live sending enabled |

These are **not conflated** in the classification above, per instruction.
Nothing in this slice attempted to create a live provider object of any kind.

**BLOCKED** (Google, Resend real E2E — credentials unavailable, not a code
defect) / **VERIFIED** (Stripe, Duffel TEST-mode E2E — prior slice, preserved
unchanged)

---

## 15. Analytics / attribution / marketing — must remain OFF

Re-verified, not re-implemented:

- `frontend/src/main.tsx` calls `initAnalytics()` with **no consent
  argument** — confirmed by reading the call site directly. A full grep of
  every non-test call site in `frontend/src` found no caller anywhere that
  grants `analytics: true`.
- The two-factor gate (`VITE_ANALYTICS_FIRST_PARTY==="true"` at build time,
  *and* an explicit runtime `analytics: true` consent grant) is unchanged by
  this slice and was not touched.
- Backend `/api/v1/events` remains allow-listed, PII-rejecting and rate
  limited regardless of the frontend gate.
- No cookie/consent-management UI exists or was built in this slice, per
  instruction.
- Marketing has no delivery adapter of any kind.

No infrastructure variable introduced in this slice (`VITE_STAGING`,
`DETOURA_ENV`, `AUTH_TRUSTED_PROXY_HOPS`) has any interaction with analytics —
confirmed by inspection, since `analytics.ts` reads only
`VITE_ANALYTICS_FIRST_PARTY` and an explicit runtime consent call, neither of
which this slice touches.

**VERIFIED**

---

## 16. Full-suite hygiene follow-up — `test_v85_ops_c2.py::test_create_activate_and_history`

Reproduced scientifically per instruction, not blindly fixed:

- **In isolation** (`pytest tests/test_v85_ops_c2.py::test_create_activate_and_history`):
  PASSES.
- **As its complete file** (`pytest tests/test_v85_ops_c2.py`): PASSES.
- **Full suite, pre-edit baseline** (`pytest tests/` at the exact starting
  commit `2421676`, before any change in this slice): 100% pass, no
  failures — several intentionally-skipped tests (`s`) but zero failures.
  The previously-reported failure **did not reproduce**.
- **Full suite, post-edit** (after this slice's changes): see §19.

**Classification: NOT REPRODUCIBLE** in this environment, on either the
pre-edit or post-edit tree, across a complete run of all 115 test files. This
matches the prior slice's own note that it "passed in isolation... and as its
complete file" — it now also passes as part of the full suite. Notably, no
test-order-randomizing plugin (`pytest-randomly`, `pytest-xdist`, etc.) is
installed (confirmed via `pip list`) — this suite always runs in the same,
deterministic file/definition order, which makes a one-off, order-dependent
pollution harder to explain and more likely to have been transient
(timing/filesystem/environment-specific) or already resolved by an
intervening commit than a live, reproducible ordering bug. Per instruction,
this is recorded honestly rather than either "fixed" (there is no
demonstrated root cause to fix) or silently waived (it is disclosed here, not
hidden). If it recurs, capture the exact failure output and the full ordered
test list from that run before attempting a fix.

---

## 17. Independent adversarial review

Performed as a separate, read-only pass after implementation, checking
specifically for each item the task named:

| Check | Finding |
| --- | --- |
| Production using test assumptions | **Found and fixed**: `render.yaml` never declared `DETOURA_ENV=production` — see §2. |
| Staging using live provider credentials | Not applicable — no staging credentials exist in this repo/environment to be live. Adapters fail closed regardless (§14). |
| Secrets committed/logged | None found. `.env` confirmed untracked; forbidden-field redaction confirmed by existing, still-passing tests. |
| Insecure cookies | Root-caused to the same `DETOURA_ENV` gap above; fixed at the config layer, not by changing cookie code, which was already correct. |
| Untrusted proxy headers | Correctly fail-closed by default (`AUTH_TRUSTED_PROXY_HOPS=0`); Render-specific value now declared. |
| Public metrics | Confirmed unauthenticated by design; documented as a network-layer requirement, not silently left ambiguous. |
| Indexable staging | **Found and fixed** — see §9. |
| Analytics accidentally enabled | Not found — two-factor gate intact, unchanged. |
| Legal gate bypass | **Found and fixed** for the Docker/CI path — see §8. |
| SQLite ephemeral filesystem | Not ephemeral as configured (`VOLUME` + `DETOURA_DB_PATH`), contingent on the operator actually mounting a persistent volume — documented as a runbook requirement, not assumed. |
| Unsafe multi-replica assumptions | **Found, documented, not silently fixed**: Ops console sessions are in-process memory with no shared-store option — see §11. |
| Missing backup path | Addressed with a minimal, documented contract (§12), not new automation. |
| Restart losing booking/payment truth | Not found — existing reconciliation states already handle this; verified via existing passing tests. |
| Fake log-retention enforcement | Not found — this slice explicitly avoids building fake in-app deletion of externally-owned logs. |
| Provider mode ambiguity | Not found — every provider fails closed with an explicit, loud error rather than an ambiguous fallback. |
| Release scripts bypassing validation | **Found and fixed** for the Docker/CI path — see §8; new `scripts/verify_production_release.sh` added as the explicit gate. |
| Test-order pollution incorrectly waived | See §16/§19 — not waived either way without reproduction. |

No additional defects beyond those already listed above were found in this
pass.

---

## 18. Documentation produced/updated in this slice

- `docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md` — this report.
- `docs/V9_STAGING_RUNBOOK.md` — new, exact staging deployment runbook.
- `docs/V9_PRODUCTION_RELEASE_CHECKLIST.md` — new, separate production
  release checklist.
- `DEPLOY.md` — corrected the stale "What is not here" section and expanded
  the configuration table into the actual, current env/secret contract
  (§2/§4) rather than duplicating it in a new file.
- `render.yaml` — added `DETOURA_ENV=production` and
  `AUTH_TRUSTED_PROXY_HOPS=1`.
- `scripts/verify_production_release.sh` — new, standalone production legal
  gate, decoupled from the staging-safe Docker/CI build path.
- `src/detoura/api/app.py` — new `X-Robots-Tag` middleware, gated on the same
  `is_production` flag the cookie code already uses.
- `frontend/vite.config.ts` — new opt-in `VITE_STAGING` build flag.
- `tests/test_v9_observability_baseline.py` — two new focused tests for the
  noindex middleware.

No consumer-facing UI or product behavior was changed. No frontend product
screen, copy, or user-facing flow was touched.

---

## 19. Final verification

Executed against the final executable state (all six code/config files
above applied; no further changes pending):

- **Focused**: `tests/test_v9_observability_baseline.py` — 22/22 pass,
  including the two new noindex-middleware tests.
- **Legal gate**: `scripts/verify_production_release.sh` — exits 1 against
  the current (empty) `legalConfig.ts`, exactly as required (§8).
- **Frontend build diff**: `npm run build` with and without
  `VITE_STAGING=true` produces byte-identical output to the pre-slice
  baseline when unset, and correctly emits `noindex,nofollow` +
  `Disallow: /` + no sitemap when set (§7).
- **Full regression** (`python -m pytest tests/`, all 115 files), run
  sequentially (never concurrently with itself), against this final state:
  - **Run 1**: 1 failure —
    `tests/test_v9_phase6_payment_security.py::test_concurrent_refund_race_never_exceeds_captured_amount`.
    This is a real-thread, `threading.Barrier`-synchronized race test,
    unrelated to anything in this slice: it calls `payment_service`
    functions directly in-process and never goes through the FastAPI
    app/middleware stack at all (confirmed by reading the test and the
    full call chain — no code this slice touched is reachable from it).
    Re-run in isolation **5/5 times, all passed** — confirming it is
    timing/scheduling-sensitive, not a deterministic break. A repo-wide
    grep found 16 test files using raw `threading.Thread`/`threading.Barrier`
    for race-condition coverage (idempotency, rate limiting, optimistic
    concurrency) — real-thread timing sensitivity under system load is a
    known risk class across this suite by design, not specific to this one
    test or to this slice.
  - **Run 2** (full suite, repeated once more to check consistency): **0
    failures**, 100% pass.
  - **Pre-edit baseline** (§16, run against the unmodified starting commit
    `2421676` before any change in this slice): **0 failures**, 100% pass.

  **Classification: NON-DETERMINISTIC INFRA ISSUE** (environment
  timing/scheduling sensitivity in a real-thread concurrency test),
  consistent with the suite's own extensive use of real-thread race tests.
  Not fixed in this slice — the failure is in payment/refund concurrency
  test code, unrelated to deployment/ops/config, and "fixing" test timing
  assertions in a security-critical payment module is out of scope for a
  narrow ops-readiness slice and risks masking a real race if the fix is
  wrong. Disclosed here rather than silently re-run until green and
  forgotten.
- **Docker**: the daemon was not reachable in this environment in either
  verification pass (`docker info` fails to reach
  `unix:///Users/a/.docker/run/docker.sock`) — the `VITE_STAGING` Docker
  build-arg wiring (§7) is therefore verified statically against the
  already-proven `VITE_API_BASE` mechanism in the same Dockerfile stage,
  not by an actual `docker build`. This is disclosed, not silently assumed.

---

## 20. Git discipline

At the start of this slice, `HEAD` was `2421676` ("V9 implement Limited
Beta privacy UI") with two unrelated untracked files present
(`AGENTS.md`, `CLAUDE.md`) — both preserved untouched throughout, per
instruction. `HEAD` never moved during this slice; everything below is
still working-tree state, not yet committed.

Files touched by this slice, and nothing else:

| Path | Change |
| --- | --- |
| `DEPLOY.md` | modified — stale "What is not here" section corrected, configuration table expanded |
| `Dockerfile` | modified — `VITE_STAGING` build-arg wiring |
| `frontend/vite.config.ts` | modified — opt-in staging-noindex build flag |
| `render.yaml` | modified — `DETOURA_ENV`, `AUTH_TRUSTED_PROXY_HOPS` declared |
| `src/detoura/api/app.py` | modified — `X-Robots-Tag` middleware |
| `tests/test_v9_observability_baseline.py` | modified — two new focused tests |
| `docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md` | new — this report |
| `docs/V9_STAGING_RUNBOOK.md` | new |
| `docs/V9_PRODUCTION_RELEASE_CHECKLIST.md` | new |
| `scripts/verify_production_release.sh` | new |

`AGENTS.md` and `CLAUDE.md` remain untracked and were never staged, read for
content, or modified. No other dirty path exists in the repository.

Suggested commit, staging only the files above by exact path (never `git
add .`/`git add -A`):

```
V9 establish staging and production ops readiness
```

No push.
