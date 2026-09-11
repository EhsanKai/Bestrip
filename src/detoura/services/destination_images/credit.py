"""Reusable photo-credit rendering (V9 Phase 2.6 §B5).

One license does not dictate one attribution style — a CC0 image needs no
credit line at all, a CC-BY-SA one needs creator + license + a link. This
module renders whatever :class:`DestinationImage` actually recorded rather
than hardcoding a single template for every license.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...models.destination_image import DestinationImage


@dataclass(frozen=True, slots=True)
class PhotoCredit:
    #: e.g. "Photo: Jane Doe / Wikimedia Commons" - empty string when the
    #: license needs no attribution and none was recorded.
    photo_line: str
    #: e.g. "License: CC BY-SA 4.0" - empty string when unknown/not required.
    license_line: str
    #: The exact text to render if a caller wants one combined string.
    combined: str
    license_url: str | None
    source_page_url: str | None
    requires_attribution: bool


def render_credit(image: DestinationImage) -> PhotoCredit:
    creator = image.photographer_or_creator
    source = image.source_name

    if creator and source:
        photo_line = f"Photo: {creator} / {source}"
    elif source:
        photo_line = f"Photo: {source}"
    else:
        photo_line = ""

    license_line = f"License: {image.license_name}" if image.license_name else ""

    lines = [line for line in (photo_line, license_line) if line]
    combined = image.attribution_text or " · ".join(lines)

    return PhotoCredit(
        photo_line=photo_line, license_line=license_line, combined=combined,
        license_url=image.license_url, source_page_url=image.source_page_url,
        requires_attribution=bool(image.requires_attribution),
    )
