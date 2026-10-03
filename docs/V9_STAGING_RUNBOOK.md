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

## 2. Render persistent disk for SQLite (read before any Render deploy)

Detoura is single-writer SQLite — exactly one `sqlite3.connect` call site
exists in the whole codebase (`src/detoura/persistence/db.py`), and every
table (accounts, bookings, payments, documents, the Ops audit trail) lives
in that one file. `render.yaml` attaches a Render persistent disk to the
`detoura` service specifically so this survives redeploys:

| Fact | Value |
| --- | --- |
| Disk name | `detoura-data` |
| Mount path | `/app/data/db` |
| Disk size | 1 GB (smallest Render allows; resize later if the DB grows) |
| `DETOURA_DB_PATH` on Render | `/app/data/db/detoura.db` (set in `render.yaml`'s `envVars` — **not** the Dockerfile's own default, see below) |
| SQLite WAL/SHM files | Created automatically as `detoura.db-wal`/`detoura.db-shm`, siblings of `detoura.db` — on the same disk, with no separate configuration, by stock SQLite's own behavior |

**Why the disk is mounted at `/app/data/db`, not the whole `/app/data`
directory**: the Dockerfile also bakes `destination_images/manifest.json`
and `assets/` into `/app/data/destination_images` as a *build-time*
artifact. A disk mounted over the entire `/app/data` directory would be
empty on first attach and would shadow those baked-in files, silently
breaking the destination-images feature on the very first deploy. Mounting
at the narrower `/app/data/db` subdirectory avoids that collision entirely
— confirmed by a real local Docker reproduction during this slice (see
`docs/V9_REAL_STAGING_DEPLOYMENT_REPORT.md`).

**Why the Dockerfile pre-creates `/app/data/db` and `chown`s it to the
`detoura` user before anything ever mounts there**: a fresh Docker named
volume (and, by the same first-mount semantics, a fresh Render disk) mounted
at a path with no directory already present in the image comes up owned by
`root`. This container runs as the non-root `detoura` user (uid 10001) for
security — reproduced directly as
`sqlite3.OperationalError: unable to open database file` with the directory
missing from the image, fixed by pre-creating it there so the volume/disk
inherits the right ownership on first mount.

**What this means for local, non-Render Docker use (unchanged)**: a plain
`docker run` with no `DETOURA_DB_PATH` override still uses the Dockerfile's
own default, `/app/data/detoura.db` — a sibling of `destination_images`
directly under `/app/data`, exactly as before this slice. The `/app/data/db`
path above is a **Render-specific** override declared only in `render.yaml`,
not a change to local/Docker/test defaults anywhere else in this repository.

**Single-replica constraint, unchanged and non-negotiable**: Render disks
cannot be shared across multiple service instances. This is not a new
limitation this disk introduces — Detoura's SQLite file locking and its
in-process session/Ops-token stores already require exactly one running
instance (`DEPLOY.md`, `docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md`
§11). Do not scale this service to more than one instance on Render.

**Persistence expectations**: an account, booking, payment or document
written today survives a Render redeploy, a container restart, and a full
container replacement, exactly as long as the `detoura-data` disk itself is
not deleted or detached. **Deleting or replacing the disk destroys
everything on it** — there is no separate backup copy unless one was taken
deliberately (see step 12). The container's own filesystem outside
`/app/data/db` (including `/app/data/destination_images`, which is fine
since it's a rebuildable build-time artifact) is **ephemeral** and must
never be assumed to survive a redeploy.

**Restore remains a manual Ops procedure** — there is no automated restore
tooling in this repository, on Render or otherwise. A restore means
replacing the disk's `detoura.db` (and its `-wal`/`-shm` files, or none if a
clean `.backup` was used) with a known-good backup file while the service is
stopped, then restarting it.

## 3. Environment variables (staging values)

| Variable | Staging value | Why |
| --- | --- | --- |
| `DETOURA_ENV` | *(leave unset)* | Unset = `is_production` is `False` = Secure-cookie flag off (fine for a staging URL that may not be HTTPS) and `X-Robots-Tag: noindex, nofollow` applied automatically. Setting this to `production` on a staging host would defeat the noindex protection in step 6. |
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

## 4. Frontend build (staging, non-indexable)

To check the client build alone, outside Docker:

```bash
cd frontend
npm ci
VITE_STAGING=true npm run build       # NOT build:release - see step 5
```

The Docker image (step 6) builds the same client internally — pass
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

## 5. Do not run the production legal gate for staging

`npm run build:release` (which runs `scripts/verify-legal-readiness.mjs` /
`frontend/scripts/verify-legal-readiness.mjs`) is expected to **fail** right
now, because `frontend/src/privacy/legalConfig.ts` is intentionally still
draft/empty pending real legal review — that failure is correct, not a bug in
your staging setup. Use plain `npm run build` for staging, exactly as CI and
the Dockerfile already do. `scripts/verify_production_release.sh` is a
**production-only** gate — do not run it as a staging precondition.

## 6. Build and run the image

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

## 7. Health / readiness / smoke tests

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

## 8. Provider TEST-mode verification

```bash
# Confirms the running config resolves to a usable provider without a network call:
curl -fsS http://localhost:8000/readyz
```

If `PAYMENT_PROVIDER=stripe` is set, confirm the key is TEST-mode
(`sk_test_...`) before starting the container — the adapter itself refuses
anything else, but verify at config time rather than discovering it at first
checkout. Same for `DUFFEL_ACCESS_TOKEN` (`duffel_test_...`).

## 9. Analytics-off verification

```bash
curl -fsS http://localhost:8000/ | grep -o 'VITE_ANALYTICS_FIRST_PARTY[^"]*' || true
```

Confirm no production analytics build flag was baked in. The default staging
build (no `VITE_ANALYTICS_FIRST_PARTY` set) already ships analytics
structurally disabled — this step is a sanity check, not a required action.

## 10. Cookie / security-header verification

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
which staging should not do; see step 3). Delete this test account
afterwards via the account-deletion endpoint if this staging environment is
shared.

## 11. Log-retention configuration

Application logs go to stdout only (`observability/logging.py`) — there is no
in-app retention to configure. Set the **host/platform's** log retention to
14 days maximum for this staging service, matching the Product/Ops policy in
`docs/V9_RETENTION_REGISTER.md` §C, via whatever your chosen host provides
(Render log retention settings, or an external sink's own policy). This is a
platform-console action, not a repository change.

## 12. Backup verification

The `python:3.11-slim` runtime image does **not** include the `sqlite3` CLI
binary (confirmed by real exec attempt — `exec: "sqlite3": executable file
not found in $PATH`), only Python's built-in `sqlite3` module. Use that
module directly, which performs the identical WAL-safe online backup the
CLI's `.backup` command would.

**Read the actual configured path from the container rather than hardcoding
it** — it differs between a plain `docker run` (`/app/data/detoura.db`,
the Dockerfile's own default) and a Render deployment
(`/app/data/db/detoura.db`, set in `render.yaml`; see step 2). A backup
command that hardcodes the wrong one of these silently backs up a stale or
nonexistent file:

```bash
docker exec detoura-staging python3 -c "
import os, sqlite3
db_path = os.environ.get('DETOURA_DB_PATH') or '/app/data/detoura.db'
src = sqlite3.connect(db_path)
dst = sqlite3.connect('/tmp/staging-backup.db')
src.backup(dst)
dst.close(); src.close()
print('backed up', db_path)
"
docker cp detoura-staging:/tmp/staging-backup.db ./staging-backup.db
python3 -c "
import sqlite3
c = sqlite3.connect('./staging-backup.db')
print(c.execute('SELECT count(*) FROM user_accounts').fetchone())
"
```

The backup destination (`/tmp/staging-backup.db` inside the container) is
deliberately **not** on the persistent disk — it's copied out immediately
via `docker cp` and is never the durable copy itself; the durable artifact
is `./staging-backup.db` on the machine running this command, which should
then be stored wherever your backup retention policy requires.

## 13. Legal-draft behavior

Load `http://localhost:8000/privacy` (or the frontend dev server's
equivalent) and confirm it renders the **non-production draft state** — a
neutral unavailable message, not fabricated legal content — per
`docs/V9_LIMITED_BETA_PRIVACY_UI_REPORT.md`. This is expected and correct for
staging; do not "fix" it by filling in placeholder legal values.

## 14. Rollback

Single-image, single-process deployment: rollback is redeploying the
previous image tag (`docker run ... detoura:<previous-tag>`) against the same
mounted volume. No schema migration exists to roll back. If the DB schema
itself needs to change in a future slice, that is a new, separate
migration-strategy decision — out of scope here.

## 15. Smoke tests (full pass)

Repeat step 7 in full, then run one real search + one sandbox booking flow
end-to-end (search → booking-intent → travelers → sandbox payment → confirm)
against the running staging container to confirm the whole path works
end-to-end before calling the environment ready for engineering use.
