"""A static HTML contact-sheet for human review (V9 Phase 2.6 §B8).

A review tool, not a Detoura page: no app chrome, no design system, nothing
this project's frontend owner needs to weigh in on. Its only job is letting
a human scan every selection at once and see exactly what the pipeline
recorded for it.
"""

from __future__ import annotations

import html
from pathlib import Path

from ...models.destination_image import DestinationImage, ImageStatus

_STATUS_COLOR = {
    ImageStatus.SELECTED: "#1a7f37",
    ImageStatus.REVIEW_REQUIRED: "#9a6700",
    ImageStatus.REJECTED: "#cf222e",
    ImageStatus.MISSING: "#57606a",
}


def _card(img: DestinationImage, *, repo_root: Path) -> str:
    color = _STATUS_COLOR.get(img.status, "#57606a")
    thumb = ""
    if img.local_asset_path:
        asset = repo_root / img.local_asset_path
        if asset.exists():
            rel = asset.relative_to(repo_root)
            thumb = f'<img src="../{rel.as_posix()}" loading="lazy" alt="{html.escape(img.city_name)}">'
    if not thumb:
        thumb = '<div class="noimg">no image</div>'
    creator = html.escape(img.photographer_or_creator or "unknown")
    license_name = html.escape(img.license_name or "UNKNOWN LICENSE")
    source_link = (
        f'<a href="{html.escape(img.source_page_url)}" target="_blank" rel="noopener">source</a>'
        if img.source_page_url else "no source url"
    )
    dims = f"{img.optimized_width}×{img.optimized_height}" if img.optimized_width else "—"
    return f"""
    <div class="card">
      <div class="thumb">{thumb}</div>
      <div class="meta">
        <div class="city">{html.escape(img.city_name)} <span class="country">{html.escape(img.country_code or '')}</span></div>
        <div class="status" style="color:{color}">{img.status.value}</div>
        <div class="line">Creator: {creator}</div>
        <div class="line">License: {license_name}</div>
        <div class="line">{source_link} · {dims} · {html.escape(img.local_asset_path or 'no local asset')}</div>
        <div class="notes">{html.escape(img.selection_notes)}</div>
      </div>
    </div>"""


def generate_gallery(records: dict[str, DestinationImage], *, repo_root: Path) -> str:
    by_status: dict[str, list[DestinationImage]] = {}
    for img in records.values():
        by_status.setdefault(img.status.value, []).append(img)
    counts = {k: len(v) for k, v in by_status.items()}

    cards = "".join(
        _card(img, repo_root=repo_root)
        for img in sorted(records.values(), key=lambda r: (r.status.value, r.city_name))
    )
    summary = " · ".join(f"{k}: {v}" for k, v in sorted(counts.items()))

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Destination image review</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #0d1117; color: #e6edf3; margin: 0; padding: 24px; }}
  h1 {{ font-size: 20px; }}
  .summary {{ color: #8b949e; margin-bottom: 20px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }}
  .thumb {{ aspect-ratio: 16/9; background: #21262d; display: flex; align-items: center; justify-content: center; }}
  .thumb img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .noimg {{ color: #6e7681; font-size: 13px; }}
  .meta {{ padding: 10px 12px; }}
  .city {{ font-weight: 600; font-size: 15px; }}
  .country {{ color: #8b949e; font-weight: 400; font-size: 12px; }}
  .status {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; margin: 2px 0 6px; }}
  .line {{ font-size: 12px; color: #c9d1d9; margin: 2px 0; }}
  .line a {{ color: #58a6ff; }}
  .notes {{ font-size: 11px; color: #6e7681; margin-top: 6px; }}
</style></head>
<body>
  <h1>Destination image review — this is a review tool, not the Detoura app</h1>
  <div class="summary">{len(records)} destinations · {summary}</div>
  <div class="grid">{cards}</div>
</body></html>"""


def write_gallery(records: dict[str, DestinationImage], *, out_path: Path, repo_root: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(generate_gallery(records, repo_root=repo_root))
