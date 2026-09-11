"""Coverage reporting against the live catalog (V9 Phase 2.6 §B9).

The catalog (``acquisition_catalog()``) is always the source of truth for
"how many destinations exist" — this module never hardcodes a city count.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...models.destination import Destination
from ...models.destination_image import DestinationImage, ImageStatus


@dataclass(frozen=True, slots=True)
class CoverageReport:
    total_catalog_destinations: int
    image_found: int
    """Has some manifest record, regardless of status."""
    production_license_eligible: int
    missing: list[str] = field(default_factory=list)
    review_required: list[str] = field(default_factory=list)
    orphan_records: list[str] = field(default_factory=list)
    """Manifest entries whose destination_id is no longer in the catalog."""

    def as_dict(self) -> dict:
        return {
            "total_catalog_destinations": self.total_catalog_destinations,
            "image_found": self.image_found,
            "production_license_eligible": self.production_license_eligible,
            "missing_count": len(self.missing),
            "review_required_count": len(self.review_required),
            "orphan_records_count": len(self.orphan_records),
            "missing": sorted(self.missing),
            "review_required": sorted(self.review_required),
            "orphan_records": sorted(self.orphan_records),
        }


def build_coverage_report(
    catalog: list[Destination], manifest: dict[str, DestinationImage],
) -> CoverageReport:
    catalog_ids = {d.id for d in catalog}
    missing, review_required = [], []
    found = eligible = 0
    for dest in catalog:
        rec = manifest.get(dest.id)
        if rec is None or rec.status is ImageStatus.MISSING:
            missing.append(dest.id)
            continue
        found += 1
        if rec.status is ImageStatus.REVIEW_REQUIRED:
            review_required.append(dest.id)
        if rec.production_eligible:
            eligible += 1
    orphans = [did for did in manifest if did not in catalog_ids]
    return CoverageReport(
        total_catalog_destinations=len(catalog), image_found=found,
        production_license_eligible=eligible, missing=missing,
        review_required=review_required, orphan_records=orphans,
    )
