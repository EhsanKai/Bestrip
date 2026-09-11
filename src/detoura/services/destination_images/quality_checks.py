"""Post-hoc quality/integrity audit over an existing manifest (V9 Phase 2.6 §B10).

Runs entirely offline against the manifest + local asset files — no network,
so this is safe to run in CI or a pre-commit check, not just during
acquisition.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from ...models.destination_image import DestinationImage, ImageStatus, license_is_accepted
from .optimizer import MIN_ORIGINAL_HEIGHT, MIN_ORIGINAL_WIDTH
from .pipeline import title_mentions_place


@dataclass
class QualityIssue:
    destination_id: str
    kind: str
    detail: str


@dataclass
class QualityReport:
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict:
        return {"ok": self.ok, "issue_count": len(self.issues),
                "issues": [vars(i) for i in self.issues]}


def audit_manifest(records: dict[str, DestinationImage], *, repo_root: Path) -> QualityReport:
    report = QualityReport()
    sha_to_destinations: dict[str, list[str]] = {}

    for did, rec in records.items():
        if did != rec.destination_id:
            report.issues.append(QualityIssue(did, "key_mismatch",
                                              f"manifest key {did!r} != record.destination_id {rec.destination_id!r}"))

        if rec.status is not ImageStatus.SELECTED:
            continue  # only SELECTED records are held to the full production bar

        if not rec.source_page_url:
            report.issues.append(QualityIssue(did, "missing_source_url", "no source_page_url"))
        if not rec.license_name:
            report.issues.append(QualityIssue(did, "missing_license", "no license_name"))
        elif not license_is_accepted(rec.license_name):
            # Re-validated here, not just trusted from acquisition time: a
            # hand-edited manifest.json (or a future acquisition-side bug)
            # could otherwise write any string into license_name and have it
            # read as "understood" just because the field is non-empty.
            # Found by independent adversarial QA against a constructed
            # tampered record.
            report.issues.append(QualityIssue(did, "unaccepted_license",
                                              f"license_name {rec.license_name!r} is not on the configured allowlist"))
        if rec.requires_attribution and not rec.attribution_text:
            report.issues.append(QualityIssue(did, "missing_attribution", "requires_attribution but no attribution_text"))

        if rec.source_page_url:
            # The URL is percent-encoded (Å -> %C3%85); decode it before
            # checking, or every non-ASCII city name false-positives here
            # (found live: Ålesund, Düsseldorf, Münster, Niš, Timișoara,
            # Iași, València, Zürich all correctly matched by the live
            # pipeline - which reads the API's own decoded title field - but
            # wrongly flagged by this offline re-check before this fix).
            decoded_url = unquote(rec.source_page_url)
            if not title_mentions_place(decoded_url, rec.city_name):
                report.issues.append(QualityIssue(
                    did, "destination_mismatch_suspected",
                    f"neither {rec.city_name!r} nor a distinctive word from it appears in "
                    f"the source title/URL {decoded_url!r} - possible wrong-city image",
                ))

        if not rec.local_asset_path:
            report.issues.append(QualityIssue(did, "no_local_asset", "SELECTED with no local_asset_path"))
            continue
        asset_path = repo_root / rec.local_asset_path
        if not asset_path.exists():
            report.issues.append(QualityIssue(did, "asset_file_missing", str(asset_path)))
            continue
        data = asset_path.read_bytes()
        if len(data) == 0:
            report.issues.append(QualityIssue(did, "zero_byte_file", str(asset_path)))
            continue
        actual_sha = hashlib.sha256(data).hexdigest()
        if rec.sha256 and actual_sha != rec.sha256:
            report.issues.append(QualityIssue(did, "sha256_mismatch",
                                              f"manifest says {rec.sha256}, file is {actual_sha}"))
        sha_to_destinations.setdefault(actual_sha, []).append(did)

        if rec.original_width and rec.original_width < MIN_ORIGINAL_WIDTH:
            report.issues.append(QualityIssue(did, "resolution_too_small",
                                              f"original_width {rec.original_width} < {MIN_ORIGINAL_WIDTH}"))
        if rec.original_height and rec.original_height < MIN_ORIGINAL_HEIGHT:
            report.issues.append(QualityIssue(did, "resolution_too_small",
                                              f"original_height {rec.original_height} < {MIN_ORIGINAL_HEIGHT}"))
        if rec.optimized_width and rec.optimized_height:
            aspect = rec.optimized_width / rec.optimized_height
            if abs(aspect - 16 / 9) > 0.02:
                report.issues.append(QualityIssue(did, "unexpected_aspect_ratio", f"{aspect:.3f}"))

    for sha, dests in sha_to_destinations.items():
        if len(dests) > 1:
            report.issues.append(QualityIssue(
                ",".join(sorted(dests)), "duplicate_image_reused",
                f"identical asset (sha256 {sha[:12]}...) used for {len(dests)} destinations: {sorted(dests)}",
            ))

    return report
