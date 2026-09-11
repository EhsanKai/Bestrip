"""The destination-image provenance record (V9 Phase 2.6 Part B).

One :class:`DestinationImage` per destination's chosen primary hero photo.
Every field the acquisition pipeline could not determine is ``None``
(UNKNOWN), never guessed or defaulted to something that reads as real data.

**This module records provenance; it does not grant a license.** A
``license_name`` being present means the acquisition pipeline read and
stored a license declaration from the source — it means
``LICENSE_METADATA_VERIFIED`` (the metadata itself was captured and is
internally consistent), phrased deliberately short of a legal conclusion.
Nothing in this codebase may claim an image is "legally approved" for use;
see :attr:`DestinationImage.production_eligible` and its docstring.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


ACCEPTED_LICENSE_PREFIXES = (
    "CC0", "PUBLIC DOMAIN", "CC BY ", "CC BY-SA", "CC BY 2", "CC BY 3", "CC BY 4",
)
"""The production-eligibility license allowlist, defined once here (the
domain layer) so both acquisition-time filtering (``services.destination_images``)
and after-the-fact validation (``production_eligible`` below,
``quality_checks.audit_manifest``) enforce the identical rule. A record
whose ``license_name`` merely looks non-empty is NOT enough — it must
match this allowlist, or it is not production eligible, no matter how the
record entered the manifest (acquired, hand-edited, or a future pipeline
bug)."""


def license_is_accepted(license_short_name: str | None) -> bool:
    if not license_short_name:
        return False
    upper = license_short_name.strip().upper()
    return any(upper.startswith(p) for p in ACCEPTED_LICENSE_PREFIXES)


class ImageStatus(str, Enum):
    """The pipeline's own verdict on this candidate — never a legal
    conclusion, just where it landed in the acquisition/review process."""

    SELECTED = "SELECTED"
    """Chosen as the destination's primary image and eligible for use under
    the configured policy (see ``production_eligible``)."""
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    """A candidate exists but needs a human look before use — ambiguous
    license text, borderline quality, or an automated heuristic that could
    not confirm what §B2/§B10 require."""
    REJECTED = "REJECTED"
    """Considered and explicitly turned down (bad license, corrupt file,
    duplicate, wrong destination) — kept in the manifest so the pipeline
    never re-tries the same rejected candidate silently."""
    MISSING = "MISSING"
    """No candidate has been found for this destination at all."""


class DestinationImage(BaseModel):
    model_config = ConfigDict(frozen=True)

    destination_id: str = Field(min_length=1, max_length=200)
    city_name: str = Field(min_length=1, max_length=200)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)

    image_id: str = Field(min_length=1, max_length=200)
    source_name: str = Field(min_length=1, max_length=100)
    source_page_url: str | None = None
    original_image_url: str | None = None
    photographer_or_creator: str | None = None
    license_name: str | None = None
    """``None`` = UNKNOWN. An unknown license means this image is NOT
    eligible for production use, however good it looks (§B4)."""
    license_url: str | None = None
    attribution_text: str | None = None
    requires_attribution: bool | None = None
    retrieved_at: datetime | None = None

    original_width: int | None = None
    original_height: int | None = None
    local_asset_path: str | None = None
    optimized_width: int | None = None
    optimized_height: int | None = None
    file_format: str | None = None
    file_size_bytes: int | None = None
    sha256: str | None = None

    focal_point_x: float = Field(default=0.5, ge=0.0, le=1.0)
    focal_point_y: float = Field(default=0.5, ge=0.0, le=1.0)

    selection_notes: str = Field(default="", max_length=2000)
    status: ImageStatus = ImageStatus.MISSING
    active: bool = True

    @property
    def has_understood_license(self) -> bool:
        """``True`` only when ``license_name`` is both present AND on the
        configured production-license allowlist (:data:`ACCEPTED_LICENSE_PREFIXES`)
        — not merely non-empty. A record with a fabricated or unrecognized
        license string (e.g. "All Rights Reserved") must never read as
        "understood" just because some text was written into the field;
        found by independent adversarial QA against a hand-edited record."""
        return (
            bool(self.source_page_url)
            and license_is_accepted(self.license_name)
        )

    @property
    def production_eligible(self) -> bool:
        """``PRODUCTION_ELIGIBLE_BY_CONFIGURED_POLICY`` — not a legal
        conclusion. ``True`` only when the pipeline's own policy considers
        the recorded metadata sufficient: selected, a local optimized asset
        exists, the license is understood, and attribution text is present
        whenever the license requires it (§B4, the security/legal boundary
        note)."""
        if self.status is not ImageStatus.SELECTED or not self.active:
            return False
        if self.local_asset_path is None or self.sha256 is None:
            return False
        if not self.has_understood_license:
            return False
        if self.requires_attribution and not self.attribution_text:
            return False
        return True
