"""Decode, validate, crop and re-encode one destination photo (V9 Phase 2.6 §B6, §B10).

Pillow is the only new runtime dependency this touches, and only for the
acquisition *tooling* — the served application reads a pre-built manifest
and static WebP files, never processes an image at request time (see the
``images`` extra in ``pyproject.toml``).
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

TARGET_ASPECT = 16 / 9
TARGET_WIDTH = 1600
MIN_ORIGINAL_WIDTH = 1200
MIN_ORIGINAL_HEIGHT = 675
WEBP_QUALITY = 82


class ImageValidationError(ValueError):
    """The candidate fails a hard quality/integrity check (§B10) — never
    silently downgraded, the caller rejects the candidate outright."""


@dataclass(frozen=True, slots=True)
class OptimizedImage:
    webp_bytes: bytes
    width: int
    height: int
    original_width: int
    original_height: int
    sha256_original: str
    sha256_optimized: str


def _decode(raw: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()  # forces a full decode - a truncated file raises here (§B10 "corrupt download")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageValidationError(f"could not decode image: {exc}") from exc
    return img


def _crop_to_aspect(img: Image.Image, *, focal_x: float = 0.5, focal_y: float = 0.42) -> Image.Image:
    """Crops to :data:`TARGET_ASPECT`, keeping the region around
    ``(focal_x, focal_y)`` (fractional) rather than always the dead centre —
    a plain centre crop on a skyline shot routinely cuts the skyline off the
    top. Defaults bias slightly upward, which suits skylines/landmark shots
    better than a photo-agency portrait crop would."""
    w, h = img.size
    current_aspect = w / h
    if current_aspect > TARGET_ASPECT:
        new_w = int(h * TARGET_ASPECT)
        max_x = w - new_w
        x0 = min(max(int(w * focal_x - new_w / 2), 0), max_x)
        return img.crop((x0, 0, x0 + new_w, h))
    else:
        new_h = int(w / TARGET_ASPECT)
        max_y = h - new_h
        y0 = min(max(int(h * focal_y - new_h / 2), 0), max_y)
        return img.crop((0, y0, w, y0 + new_h))


def validate_and_optimize(
    raw: bytes, *, focal_x: float = 0.5, focal_y: float = 0.42,
    target_width: int = TARGET_WIDTH,
) -> OptimizedImage:
    """Raises :class:`ImageValidationError` for anything that fails
    decode/minimum-resolution — never returns a "best effort" degraded
    result for a candidate that should simply be rejected."""
    if not raw:
        raise ImageValidationError("zero-byte file")
    sha_original = hashlib.sha256(raw).hexdigest()
    img = _decode(raw)
    img = img.convert("RGB")  # drops any alpha/CMYK oddity before crop/resize
    w, h = img.size
    if w < MIN_ORIGINAL_WIDTH or h < MIN_ORIGINAL_HEIGHT:
        raise ImageValidationError(
            f"resolution {w}x{h} is below the minimum {MIN_ORIGINAL_WIDTH}x{MIN_ORIGINAL_HEIGHT}"
        )
    cropped = _crop_to_aspect(img, focal_x=focal_x, focal_y=focal_y)
    cw, ch = cropped.size
    if cw > target_width:
        scale = target_width / cw
        cropped = cropped.resize((target_width, round(ch * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    cropped.save(buf, format="WEBP", quality=WEBP_QUALITY, method=6)
    webp_bytes = buf.getvalue()
    return OptimizedImage(
        webp_bytes=webp_bytes, width=cropped.size[0], height=cropped.size[1],
        original_width=w, original_height=h,
        sha256_original=sha_original, sha256_optimized=hashlib.sha256(webp_bytes).hexdigest(),
    )
