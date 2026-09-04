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

## Configuration

| Variable | Where | Default | Meaning |
| --- | --- | --- | --- |
| `VITE_API_BASE` | client, build time | `/api/v1` | Where the client sends requests. Unset *or empty* means same-origin. |
| `DETOURA_CORS_ORIGINS` | API, runtime | the two localhost dev origins | Comma-separated origins allowed to call the API. |
| `DETOURA_FRONTEND_DIST` | API, runtime | `frontend/dist` | Where the built client is. The image sets it to `/app/web`. |
| `PORT` | API, runtime | `8000` | Injected by most hosts. |

When there is no build at `DETOURA_FRONTEND_DIST`, the API simply does not
serve a client — which is what `pytest`, `uvicorn --reload` and the Vite dev
server all rely on.

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

Deliberately, so nothing implies more than is built:

- **No database.** Saved trips live in the browser's `localStorage`. Nothing is
  persisted server-side, and there are no accounts.
- **No authentication.** Every endpoint is public.
- **No rate limiting.** Put it at the edge (Cloudflare, the host's own) before
  exposing this publicly.
- **No error tracking or analytics.** See the V6 plan.
- **No booking.** The UI has no path that transacts.
