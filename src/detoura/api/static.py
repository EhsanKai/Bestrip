"""Serving the built web client from the API process.

The frontend's API base defaults to ``/api/v1`` - a *relative* path - which
means the simplest correct deployment is one origin serving both: no CORS, no
second hostname, no build-time knowledge of where the API lives. This module is
what makes that deployment possible.

It is deliberately a no-op when there is no build on disk. Tests, ``uvicorn
--reload`` and the Vite dev server all run against a source tree with no
``frontend/dist``, and none of them should have to care that a production
concern exists.

Two rules the mount has to respect, both of which are correctness rather than
taste:

1. **The API wins.** Routers are included before this mount, and the SPA
   fallback explicitly refuses ``/api`` and the docs paths. If it did not, an
   unknown API route would return ``index.html`` with status 200, and the
   client - which parses every response as JSON - would report a syntax error
   instead of a 404.
2. **``index.html`` is never cached.** Vite fingerprints every asset it emits,
   so those are immutable and cached for a year; the HTML that names them must
   not be, or a deploy leaves browsers pinned to the previous build's assets.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

# Paths the SPA fallback must never answer for. A request under one of these
# that reached the fallback is a genuine 404, and has to be reported as one.
RESERVED_PREFIXES = ("/api", "/docs", "/redoc", "/openapi.json")

_IMMUTABLE = "public, max-age=31536000, immutable"
_NO_STORE = "no-cache, no-store, must-revalidate"


def frontend_dist() -> Path | None:
    """Locate the built client, or ``None`` if there is not one.

    ``DETOURA_FRONTEND_DIST`` wins when set, because a container copies the
    build to its own location rather than preserving the repository layout.
    Otherwise we look where a developer's ``npm run build`` puts it.
    """
    override = os.getenv("DETOURA_FRONTEND_DIST")
    if override:
        candidate = Path(override).expanduser().resolve()
        return candidate if (candidate / "index.html").is_file() else None

    # src/detoura/api/static.py -> src/detoura/api -> src/detoura -> src -> root
    repo_root = Path(__file__).resolve().parents[3]
    candidate = repo_root / "frontend" / "dist"
    return candidate if (candidate / "index.html").is_file() else None


class _CachingStaticFiles(StaticFiles):
    """Static files with the cache policy a fingerprinted build wants."""

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        path = str(getattr(response, "path", ""))
        if path.endswith(".html"):
            response.headers["Cache-Control"] = _NO_STORE
        elif "/assets/" in path.replace(os.sep, "/"):
            # Vite emits assets/ with a content hash in every filename.
            response.headers["Cache-Control"] = _IMMUTABLE
        return response


def mount_frontend(app: FastAPI) -> bool:
    """Serve the built client from ``app``, if a build exists.

    Returns whether anything was mounted, so the caller can report it. Must be
    called *after* every router is included: the catch-all route registered
    here would otherwise shadow them.
    """
    dist = frontend_dist()
    if dist is None:
        return False

    index = dist / "index.html"

    # Fingerprinted bundles. Mounted at the path Vite emits so that the hrefs
    # inside index.html resolve without rewriting anything.
    assets = dist / "assets"
    if assets.is_dir():
        app.mount(
            "/assets", _CachingStaticFiles(directory=assets), name="detoura-assets"
        )

    # GET *and* HEAD: uptime monitors, load balancers and CDNs all probe with
    # HEAD, and FastAPI - unlike bare Starlette - does not add it alongside GET.
    # Answering a monitor's HEAD / with 405 reads as an outage.
    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(request: Request, full_path: str) -> Response:
        """Serve a real file when there is one, else the app shell."""
        if request.url.path.startswith(RESERVED_PREFIXES):
            # Reached the fallback under a reserved prefix, so no route matched
            # it. Answer as the API, not as the app: the client parses this.
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        # A concrete file in the build (favicon.svg, robots.txt, ...) wins over
        # the shell. `resolve()` on both sides is what stops `../` escaping the
        # build directory.
        if full_path:
            target = (dist / full_path).resolve()
            if target.is_file() and target.is_relative_to(dist.resolve()):
                return FileResponse(target)

        return FileResponse(index, headers={"Cache-Control": _NO_STORE})

    return True
