"""V9 Phase 2.6 Part B — the destination-image acquisition CLI (§B7).

Deterministic, resumable: re-running skips any destination already
``SELECTED`` in the manifest unless ``--refresh <id>`` (or ``--refresh-all``)
is given. ``--dry-run`` does the whole discovery/scoring pass with **zero**
downloads (§B10 "no network during dry-run" is the test-suite's
responsibility; this flag is the same guarantee for a live run against
Wikimedia Commons).

Usage::

    python3 scripts/acquire_destination_images.py --limit 10
    python3 scripts/acquire_destination_images.py                # full catalog
    python3 scripts/acquire_destination_images.py --dry-run
    python3 scripts/acquire_destination_images.py --refresh Paris
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detoura.data.destinations import acquisition_catalog  # noqa: E402
from detoura.models.destination_image import ImageStatus  # noqa: E402
from detoura.services.destination_images.manifest import load_manifest, save_manifest  # noqa: E402
from detoura.services.destination_images.pipeline import ImagePipeline  # noqa: E402
from detoura.services.destination_images.wikimedia import WikimediaCommonsClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "data" / "destination_images" / "manifest.json"
ASSETS_DIR = REPO_ROOT / "data" / "destination_images" / "assets"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="only process the first N destinations")
    parser.add_argument("--dry-run", action="store_true", help="discovery/scoring only, zero downloads")
    parser.add_argument("--refresh", action="append", default=[], help="destination id to force re-acquire")
    parser.add_argument("--refresh-all", action="store_true")
    parser.add_argument("--rate-seconds", type=float, default=1.0, help="minimum seconds between HTTP requests")
    args = parser.parse_args()

    catalog = acquisition_catalog()
    if args.limit:
        catalog = catalog[: args.limit]

    manifest = load_manifest(MANIFEST_PATH)
    client = WikimediaCommonsClient(min_request_interval_seconds=args.rate_seconds)
    pipeline = ImagePipeline(client=client)
    for rec in manifest.values():
        if rec.sha256:
            pipeline.used_sha256.add(rec.sha256)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    counts = {"selected": 0, "review_required": 0, "missing": 0, "skipped_resume": 0}
    started = time.perf_counter()
    for i, dest in enumerate(catalog, 1):
        refresh = args.refresh_all or dest.id in args.refresh
        existing = manifest.get(dest.id)
        result, img, webp_bytes = pipeline.acquire_one(
            dest, execute=not args.dry_run, refresh=refresh, existing=existing,
        )
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
        if img is not None:
            if webp_bytes is not None:
                asset_path = ASSETS_DIR / f"{dest.id}.webp"
                asset_path.write_bytes(webp_bytes)
                img = img.model_copy(update={"local_asset_path": str(asset_path.relative_to(REPO_ROOT))})
            manifest[dest.id] = img
        print(f"[{i}/{len(catalog)}] {dest.id:30s} {result.outcome:16s} "
              f"reqs={result.requests_made:2d} {result.notes}", flush=True)
        if i % 10 == 0:
            save_manifest(MANIFEST_PATH, manifest)  # checkpoint - resumable if interrupted

    save_manifest(MANIFEST_PATH, manifest)
    elapsed = time.perf_counter() - started
    print(f"\nDone in {elapsed:.1f}s. requests_made(total)={client.requests_made}")
    print(f"counts: {counts}")


if __name__ == "__main__":
    main()
