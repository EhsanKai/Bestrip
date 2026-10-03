# Deploying Detoura

There are two arrangements. The first is the one this repository is set up for
and the one you should pick unless you have a reason not to.

> **All prices, schedules and availability in this build are synthetic.** The
> engine is real; the data behind it is fabricated. Do not deploy this as a
> booking service.

---

## 1. One origin (recommended)

The API process serves the built web client. One image, one URL, one thing to
deploy.

This works without configuration because the client's API base defaults to the
relative path `/api/v1`. It never needs to be told the hostname it is running
on, so the same image runs unchanged on your laptop, in staging and in
production.

It also means **CORS never applies**: the client's requests are same-origin, so
there is no cross-origin preflight to get wrong.

```bash
docker build -t detoura .
docker run -p 8000:8000 detoura
# http://localhost:8000
```

### Hosts

| Host | What it needs |
| --- | --- |
| **Railway** | Nothing. It detects the `Dockerfile` and injects `$PORT`, which the image honours. |
| **Render** | `render.yaml` is in the repository root. Point Render at the repo. |
| **Fly.io** | `fly launch --dockerfile Dockerfile`. |
| **Cloud Run** | `gcloud run deploy --source .`. `$PORT` is honoured. |

Set the health check to `/api/v1/health` if the host does not read
`render.yaml`.

---

## 2. Split hosting

The client on a static CDN, the API somewhere else. Choose this if you want the
client on a global edge network, and accept two deploys and a CORS
configuration in exchange.

Both sides need to know about each other, and **both settings are required** —
setting one without the other produces a client that fails every request.

**Client** (build-time — Vite inlines it, so changing it means rebuilding):

```
VITE_API_BASE=https://api.detoura.app/api/v1
```

Include the `/api/v1` suffix: this value is the base the client appends paths
to, not just the API's hostname. Setting it to a blank value is treated as
same-origin rather than as a real base — a saved-but-empty field in a hosting
dashboard would otherwise compile every request down to `/search` and fail only
at search time, long after the health check has gone green.

**API** (runtime):

```
DETOURA_CORS_ORIGINS=https://detoura.app,https://www.detoura.app
```

Setting `DETOURA_CORS_ORIGINS` *replaces* the localhost defaults rather than
adding to them — a deployment that names its origins should not keep trusting a
dev server. There is no wildcard.

`frontend/vercel.json` and `frontend/netlify.toml` are both ready: set the
project's base directory to `frontend` and add `VITE_API_BASE` to its
environment.

---

## Running more than one worker

Search is CPU-bound pure Python, so concurrent searches serialize on the GIL
and only separate worker *processes* run them in parallel. Measured, 20-way
concurrent SMART:

| workers | p50 | p95 | throughput | memory |
| --- | --- | --- | --- | --- |
| 1 | 10.32s | 10.45s | 1.9 req/s | 74 MB |
| 2 | 6.24s | 6.85s | 2.9 req/s | 151 MB |
| 4 | **3.83s** | **4.38s** | **4.6 req/s** | 243 MB |

**More than one worker requires a shared session store.** Personalization
state is per-process, so multiple workers without one hold disagreeing copies
of the same traveller's profile - twelve signals to one session returned
`[1,2,3,4,5,1,2,6,3,4,7,8]`. This is silent: nothing errors, the numbers are
just wrong.

```bash
DETOURA_SESSION_STORE=redis
DETOURA_REDIS_URL=redis://your-redis:6379/0
```

Install the extra in the image (`pip install ".[api,session]"` - already in the
Dockerfile) and run with `--workers N`. With the shared store configured, the
same twelve signals return `[1..12]` at 1, 2 and 4 workers.

A single worker needs none of this and remains the default: the store falls
back to an in-memory implementation, which is correct for one process.

---

## Configuration

Deployment/runtime (non-secret unless noted):

| Variable | Where | Default | Meaning |
| --- | --- | --- | --- |
| `VITE_API_BASE` | client, build time | `/api/v1` | Where the client sends requests. Unset *or empty* means same-origin. |
| `VITE_PUBLIC_SITE_URL` / `SITE_URL` | client, build time | unset | Real public production hostname. Only when set does the build emit a real `sitemap.xml`, canonical URL and JSON-LD. Leave unset for staging/preview builds. |
| `VITE_STAGING` | client, build time | `false` | Set to `true` to build a never-indexable client: `noindex,nofollow` meta tag, blanket `Disallow: /` robots.txt, no sitemap — regardless of `VITE_PUBLIC_SITE_URL`. See docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md. |
| `DETOURA_CORS_ORIGINS` | API, runtime | the two localhost dev origins | Comma-separated origins allowed to call the API. Only relevant for split hosting — same-origin deploys never hit CORS. |
| `DETOURA_FRONTEND_DIST` | API, runtime | `frontend/dist` | Where the built client is. The image sets it to `/app/web`. |
| `DETOURA_DB_PATH` | API, runtime | `<cwd>/detoura.db` | SQLite file path. The image sets it to `/app/data/detoura.db` (inside the declared `VOLUME`) for plain `docker run`. **On Render specifically**, `render.yaml` overrides this to `/app/data/db/detoura.db` — a dedicated subdirectory, not the whole `/app/data` tree, so the attached persistent disk never shadows the `destination_images` assets also baked into `/app/data`. See `docs/V9_STAGING_RUNBOOK.md` §2 for the full rationale. |
| `DETOURA_DESTINATION_IMAGES_DIR` | API, runtime | unset (feature no-ops) | Where the destination-image manifest/assets live. The image bakes these in at `/app/data/destination_images`. |
| `DETOURA_SESSION_STORE` | API, runtime | `memory` | `memory` or `redis`. **Required to be `redis` when running more than one worker.** |
| `DETOURA_REDIS_URL` | API, runtime | `redis://localhost:6379/0` | Used only when the store is `redis`. |
| `DETOURA_SESSION_TTL_SECONDS` | API, runtime | 14 days | How long an idle session survives. |
| `DETOURA_ENV` | API, runtime | unset (non-production) | Set to `production` on any real production deployment. Gates `Secure` on session/CSRF cookies (`auth.py`) and HSTS (`app.py`) — **omitting this in production is an insecure-cookie defect**, not a neutral default. No longer the sole gate on the `X-Robots-Tag: noindex` header — see `DETOURA_FORCE_NOINDEX` below. |
| `DETOURA_FORCE_NOINDEX` | API, runtime | unset (`false`) | Set to `true` to force `X-Robots-Tag: noindex, nofollow` even when `DETOURA_ENV=production` (and Secure cookies) is also set — for a deployment, like a Render staging service, that needs production-grade cookie security but must not be indexed. Pair with `VITE_STAGING=true` below so the frontend's own meta tag/robots.txt/sitemap agree. Leaving it unset preserves today's behavior: a real `DETOURA_ENV=production` deployment stays indexable. |
| `AUTH_TRUSTED_PROXY_HOPS` | API, runtime | `0` (trust nothing) | Number of reverse-proxy hops in front of this service whose `X-Forwarded-For` entry to trust for rate limiting. Render's edge is one hop — set to `1` there. A wrong, too-high value is a spoofing hole; see `services/client_ip.py`. |
| `DETOURA_METRICS_ENABLED` | API, runtime | unset (off) | Exposes `GET /metrics` (Prometheus text) with **no authentication of its own**. Keep off any publicly-reachable network path; if enabling it, restrict access at the network/proxy layer. |
| `DETOURA_LOG_LEVEL` | API, runtime | `INFO` | Root logger level for the JSON logs written to stdout. |
| `DETOURA_MAX_REQUEST_BODY_BYTES` | API, runtime | see `body_limit.py` | Request bodies larger than this are rejected before being buffered. |
| `DETOURA_OPS_TOKEN` | API, runtime, **secret** | unset (Ops console disabled) | Shared bearer secret gating every `/api/v1/ops/*` route. Unset = the entire Ops console 503s — fail-closed by design. Ops sessions are held **in-process memory only**; they do not survive a restart and are not shared across replicas/workers. |
| `PORT` | API, runtime | `8000` | Injected by most hosts. |

Provider configuration (all fail closed to a safe sandbox/disabled state if
unset or malformed — see `docs/V9_THIRD_PARTY_PRIVACY_PROVIDER_REGISTER.md`):

| Variable | Purpose | Secret? | Fail-closed behavior |
| --- | --- | --- | --- |
| `PAYMENT_PROVIDER` | `sandbox` (default) or `stripe` | no | Anything else raises; no silent default. |
| `PAYMENT_LIVE_CHARGING_ENABLED` | master kill switch, default `false` | no | `false` forces the sandbox adapter regardless of `PAYMENT_PROVIDER`. |
| `STRIPE_SECRET_KEY` | Stripe API key | **yes** | A non-`sk_test_`-shaped key is refused outright — this codebase cannot create a live Stripe charge no matter how it is configured until a future slice explicitly adds that. |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signature verification | **yes** | Required only if the webhook route is exposed. |
| `SEARCH_LIVE_ENABLED` / `DUFFEL_ACCESS_TOKEN` | live flight search | `DUFFEL_ACCESS_TOKEN` is **secret** | A token not prefixed `duffel_test_` is refused before any request is built — Duffel LIVE order issuance does not exist in this codebase today, by design. |
| `COMMUNICATION_PROVIDER` | `sandbox` (default) or `resend` | no | Anything else raises; no silent default. |
| `COMMUNICATION_LIVE_SENDING_ENABLED` | master kill switch, default `false` | no | `false` forces the sandbox adapter regardless of `COMMUNICATION_PROVIDER`. |
| `RESEND_API_KEY` | Resend API key | **yes** | Missing/malformed with live sending enabled raises loudly rather than silently using the sandbox. |
| `RESEND_FROM_EMAIL` | verified sender address | no | Required alongside `RESEND_API_KEY`. |
| `RESEND_FROM_NAME` / `RESEND_REPLY_TO` | display name / reply-to | no | Optional. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_REDIRECT_URI` / `GOOGLE_POST_LOGIN_REDIRECT_URL` | Google Sign-In | no | Google Sign-In is fully disabled (503) unless client id, redirect URI, *and* the secret below are all set. |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret | **yes** | Read fresh from the environment on every use; never cached on a config object. |

When there is no build at `DETOURA_FRONTEND_DIST`, the API simply does not
serve a client — which is what `pytest`, `uvicorn --reload` and the Vite dev
server all rely on.

None of these are loaded from a `.env` file automatically — there is no
`python-dotenv` (or equivalent) anywhere in this codebase. A deployment must
inject every variable it needs explicitly (host dashboard, `docker run -e`,
`--env-file`, etc.); a local `.env` only helps a shell you source it into
yourself.

---

## Local development

Two processes, because the Vite dev server does hot reload and the proxy in
`vite.config.ts` sends `/api` to the backend:

```bash
pip install -e ".[dev]"
uvicorn detoura.api.app:app --reload          # :8000

cd frontend && npm install && npm run dev      # :5173
```

Open <http://localhost:5173>. The dev-server defaults in `DETOURA_CORS_ORIGINS`
exist for exactly this.

To check the production arrangement locally, build the client and run only the
API:

```bash
cd frontend && npm run build && cd ..
uvicorn detoura.api.app:app          # :8000 now serves both
```

---

## Before the first deploy

- [ ] `python -m pytest` — the full suite
- [ ] `cd frontend && npm run lint && npm run build`
- [ ] `docker build -t detoura . && docker run -p 8000:8000 detoura`, then load
      `/` and `/api/v1/health`
- [ ] Decide arrangement 1 or 2. For 2, set **both** `VITE_API_BASE` and
      `DETOURA_CORS_ORIGINS`
- [ ] Point the host's health check at `/api/v1/health`
- [ ] If the deployment is public, note that the data is synthetic

This list is what CI already exercises on every push — it makes a build
*staging-safe*, not production-ready. A real production release additionally
needs `docs/V9_PRODUCTION_RELEASE_CHECKLIST.md` and
`scripts/verify_production_release.sh` (the frontend legal gate, not run by
CI or the Dockerfile — see that script's own comments for why).

CI (`.github/workflows/ci.yml`) runs the first three on every push, including
building the image and asserting that the running container serves the shell at
`/` and a JSON 404 — not the shell — at an unknown `/api/v1` path.

---

## Caching

The API and both static-host configs apply the same policy, because getting it
wrong is a class of bug that only appears on the *second* deploy:

- `/assets/*` — `max-age=31536000, immutable`. Vite puts a content hash in
  every filename, so these can never go stale.
- `index.html` — `no-store`. It names the fingerprinted assets, so caching it
  pins browsers to the previous deploy's bundle.

---

## What is not here

This section described an earlier (V6-era) build and had drifted out of date
with the current `src/` tree — corrected as part of the V9 staging/production
ops readiness slice (see docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md).
Current state:

- **There is a database.** A single SQLite file (`DETOURA_DB_PATH`, WAL mode,
  one connection, every access serialized) holds accounts, sessions, bookings,
  payments and the Ops audit trail. It is **not** an in-memory/throwaway store
  — see the Database/Persistence section of the ops readiness report for the
  volume, backup and multi-replica implications.
- **There is authentication.** Email/password accounts (Argon2id hashing) and
  Google Sign-In, both with server-side sessions in an `HttpOnly` cookie and
  double-submit CSRF protection on mutating routes. See
  `src/detoura/api/auth.py`, `auth_google.py`.
- **There is rate limiting**, in-process, on login/register/password-reset and
  the Ops shared-token exchange (`services/rate_limit.py`). It is per-process
  state, not shared across workers/replicas — still worth an edge-level limiter
  for defense in depth, but this is not the same as having none.
- **There is real payment and booking issuance**, gated fail-closed to
  sandbox/TEST-mode providers unless explicitly and correctly configured for
  Stripe TEST and Duffel TEST (see the provider configuration table above).
  Real Stripe TEST + Duffel TEST end-to-end issuance has been verified; **no
  live-provider path exists for either** as currently configured.
- **No error tracking or analytics beyond a first-party, consent-gated,
  currently-disabled seam.** See
  `docs/V9_LIMITED_BETA_ANALYTICS_FOUNDATION_REPORT.md` — analytics,
  attribution and marketing tracking are all OFF for Limited Beta.
