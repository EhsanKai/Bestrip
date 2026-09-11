"""V9 Phase 2.6 Part B — generate the human-review contact sheet (§B8).

Reads the manifest and local assets only — no network. The output is a
review tool, not a Detoura page.

Usage::

    python3 scripts/generate_image_review_gallery.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.services.destination_images.gallery import write_gallery  # noqa: E402
from detoura.services.destination_images.manifest import load_manifest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "data" / "destination_images" / "manifest.json"
OUT_PATH = REPO_ROOT / "data" / "destination_images" / "reports" / "review_gallery.html"


def main() -> None:
    records = load_manifest(MANIFEST_PATH)
    write_gallery(records, out_path=OUT_PATH, repo_root=REPO_ROOT)
    print(f"Wrote {OUT_PATH} ({len(records)} destinations)")


if __name__ == "__main__":
    main()
