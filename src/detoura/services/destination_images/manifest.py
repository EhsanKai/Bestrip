"""The destination-image manifest: a versioned, deterministic JSON file
keyed by ``destination_id`` (V9 Phase 2.6 §B7, §B11).

Not a database table — this is a build-time asset artifact, read by the
running application as a plain file (see ``api/destination_images.py``),
exactly the "versioned static manifest keyed by destination_id" the frontend
integration seam calls for. The acquisition pipeline is the only writer;
the API only reads it.
"""

from __future__ import annotations

import json
from pathlib import Path

from ...models.destination_image import DestinationImage

MANIFEST_VERSION = 1


def load_manifest(path: Path) -> dict[str, DestinationImage]:
    """``{}`` if the file does not exist yet — a fresh pipeline run starts
    from nothing, not an error. Expects exactly the shape :func:`save_manifest`
    writes: ``{"version": ..., "destinations": {destination_id: record}}``."""
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    records = doc.get("destinations", {}) if isinstance(doc, dict) else {}
    return {did: DestinationImage(**rec) for did, rec in records.items()}


def save_manifest(path: Path, records: dict[str, DestinationImage]) -> None:
    """Deterministic output: keys sorted, so a re-run over unchanged data
    produces a byte-identical file (§B7 "the pipeline must be resumable",
    and a diff-friendly manifest for review)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": MANIFEST_VERSION,
        "destinations": {
            did: json.loads(records[did].model_dump_json())
            for did in sorted(records)
        },
    }
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
