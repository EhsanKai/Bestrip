"""Destination-image frontend integration seam (V9 Phase 2.6 §B11).

A NEW, additive read-only endpoint — no existing response model changes, so
existing API compatibility is untouched. Serves the pre-built manifest
(``data/destination_images/manifest.json``); no image processing happens at
request time. Keyed by ``destination_id``, never city-name string matching,
so the frontend can join it directly against catalog/search results.

Not itself the frontend — no page, no design decision. Just the seam a
later UI change can consume: ``image.primary`` (the served asset URL),
``image.credit``, ``image.aspect_ratio``, ``image.focal_point``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles

from ..models.destination_image import DestinationImage
from ..services.destination_images.credit import render_credit
from ..services.destination_images.manifest import load_manifest

router = APIRouter(prefix="/api/v1/destinations", tags=["destination-images"])


def _destination_images_dir() -> Path:
    """Locate the directory holding ``manifest.json`` and ``assets/``.

    ``DETOURA_DESTINATION_IMAGES_DIR`` wins when set (see the Dockerfile) -
    the same pattern ``static.frontend_dist`` uses for the frontend build,
    and for the same reason: once this package is ``pip install``-ed, the
    running module lives under ``site-packages/detoura/api/...``, not under
    a checkout's ``src/detoura/api/...``, so a ``__file__``-relative guess at
    ``parents[3]`` silently resolves to the wrong directory (a real bug this
    project's own Docker health-check caught: assets present on disk but the
    API served an empty manifest because it was looking inside
    site-packages). The repo-relative fallback below is for a developer
    checkout only, never relied on in a packaged/deployed process."""
    override = os.getenv("DETOURA_DESTINATION_IMAGES_DIR")
    if override:
        return Path(override).expanduser().resolve()
    # src/detoura/api/destination_images.py -> src/detoura/api -> src/detoura -> src -> root
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "data" / "destination_images"


_DEST_IMAGES_DIR = _destination_images_dir()
_MANIFEST_PATH = _DEST_IMAGES_DIR / "manifest.json"
_ASSETS_DIR = _DEST_IMAGES_DIR / "assets"

#: Where the optimized assets are actually mounted for serving. Not
#: ``local_asset_path`` directly (a repo-relative path, meaningful to the
#: acquisition tooling, not a URL).
ASSET_URL_PREFIX = "/destination-images"


def mount_destination_images(app: FastAPI) -> bool:
    """Serves the optimized WebP assets at ``/destination-images/*.webp``.

    A no-op when there is no acquired library on disk yet — the same
    "absent build, no error" shape as ``static.mount_frontend`` for the
    frontend bundle. Must be called before the SPA catch-all route (see
    ``app.py``), for the same reason that one documents."""
    if not _ASSETS_DIR.is_dir():
        return False
    app.mount(ASSET_URL_PREFIX, StaticFiles(directory=_ASSETS_DIR), name="destination-images")
    return True


def _manifest_mtime() -> float:
    try:
        return _MANIFEST_PATH.stat().st_mtime
    except OSError:
        return 0.0


@lru_cache(maxsize=4)
def _cached_manifest(_mtime: float) -> dict[str, DestinationImage]:
    return load_manifest(_MANIFEST_PATH)


def _current_manifest() -> dict[str, DestinationImage]:
    """Reloads only when the manifest file has actually changed — this is a
    build-time artifact, not something the running app ever writes, so a
    process-lifetime cache keyed on mtime is enough and avoids re-parsing a
    ~200-entry JSON file on every request."""
    return _cached_manifest(_manifest_mtime())


def _image_dto(img: DestinationImage) -> dict | None:
    if not img.production_eligible:
        return None
    credit = render_credit(img)
    asset_url = (
        f"{ASSET_URL_PREFIX}/{Path(img.local_asset_path).name}" if img.local_asset_path else None
    )
    aspect_ratio = (
        round(img.optimized_width / img.optimized_height, 4)
        if img.optimized_width and img.optimized_height else None
    )
    return {
        "primary": asset_url,
        "credit": credit.combined,
        "photographer_or_creator": img.photographer_or_creator,
        "license_name": img.license_name,
        "license_url": img.license_url,
        "requires_attribution": credit.requires_attribution,
        "aspect_ratio": aspect_ratio,
        "focal_point": {"x": img.focal_point_x, "y": img.focal_point_y},
        "width": img.optimized_width,
        "height": img.optimized_height,
    }


@router.get("/images")
def list_destination_images() -> dict:
    """The full manifest, keyed by ``destination_id`` — only
    production-eligible entries; a MISSING/REVIEW_REQUIRED/REJECTED
    destination is simply absent, never a placeholder image (§B9: "do not
    silently substitute")."""
    manifest = _current_manifest()
    images = {did: dto for did, img in manifest.items() if (dto := _image_dto(img)) is not None}
    return {"images": images}


@router.get("/{destination_id}/image")
def get_destination_image(destination_id: str) -> dict | None:
    manifest = _current_manifest()
    img = manifest.get(destination_id)
    if img is None:
        return None
    return _image_dto(img)
