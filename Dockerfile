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

# `npm run build` is `tsc -b && vite build`, so a type error fails the image
# rather than shipping.
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: the runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DETOURA_FRONTEND_DIST=/app/web

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir ".[api]"

COPY --from=web /build/dist /app/web

# Nothing here needs root, and an unprivileged runtime is one less thing to
# reason about if the process is ever compromised.
RUN useradd --create-home --uid 10001 detoura \
    && chown -R detoura:detoura /app
USER detoura

EXPOSE 8000

# Most hosts (Railway, Render, Fly, Cloud Run) inject $PORT and expect the
# process to honour it; 8000 is the local default.
ENV PORT=8000
CMD ["sh", "-c", "exec uvicorn detoura.api.app:app --host 0.0.0.0 --port ${PORT}"]
