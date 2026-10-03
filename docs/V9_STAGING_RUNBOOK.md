# V9 — Staging Deployment Runbook

Exact steps to deploy a non-production, non-indexable Detoura environment for
engineering verification. Companion to
`docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md` and
`docs/V9_PRODUCTION_RELEASE_CHECKLIST.md` (production only — do not use that
checklist for staging). See `DEPLOY.md` for the full environment-variable
reference this runbook draws from.

All commands below are real repository commands, verified against this
codebase during the ops-readiness audit — none are illustrative.

## 1. Prerequisites

- Docker (the single-origin image is the supported staging topology — see
  §11/§13 of the readiness report for why this must stay single-process).
- No database migration step: SQLite auto-initializes and seeds example
  markup/promo data on first run (`persistence.bootstrap`).
- No `.env` file is loaded automatically — every variable below must be
  injected explicitly (host dashboard, `docker run -e`, `--env-file`).

## 2. Environment variables (staging values)

| Variable | Staging value | Why |
| --- | --- | --- |
| `DETOURA_ENV` | *(leave unset)* | Unset = `is_production` is `False` = Secure-cookie flag off (fine for a staging URL that may not be HTTPS) and `X-Robots-Tag: noindex, nofollow` applied automatically. Setting this to `production` on a staging host would defeat the noindex protection in §5. |
| `DETOURA_CORS_ORIGINS` | unset (single-origin) or the staging client's origin if split-hosting | Only needed for split hosting. |
| `DETOURA_DB_PATH` | `/app/data/detoura.db` (Dockerfile default) | Ensure the volume below is mounted, or state does not survive a restart. |
| `DETOURA_OPS_TOKEN` | a staging-only secret, **never the production value** | Unset disables Ops entirely (also acceptable for staging). |
| `PAYMENT_PROVIDER` | `sandbox` (default) or `stripe` with a **`sk_test_...`** key | Never a live-looking key — the adapter refuses it anyway, but do not rely on that as your only control. |
| `PAYMENT_LIVE_CHARGING_ENABLED` | leave unset/`false` | Must never be `true` in staging. |
| `DUFFEL_ACCESS_TOKEN` / `SEARCH_LIVE_ENABLED` | a **`duffel_test_...`** token, or leave unset | Live Duffel tokens are refused by the adapter regardless. |
| `COMMUNICATION_PROVIDER` / `COMMUNICATION_LIVE_SENDING_ENABLED` | leave unset/`sandbox`/`false` | Never send real email from staging. |
| `GOOGLE_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | staging-registered OAuth client only, or leave unset | A production Google OAuth client's redirect URI will not match a staging host anyway. |
| `DETOURA_METRICS_ENABLED` | leave unset unless you have network-level access control in front of this deployment | `/metrics` has no auth of its own. |
| `AUTH_TRUSTED_PROXY_HOPS` | `1` if staging also sits behind a single reverse proxy (e.g. Render), else `0` | See the production checklist for the reasoning — the same logic applies. |

## 3. Frontend build (staging, non-indexable)

To check the client build alone, outside Docker:

```bash
cd frontend
npm ci
VITE_STAGING=true npm run build       # NOT build:release - see step 4
```

The Docker image (step 5) builds the same client internally — pass
`--build-arg VITE_STAGING=true` to that `docker build` instead of setting the
environment variable directly; the Dockerfile only forwards `VITE_STAGING`
into the client build stage via that build arg (see `Dockerfile`'s "Stage 1"
comments).

Do **not** set `VITE_PUBLIC_SITE_URL`/`SITE_URL` for a staging build even
though `VITE_STAGING=true` already suppresses the sitemap and forces
`noindex,nofollow` — there is no reason for a staging deploy to also claim a
real canonical URL.

Verify the build is actually non-indexable before shipping it:

```bash
grep -o '<meta name="robots"[^>]*>' dist/index.html   # must show noindex,nofollow
cat dist/robots.txt                                    # must show "Disallow: /"
```

## 4. Do not run the production legal gate for staging

`npm run build:release` (which runs `scripts/verify-legal-readiness.mjs` /
`frontend/scripts/verify-legal-readiness.mjs`) is expected to **fail** right
now, because `frontend/src/privacy/legalConfig.ts` is intentionally still
draft/empty pending real legal review — that failure is correct, not a bug in
your staging setup. Use plain `npm run build` for staging, exactly as CI and
the Dockerfile already do. `scripts/verify_production_release.sh` is a
**production-only** gate — do not run it as a staging precondition.

## 5. Build and run the image

```bash
docker build -t detoura:staging --build-arg VITE_STAGING=true .
docker run -d --name detoura-staging \
  -p 8000:8000 \
  -v detoura-staging-data:/app/data \
  -e DETOURA_OPS_TOKEN=<staging-only-token> \
  detoura:staging
```

If deploying to Render (or any host) instead of running the container
directly, inject the same environment variables via that host's own
mechanism — `render.yaml`'s checked-in values (`DETOURA_ENV=production`,
`AUTH_TRUSTED_PROXY_HOPS=1`) are for the **production** service block only.
A separate staging service on the same host must not inherit
`DETOURA_ENV=production`.

## 6. Health / readiness / smoke tests

```bash
curl -fsS http://localhost:8000/api/v1/health        # {"status":"ok",...}
curl -fsS http://localhost:8000/readyz                # {"status":"ready"}
curl -fsS http://localhost:8000/ | grep -q '<div id="root">'

# Unknown API path must be a JSON 404, not the SPA shell:
curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/api/v1/nope   # 404

# X-Robots-Tag must be present (DETOURA_ENV is unset in staging):
curl -sI http://localhost:8000/ | grep -i x-robots-tag   # noindex, nofollow
```

These mirror exactly what `.github/workflows/ci.yml`'s `image` job already
asserts, plus the new noindex header check.

## 7. Provider TEST-mode verification

```bash
# Confirms the running config resolves to a usable provider without a network call:
curl -fsS http://localhost:8000/readyz
```

If `PAYMENT_PROVIDER=stripe` is set, confirm the key is TEST-mode
(`sk_test_...`) before starting the container — the adapter itself refuses
anything else, but verify at config time rather than discovering it at first
checkout. Same for `DUFFEL_ACCESS_TOKEN` (`duffel_test_...`).

## 8. Analytics-off verification

```bash
curl -fsS http://localhost:8000/ | grep -o 'VITE_ANALYTICS_FIRST_PARTY[^"]*' || true
```

Confirm no production analytics build flag was baked in. The default staging
build (no `VITE_ANALYTICS_FIRST_PARTY` set) already ships analytics
structurally disabled — this step is a sanity check, not a required action.

## 9. Cookie / security-header verification

```bash
# Register + log in against the running staging container, then:
curl -isS -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"staging-check@example.com","password":"correct horse battery staple"}'
curl -isS -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"staging-check@example.com","password":"correct horse battery staple"}' \
  | grep -i set-cookie
```

Expect `detoura_session=...; HttpOnly; SameSite=lax` (no `Secure` unless this
staging host is itself served over HTTPS with `DETOURA_ENV=production` set —
which staging should not do; see step 2). Delete this test account
afterwards via the account-deletion endpoint if this staging environment is
shared.

## 10. Log-retention configuration

Application logs go to stdout only (`observability/logging.py`) — there is no
in-app retention to configure. Set the **host/platform's** log retention to
14 days maximum for this staging service, matching the Product/Ops policy in
`docs/V9_RETENTION_REGISTER.md` §C, via whatever your chosen host provides
(Render log retention settings, or an external sink's own policy). This is a
platform-console action, not a repository change.

## 11. Backup verification

The `python:3.11-slim` runtime image does **not** include the `sqlite3` CLI
binary (confirmed by real exec attempt — `exec: "sqlite3": executable file
not found in $PATH`), only Python's built-in `sqlite3` module. Use that
module directly, which performs the identical WAL-safe online backup the
CLI's `.backup` command would:

```bash
docker exec detoura-staging python3 -c "
import sqlite3
src = sqlite3.connect('/app/data/detoura.db')
dst = sqlite3.connect('/app/data/staging-backup.db')
src.backup(dst)
dst.close(); src.close()
"
docker cp detoura-staging:/app/data/staging-backup.db ./staging-backup.db
python3 -c "
import sqlite3
c = sqlite3.connect('./staging-backup.db')
print(c.execute('SELECT count(*) FROM user_accounts').fetchone())
"
```

## 12. Legal-draft behavior

Load `http://localhost:8000/privacy` (or the frontend dev server's
equivalent) and confirm it renders the **non-production draft state** — a
neutral unavailable message, not fabricated legal content — per
`docs/V9_LIMITED_BETA_PRIVACY_UI_REPORT.md`. This is expected and correct for
staging; do not "fix" it by filling in placeholder legal values.

## 13. Rollback

Single-image, single-process deployment: rollback is redeploying the
previous image tag (`docker run ... detoura:<previous-tag>`) against the same
mounted volume. No schema migration exists to roll back. If the DB schema
itself needs to change in a future slice, that is a new, separate
migration-strategy decision — out of scope here.

## 14. Smoke tests (full pass)

Repeat step 6 in full, then run one real search + one sandbox booking flow
end-to-end (search → booking-intent → travelers → sandbox payment → confirm)
against the running staging container to confirm the whole path works
end-to-end before calling the environment ready for engineering use.
