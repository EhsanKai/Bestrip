"""V9 Phase 2.6 Part B — coverage + quality report (§B9, §B10).

Zero network. The catalog (``acquisition_catalog()``) is the source of
truth for destination count — never a hardcoded number.

Usage::

    python3 scripts/report_image_coverage.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.data.destinations import acquisition_catalog  # noqa: E402
from detoura.services.destination_images.coverage import build_coverage_report  # noqa: E402
from detoura.services.destination_images.manifest import load_manifest  # noqa: E402
from detoura.services.destination_images.quality_checks import audit_manifest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "data" / "destination_images" / "manifest.json"


def main() -> None:
    catalog = list(acquisition_catalog())
    manifest = load_manifest(MANIFEST_PATH)

    coverage = build_coverage_report(catalog, manifest)
    quality = audit_manifest(manifest, repo_root=REPO_ROOT)

    total_bytes = sum(r.file_size_bytes or 0 for r in manifest.values())
    print(json.dumps({
        "coverage": coverage.as_dict(),
        "quality": quality.as_dict(),
        "total_optimized_bytes": total_bytes,
        "total_optimized_mb": round(total_bytes / 1_000_000, 2),
        "note": "PRODUCTION_ELIGIBLE_BY_CONFIGURED_POLICY, not a legal clearance - see docs/V9_PHASE2_6_DESTINATION_IMAGES.md",
    }, indent=2))


if __name__ == "__main__":
    main()
