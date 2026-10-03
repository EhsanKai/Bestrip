# Detoura - one image, one origin, both halves.
#
# The web client's API base defaults to the relative path `/api/v1`, so an
# image that serves the build and the API together needs no build-time
# knowledge of its own hostname. That is the whole reason this is a single
# image rather than two: nothing here has to be told where it will be deployed.
#
# Build:  docker build -t detoura .
# Run:    docker run -p 8000:8000 detoura
#
# To point the client at an API on a different host instead, build with
# `--build-arg VITE_API_BASE=https://api.example.com/api/v1` and set
# DETOURA_CORS_ORIGINS on the API.

# ---------------------------------------------------------------------------
# Stage 1: build the client
# ---------------------------------------------------------------------------
FROM node:22-slim AS web

WORKDIR /build

# Dependencies first: this layer is cached until the lockfile itself changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./

# Empty means "same origin", which is the default deployment.
ARG VITE_API_BASE=""
ENV VITE_API_BASE=${VITE_API_BASE}

# Empty (the default) ships today's production-indexable output unchanged.
# `--build-arg VITE_STAGING=true` builds a never-indexable image instead - see
# docs/V9_STAGING_RUNBOOK.md. Deliberately NOT wired to any auto-detected
# signal (e.g. a missing VITE_API_BASE): staging must be an explicit choice.
ARG VITE_STAGING=""
ENV VITE_STAGING=${VITE_STAGING}

# `npm run build` is `tsc -b && vite build`, so a type error fails the image
# rather than shipping. Deliberately not `build:release`: the legal-readiness
# gate (frontend/scripts/verify-legal-readiness.mjs) must stay opt-in and
# separate - see scripts/verify_production_release.sh - or this build (and
# every staging/CI build) would fail while legal metadata is still draft.
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: the runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DETOURA_FRONTEND_DIST=/app/web \
    DETOURA_DB_PATH=/app/data/detoura.db \
    DETOURA_DESTINATION_IMAGES_DIR=/app/data/destination_images

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir ".[api,session]"

COPY --from=web /build/dist /app/web

# The destination-image provenance manifest and pre-optimized WebP assets
# (V9 Phase 2.6 Part B) - a build-time artifact like the frontend dist, not
# runtime-generated, so it ships in the image rather than depending on the
# acquisition pipeline (network access, Pillow) ever running in production.
COPY data/destination_images/manifest.json /app/data/destination_images/manifest.json
COPY data/destination_images/assets/ /app/data/destination_images/assets/

# The commercial + ops SQLite database (V8.5). Mount a volume here to keep
# promo codes, markup policy versions, the economics ledger and the audit
# trail across restarts:  docker run -v detoura-data:/app/data ...
VOLUME /app/data

# Nothing here needs root, and an unprivileged runtime is one less thing to
# reason about if the process is ever compromised.
#
# /app/data/db is pre-created (not just /app/data) and chowned here,
# BEFORE anything ever mounts a volume/disk over it: a fresh Docker named
# volume or a fresh Render persistent disk mounted at a path with no
# corresponding directory already in the image comes up owned by root,
# which this non-root process then cannot write into - reproduced directly
# (sqlite3.OperationalError: unable to open database file) with a bare
# `docker run -v vol:/app/data/db`. Pre-creating it here, owned by
# `detoura`, is what a volume/disk first mounted at that exact path
# inherits (standard Docker/OCI behavior: an empty volume mounted over an
# existing image directory is seeded from that directory, permissions
# included). This subdirectory - not the whole /app/data tree - is where
# render.yaml's disk is mounted and DETOURA_DB_PATH points on Render, so
# that disk never shadows the destination_images/ assets baked in above,
# which also live under /app/data.
RUN useradd --create-home --uid 10001 detoura \
    && mkdir -p /app/data/db \
    && chown -R detoura:detoura /app
USER detoura

EXPOSE 8000

# Most hosts (Railway, Render, Fly, Cloud Run) inject $PORT and expect the
# process to honour it; 8000 is the local default.
ENV PORT=8000

# WEB_CONCURRENCY is exported alongside --workers, not merely read by uvicorn:
# the session store uses it to refuse to start when more than one worker is
# configured without a shared store. Left at 1, this is the zero-configuration
# single-worker deployment and needs no Redis.
ENV WEB_CONCURRENCY=1
CMD ["sh", "-c", "exec uvicorn detoura.api.app:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY}"]
