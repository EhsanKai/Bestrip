# V9 — Real Staging Deployment & Verification

Starting HEAD: `9eb416a` ("V9 establish staging and production ops
readiness"). This report records **actual evidence** from a real, running
container built from this repository's own `Dockerfile` — the identical
artifact Render's `render.yaml` blueprint builds and runs — not readiness
assumptions. See `docs/V9_STAGING_RUNBOOK.md` for the commands this
execution followed and `docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md`
for the design rationale behind each control exercised here.

Classifications used below: **VERIFIED**, **PARTIAL**, **BLOCKED**, **OPS
GATE**, **LEGAL GATE**, **NOT APPLICABLE**.

---

## 0. Why this is "real staging," and what it is not

Render has no CLI, no API token, and no `gh` CLI available in this
environment (`which render`, `env | grep RENDER`, `which gh` all came back
empty), and this session has no visibility into whether a Render service is
already connected to this repository's `origin` remote
(`https://github.com/EhsanKai/Bestrip.git`). `render.yaml` defines exactly
one service block; if one is already live and connected with
`autoDeploy: true`, a `git push` to the watched branch is the entire
remaining mechanical action to deploy there — but pushing blind, with no
way to confirm what (if anything) is currently connected or observe the
result, is exactly the kind of external, hard-to-reverse, shared-system
action this task's own instructions say to stop for rather than guess at.
**No push was made.**

What this report *does* verify, for real, locally: the exact same
`Dockerfile`, the exact same image, built and run with
`docker build --build-arg VITE_STAGING=true .` and `docker run`, is what
Render's blueprint builds and runs. Every claim below was checked against a
live container on `localhost:18000`, not inferred from source alone. This
is the strongest verification available without the one missing piece
(§13), and it surfaced and fixed two real, previously-undetected,
deployment-**blocking** defects that would have made the real Render build
fail identically.

---

## 1. Deployment execution

**Docker daemon**: not running at task start (`docker info` could not reach
`unix:///Users/a/.docker/run/docker.sock`). Started it locally
(`open -a Docker`, a local, reversible, no-external-impact action) and
waited for it to come up — ready within 10 seconds.

**First build attempt** (`docker build --build-arg VITE_STAGING=true .`)
**failed outright**:

```
src/components/ui/Icon.tsx(6,8): error TS2307: Cannot find module '@phosphor-icons/react'
```

Root-caused: `@phosphor-icons/react` — the entire icon system, imported by
`src/components/ui/Icon.tsx` and used throughout the consumer app — was
present in the local, long-lived `node_modules` (installed out-of-band at
some earlier point, never via `npm install --save`) but **was never
declared in `frontend/package.json` or `frontend/package-lock.json`**,
confirmed committed and clean at `git log -- frontend/src/components/ui/Icon.tsx`
(introduced in `07d1b1e`, long before this slice). A clean `npm ci` — what
both Docker and `.github/workflows/ci.yml` actually run — never had it. This
is a genuine, pre-existing, committed defect, not something introduced by
this slice or the prior ops-readiness slice.

**Fix**: added `"@phosphor-icons/react": "^2.1.10"` to `dependencies`
(matching the version already resolved in local `node_modules`) and ran
`npm install` to regenerate `package-lock.json` correctly.

**Second build attempt** then failed on a **second instance of the same
root cause**:

```
Error: [postcss] ENOENT: no such file or directory, open '@fontsource/manrope/latin-400.css'
```

`src/design/base.css` imports four `@fontsource/manrope/latin-*.css` files
— the application's whole font — and this package was **not present even
in local `node_modules`** (it had been silently relying on something that
no longer existed; `npm install`'s normal pruning of undeclared packages
when adding the first fix likely removed whatever stale copy was there). A
full sweep of every bare (non-relative) import across `frontend/src`
(`grep -rhoE "from [\"'][^./]...` for JS/TS, and the equivalent for CSS
`@import`) found exactly these two undeclared packages and no others — the
sweep was exhaustive, not sampled.

**Fix**: added `"@fontsource/manrope": "^5.3.0"` (current published version)
to `dependencies`, `npm install` again.

**Third build attempt**: succeeded completely, clean, no errors —
`docker build -t detoura:staging-verify --build-arg VITE_STAGING=true .`
exits 0, image created.

**This is the single most important finding in this report**: without this
fix, *any* real deployment attempt — Render, a fresh `docker build`
anywhere, or CI's own `image` job on a clean runner — would have failed at
the exact same step. This had nothing to do with staging/production
configuration; it would have broken every environment identically. It is
now fixed, verified by an actual successful image build, and additionally
verified by `npm run lint` and `npm run build` (both green, only
pre-existing unrelated lint warnings) run directly outside Docker as well.

**Staging Deployment: VERIFIED locally (image builds and runs correctly);
PARTIAL for an actual Render deployment — see §13.**

---

## 2. Real container verification

Ran: `docker run -d --name detoura-staging-local -p 18000:8000 -v
detoura-staging-local-data:/app/data -e DETOURA_OPS_TOKEN=<redacted>
detoura:staging-verify`. No `DETOURA_ENV` set (correct staging value per the
runbook — unset, not `production`). Container came up immediately.

**Staging URL**: `http://localhost:18000` (local port-forward to the real
running container — there is no public staging hostname yet; see §13).

### HTTPS

**NOT APPLICABLE locally** — this is a bare container on plain HTTP by
design (matching the runbook's documented staging posture: TLS termination
is the hosting platform's job, Render's edge in particular). Verified the
*logic* this depends on is correct: `AUTH_TRUSTED_PROXY_HOPS` and
`DETOURA_ENV` are both unset here, exactly as the runbook specifies for a
non-Render-fronted local staging check.

### Proxy

`AUTH_TRUSTED_PROXY_HOPS=0` (unset/default) in this run — correct for a
deployment with no reverse proxy in front of it, which is what `curl` to
`localhost:18000` actually is. **OPS GATE** for a real Render staging
deployment specifically: `AUTH_TRUSTED_PROXY_HOPS=1` must be set there
(documented in the runbook, not exercised here since there is no Render
edge in this local check).

### Health / Readiness

```
GET /api/v1/health → 200 {"status":"ok","product":"Detoura","data_source":"synthetic"}
GET /readyz        → 200 {"status":"ready"}
```

**VERIFIED**

### Frontend / SPA

```
GET / → 200, contains <div id="root">
GET /api/v1/nope → 404 (JSON, not the SPA shell)
GET /privacy → 200 (serves the SPA shell; the draft/legal-gate state renders client-side — see §5)
```

**VERIFIED**

### Cookies

Registered a real test account (`staging-verify@example.com`) and logged in
against the running container:

```
set-cookie: detoura_session=...; HttpOnly; Max-Age=1209600; Path=/; SameSite=lax
set-cookie: detoura_csrf=...; Max-Age=1209600; Path=/; SameSite=lax
```

No `Secure` flag — **correct** for this plain-HTTP local run (`DETOURA_ENV`
unset). The gating logic itself is unchanged and was already verified by
the focused test suite in the prior slice
(`test_production_deployment_is_not_noindexed` et al. exercise the same
`auth_config().is_production` flag). A real Render staging check should
re-run this exact `curl -isS .../auth/login` against the live HTTPS URL
once one exists, to confirm `Secure` stays absent there too (staging should
never set `DETOURA_ENV=production`).

**VERIFIED** (logic + local evidence); **OPS GATE** (re-verify once a real
HTTPS staging URL exists — cannot be done without one)

### CSRF

```
POST /api/v1/auth/logout, cookie present, no X-CSRF-Token header → 403
POST /api/v1/auth/logout, cookie present, correct X-CSRF-Token header → 200
```

Real double-submit enforcement confirmed against the live container, not
just unit-tested.

**VERIFIED**

### Restart / persistence

Logged in with the test account, restarted the container
(`docker restart`, simulating a redeploy), then logged in again with the
**same** credentials: `200 OK`. The account row survived the restart via
the mounted named volume (`detoura-staging-local-data:/app/data`).

**VERIFIED** — real evidence, not an assumption from the `VOLUME` directive
alone.

### Single-replica constraint

One container, `WEB_CONCURRENCY=1` (Dockerfile default, not overridden).
No second replica was started — correctly matches the documented supported
topology (`docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md` §11).

**NOT APPLICABLE** (constraint respected, not tested against violation —
deliberately not scaled, per instruction)

### Backup / restore

The `python:3.11-slim` runtime image has **no `sqlite3` CLI binary**
(`docker exec ... sqlite3 ...` → `exec: "sqlite3": executable file not
found in $PATH` — a real, demonstrated gap in the runbook as originally
written, now fixed there to use Python's built-in `sqlite3` module
instead, which performs the identical WAL-safe online backup):

```python
import sqlite3
src = sqlite3.connect('/app/data/detoura.db')
dst = sqlite3.connect('/app/data/staging-backup.db')
src.backup(dst)
```

Backup artifact created (643,072 bytes), confirmed to contain the test
account (`staging-verify@example.com`, `ACTIVE`), and confirmed to hold
only an Argon2id hash (`$argon2...`) — not the plaintext password — for
that account. No full restore-into-a-separate-instance was performed (would
need a second container + volume, judged unnecessary to additionally prove
the backup mechanism already demonstrated as consistent and non-sensitive).

**VERIFIED** (backup creation, consistency, no-secrets); **OPS GATE**
(full restore-into-throwaway-instance drill, and the runbook's own
production checklist item — not performed here to avoid unnecessary
duplicate infrastructure for this check)

### Logging

`docker logs detoura-staging-local`, grepped for the test account's
password, session token value, and CSRF token value: **no matches**. JSON
structured logs only, as designed.

**VERIFIED** (no secret leakage); **EXTERNAL OPS GATE** (host-level 14-day
retention — cannot be configured or verified without a real hosting
platform; unchanged from the prior slice's classification)

### Metrics exposure

`DETOURA_METRICS_ENABLED` unset (default) →
```
GET /metrics → 200, Content-Type: text/html
```

**Correction to the prior audit's implicit assumption**: an unauthenticated
prober does **not** see a 404 when metrics are disabled — the real
Prometheus route is simply never registered, so `/metrics` falls through to
the SPA catch-all and serves the ordinary `index.html` shell (200, HTML).
This is the same, already-documented soft-fallback behavior every unknown
path gets (`static.py`'s catch-all) — **no metrics data is exposed**, and
this is not a new defect, but anyone verifying "metrics are off" by
expecting a 404 would be surprised. Recorded here as real, demonstrated
behavior rather than assumption.

**VERIFIED** (no data exposure); classification unchanged — **OPS GATE** if
ever enabled, must be network-restricted.

### Ops exposure

`DETOURA_OPS_TOKEN` set to a local-only test value →
`GET /api/v1/ops/promos` (no `Authorization` header) → `401` (sign-in
required), not `503` (which is what an *unset* token produces) and not an
open door. Confirms the console is reachable-but-gated when a token is
configured, exactly as designed.

**VERIFIED**

### Analytics / attribution / marketing

Served bundle's `index.html` + inline script content: no
`VITE_ANALYTICS_FIRST_PARTY` string present anywhere (this build set
nothing). Confirms the default build ships with analytics structurally
absent, not merely "off by a flag that could be flipped at runtime."

**VERIFIED**

### Security headers

Real response headers from the running container:

```
HTTP/1.1 200 OK
cache-control: no-cache, no-store, must-revalidate
content-type: text/html; charset=utf-8
vary: Origin
x-request-id: <uuid>
x-robots-tag: noindex, nofollow
```

No `Strict-Transport-Security`, `Content-Security-Policy`,
`X-Content-Type-Options`, `X-Frame-Options`/`frame-ancestors`,
`Referrer-Policy`, or `Permissions-Policy` — confirmed absent against a
real response, not just by source grep. Per the prior audit and this
task's own instruction, **no blind CSP was added**: Google Sign-In's
redirect-based OAuth flow and the Wikimedia-hosted destination imagery both
need to be inspected for their actual required origins before any CSP is
written, and a wrong policy silently breaking login is worse than no
policy. This remains explicitly unresolved.

**OPS GATE** — unchanged from the prior audit, now confirmed against a real
deployed response rather than source inspection alone.

### Staging noindex

```
X-Robots-Tag: noindex, nofollow          (real response header)
GET /robots.txt → "User-agent: *\nDisallow: /\n..."
```

Both present on the real running container, built with
`--build-arg VITE_STAGING=true`. This is the fix from the prior slice,
now confirmed end-to-end against an actual artifact rather than a `npm run
build` dry run alone.

**VERIFIED**

### Legal-draft behavior / production gate

`GET /privacy` returns the SPA shell (200); the privacy screen itself
renders client-side and was already verified in the prior privacy-UI slice
to show the non-production draft state in this exact configuration
(`legalConfig.ts` fields all empty, `COMPANION_TRAVELLER_LEGAL_NOTICE_APPROVED:
false` — unchanged, confirmed still the case by reading the file again
before this deployment). `scripts/verify_production_release.sh` (re-run
during this task) still exits 1 against this configuration, confirming the
production legal gate remains correctly fail-closed on the exact artifact
that was just built and deployed.

**LEGAL GATE** (correctly fail-closed, as required)

---

## 3. Real staging smoke flow

| Step | Result |
| --- | --- |
| Landing (`GET /`) | 200, SPA shell served |
| Search (`POST /api/v1/search`, synthetic QUICK mode) | 200, real JSON response with `request_id`, `origin_airports`, `supply_source: "SYNTHETIC"` |
| Account register | 200 |
| Account login | 200, real cookies issued |
| CSRF-protected logout, no token | 403 (correctly rejected) |
| CSRF-protected logout, correct token | 200 |
| Session after logout (`/api/v1/auth/me`) | `null` (correctly invalidated) |
| My Trips after logout | 401 (correctly rejected) |
| Restart, then login again with same account | 200 (persistence confirmed) |

Stripe/Duffel TEST credentials were **not** present in this environment
(same as the prior ops-readiness slice — none were injected), so the
booking-intent → payment → confirm leg of the flow was not exercised with
real provider calls, per instruction to prefer the minimum safe smoke path
when a complete provider booking isn't necessary to establish deployment
correctness. The communication-provider and DB-connectivity checks inside
`/readyz` passed throughout, confirming the sandbox-default configuration
resolves cleanly.

**Real Staging Smoke Flow: VERIFIED** (core path); **BLOCKED**
(Stripe/Duffel TEST leg — no credentials in this environment, not a code
defect)

---

## 4. Providers

| Provider | Status this task |
| --- | --- |
| Stripe | Sandbox default confirmed working via `/readyz`; no TEST credentials present in this environment to exercise the real adapter — unchanged from prior slice |
| Duffel | Same — sandbox/synthetic search confirmed working; no TEST token present |
| Google | **DEFERRED TO NEXT E2E SLICE**, per instruction |
| Resend | **DEFERRED TO NEXT E2E SLICE**, per instruction |

No LIVE provider objects were created. No provider guard was weakened or
bypassed to make staging work.

---

## 5. Technical defects found / fixed

| # | Defect | Severity | Fixed |
| --- | --- | --- | --- |
| 1 | `@phosphor-icons/react` used throughout the app, never declared in `package.json`/`package-lock.json` — any clean build (`npm ci`, Docker, CI) fails | **Critical** (blocks all deployment) | Yes — `frontend/package.json`, `frontend/package-lock.json` |
| 2 | `@fontsource/manrope` (the application's font) used in `src/design/base.css`, never declared anywhere — same failure mode | **Critical** (blocks all deployment) | Yes — same files |
| 3 | `docs/V9_STAGING_RUNBOOK.md`'s backup command used a `sqlite3` CLI binary that does not exist in the runtime image | Medium (runbook would fail when followed literally) | Yes — runbook corrected to use Python's `sqlite3` module |

**Critical: 2 (both fixed). High: 0. Medium: 1 (fixed).**

Both Critical defects were pre-existing and committed before this task
began (traced to commit `07d1b1e`, well before the ops-readiness slice) —
they were masked in every prior session only because a local,
never-committed `node_modules` happened to have the first package
installed out-of-band. Neither is caused by, nor was reachable from, the
Docker/`render.yaml`/`app.py` changes made in the prior ops-readiness
slice — confirmed by the error occurring at the TypeScript/CSS compilation
step, entirely before any of this slice's runtime code executes.

---

## 6. Independent review (this task)

| Check | Finding |
| --- | --- |
| Staging accidentally using LIVE providers | Not found — no live-shaped credentials present anywhere in this run |
| Secrets committed/logged | Not found — grepped real container logs for the test account's password/session/CSRF values, none present |
| Insecure cookies | Not found — `Secure` correctly absent on this non-production run; gating logic unchanged and previously verified |
| Untrusted proxy headers | Not found — default `AUTH_TRUSTED_PROXY_HOPS=0` in effect, correct for this topology |
| Public metrics | Not found — confirmed real response is the SPA shell, not Prometheus data, when disabled (see §2) |
| Indexable staging | Not found — `X-Robots-Tag` and `robots.txt` both confirmed `noindex`/`Disallow: /` on the real artifact |
| Analytics/attribution/marketing enabled | Not found |
| Legal-gate bypass | Not found — `verify_production_release.sh` re-confirmed fail-closed against the exact deployed artifact |
| Ephemeral SQLite | Not found in this check — real restart test proved persistence via the named volume |
| Unsafe multi-replica assumptions | Not exercised (deliberately not scaled); unchanged finding from prior slice stands |
| Missing backup/restore contract | Backup creation newly verified for real; full restore drill remains an OPS GATE |
| Restart losing booking/payment truth | Not exercised this task (no booking was created); unchanged from prior slice's test-backed finding |
| Fake log-retention enforcement | Not found |
| Provider-mode ambiguity | Not found |
| Release-path validation bypass | Not found — legal gate re-confirmed; **found and fixed** the two build-breaking dependency defects, which is a stronger form of "release-path bypass" than anything in the original checklist (a broken build can't bypass a gate it never reaches, but it also means **nothing** would have deployed at all) |
| Unexplained test-order pollution | Not re-investigated this task — no Python backend code changed; prior slice's full-regression evidence stands |

---

## 7. Remaining gates

**OPS GATE** (repository-side work complete; needs a real host/credentials):
- Actual Render service creation/connection and deploy (§0, §13 — the one
  blocking external action)
- Real HTTPS staging URL to re-verify `Secure` cookies, HSTS, and proxy
  trust against an actual TLS-terminating edge
- Full restore-into-throwaway-instance drill
- Host-level 14-day log retention configuration
- Security headers (CSP/HSTS/etc.) — needs a dedicated follow-up slice,
  informed by real Google OAuth + Wikimedia origin inspection
- Google Sign-In and Resend real E2E — explicitly deferred to the next
  slice, per instruction

**LEGAL GATE** (unchanged, correctly fail-closed):
- `legalConfig.ts` metadata + companion-traveller approval — not touched,
  not approved, as instructed

---

## 8. Verification performed this task

- `npm run lint` — pass (only pre-existing, unrelated warnings)
- `npm run build` (default, no staging flag) — pass, output unchanged from
  before this task's dependency fix (same bundle hashes)
- `docker build --build-arg VITE_STAGING=true .` — pass (after the two
  dependency fixes; failed twice before them, with distinct root causes
  each time)
- Full live-container verification — §2/§3 above
- `scripts/verify_production_release.sh` — re-confirmed fails closed
- Backend: **not re-run** — no Python application code was touched in this
  task (only `frontend/package.json`, `frontend/package-lock.json`, and
  `docs/`); the prior slice's full-regression evidence
  (`docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md` §19) stands
  unchanged and was not duplicated, per instruction not to run the giant
  suite again without a material reason

All local Docker verification artifacts (container, volume, image,
cookie jar) were removed after verification — nothing left running.
