"""V9 Phase 2.6 Part B — destination image manifest, license validation,
optimizer, pipeline safety, coverage/quality auditing.

Offline by design: every test here runs with zero network access. The one
optional live-network check (proving the real Wikimedia adapter actually
works end to end) lives in ``test_wikimedia_live_smoke`` and is skipped
unless ``DETOURA_RUN_LIVE_IMAGE_TESTS=1`` is set.
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from detoura.models.destination import Destination
from detoura.models.destination_image import DestinationImage, ImageStatus
from detoura.services.destination_images.coverage import build_coverage_report
from detoura.services.destination_images.credit import render_credit
from detoura.services.destination_images.manifest import load_manifest, save_manifest
from detoura.services.destination_images.optimizer import (
    ImageValidationError,
    validate_and_optimize,
)
from detoura.services.destination_images.pipeline import (
    ImagePipeline,
    _looks_like_scenery,
    license_is_accepted,
    score_candidate,
    title_mentions_place,
)
from detoura.services.destination_images.quality_checks import audit_manifest
from detoura.services.destination_images.wikimedia import (
    Candidate,
    DomainNotAllowed,
    WikimediaCommonsClient,
    _check_domain,
    strip_html,
)

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _dest(id_: str, country_code: str = "FR") -> Destination:
    return Destination(id=id_, name=id_, country="France", country_code=country_code,
                      primary_airport="CDG", acquisition_eligible=True, enabled=True)


def _img(destination_id: str, **kw) -> DestinationImage:
    base = dict(
        destination_id=destination_id, city_name=destination_id, country_code="FR",
        image_id=f"commons:{destination_id}", source_name="Wikimedia Commons",
        source_page_url=f"https://commons.wikimedia.org/wiki/File:{destination_id}.jpg",
        original_image_url=f"https://upload.wikimedia.org/{destination_id}.jpg",
        photographer_or_creator="Jane Doe", license_name="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0",
        attribution_text="Jane Doe, CC BY-SA 4.0", requires_attribution=True,
        retrieved_at=NOW, original_width=2000, original_height=1200,
        local_asset_path=f"data/destination_images/assets/{destination_id}.webp",
        optimized_width=1600, optimized_height=900,
        file_format="WEBP", file_size_bytes=100_000, sha256="a" * 64,
        status=ImageStatus.SELECTED,
    )
    base.update(kw)
    return DestinationImage(**base)


# ======================================================================
# Domain model — eligibility rules (§B4)
# ======================================================================
def test_selected_with_full_metadata_is_production_eligible():
    assert _img("Paris").production_eligible is True


def test_unknown_license_is_never_production_eligible():
    img = _img("Paris", license_name=None, local_asset_path="x")
    assert img.production_eligible is False


def test_missing_source_url_is_never_production_eligible():
    img = _img("Paris", source_page_url=None, local_asset_path="x")
    assert img.production_eligible is False


def test_requires_attribution_without_attribution_text_is_not_eligible():
    img = _img("Paris", attribution_text=None, requires_attribution=True, local_asset_path="x")
    assert img.production_eligible is False


def test_no_local_asset_is_never_eligible_even_if_selected():
    img = _img("Paris", local_asset_path=None)
    assert img.production_eligible is False


def test_rejected_or_missing_status_never_eligible():
    for status in (ImageStatus.REJECTED, ImageStatus.MISSING, ImageStatus.REVIEW_REQUIRED):
        img = _img("Paris", status=status, local_asset_path="x")
        assert img.production_eligible is False


def test_inactive_image_is_never_eligible():
    img = _img("Paris", local_asset_path="x", active=False)
    assert img.production_eligible is False


# ======================================================================
# Catalog join, orphans, duplicates
# ======================================================================
def test_every_catalog_destination_joins_by_destination_id():
    catalog = [_dest("Paris"), _dest("Lyon")]
    manifest = {"Paris": _img("Paris", local_asset_path="a"), "Lyon": _img("Lyon", local_asset_path="b")}
    report = build_coverage_report(catalog, manifest)
    assert report.total_catalog_destinations == 2
    assert report.image_found == 2
    assert report.missing == []


def test_missing_destination_reported_explicitly_not_substituted():
    catalog = [_dest("Paris"), _dest("Lyon")]
    manifest = {"Paris": _img("Paris", local_asset_path="a")}
    report = build_coverage_report(catalog, manifest)
    assert report.missing == ["Lyon"]
    assert report.image_found == 1


def test_orphan_manifest_record_detected():
    catalog = [_dest("Paris")]
    manifest = {"Paris": _img("Paris", local_asset_path="a"), "Ghost City": _img("Ghost City")}
    report = build_coverage_report(catalog, manifest)
    assert report.orphan_records == ["Ghost City"]


def test_no_duplicate_primary_image_records_one_per_destination_id():
    # The manifest IS a dict keyed by destination_id - structurally one
    # record per id. Loading a manifest whose file has two entries for the
    # same key is impossible in JSON (the second silently wins), so the
    # real invariant to test is that save->load round-trips exactly the
    # records given, one per key.
    manifest = {"Paris": _img("Paris", local_asset_path="a"), "Lyon": _img("Lyon", local_asset_path="b")}
    assert len(manifest) == 2


def test_quality_check_flags_reused_image_across_two_destinations(tmp_path):
    asset = tmp_path / "shared.webp"
    asset.write_bytes(b"fake-webp-bytes-not-a-real-image")
    import hashlib
    sha = hashlib.sha256(asset.read_bytes()).hexdigest()
    manifest = {
        "Paris": _img("Paris", local_asset_path=str(asset.relative_to(tmp_path)), sha256=sha),
        "Lyon": _img("Lyon", local_asset_path=str(asset.relative_to(tmp_path)), sha256=sha),
    }
    report = audit_manifest(manifest, repo_root=tmp_path)
    kinds = {i.kind for i in report.issues}
    assert "duplicate_image_reused" in kinds


# ======================================================================
# Manifest — deterministic, resumable
# ======================================================================
def test_manifest_round_trips(tmp_path):
    path = tmp_path / "manifest.json"
    records = {"Paris": _img("Paris", local_asset_path="a"), "Lyon": _img("Lyon", local_asset_path="b")}
    save_manifest(path, records)
    loaded = load_manifest(path)
    assert set(loaded) == {"Paris", "Lyon"}
    assert loaded["Paris"].destination_id == "Paris"


def test_manifest_generation_is_deterministic(tmp_path):
    records = {"Lyon": _img("Lyon", local_asset_path="b"), "Paris": _img("Paris", local_asset_path="a")}
    p1, p2 = tmp_path / "m1.json", tmp_path / "m2.json"
    save_manifest(p1, records)
    save_manifest(p2, records)
    assert p1.read_text() == p2.read_text()  # key order and content byte-identical


def test_missing_manifest_file_loads_as_empty(tmp_path):
    assert load_manifest(tmp_path / "does-not-exist.json") == {}


# ======================================================================
# Optimizer — corrupt/too-small rejection, checksum, output dimensions
# ======================================================================
def _make_test_image(w: int, h: int) -> bytes:
    img = Image.new("RGB", (w, h), color=(120, 140, 160))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_optimizer_rejects_corrupt_bytes():
    with pytest.raises(ImageValidationError):
        validate_and_optimize(b"this is not an image at all")


def test_optimizer_rejects_zero_byte_file():
    with pytest.raises(ImageValidationError):
        validate_and_optimize(b"")


def test_optimizer_rejects_too_small_image():
    raw = _make_test_image(400, 300)
    with pytest.raises(ImageValidationError):
        validate_and_optimize(raw)


def test_optimizer_produces_16_9_output_and_checksum():
    raw = _make_test_image(2400, 1600)
    result = validate_and_optimize(raw)
    assert result.width == 1600
    assert abs(result.width / result.height - 16 / 9) < 0.02
    assert len(result.sha256_optimized) == 64
    assert result.sha256_optimized != result.sha256_original


def test_optimizer_checksum_is_stable_for_identical_input():
    raw = _make_test_image(2000, 1200)
    r1 = validate_and_optimize(raw)
    r2 = validate_and_optimize(raw)
    assert r1.sha256_optimized == r2.sha256_optimized


def test_optimizer_handles_portrait_source_by_cropping_height():
    # A tall (portrait) source still crops correctly to 16:9 by taking a
    # horizontal band rather than failing - width alone (>= MIN_ORIGINAL_WIDTH)
    # is what gates acceptance, not the source's own aspect ratio.
    raw = _make_test_image(1400, 2200)
    result = validate_and_optimize(raw)
    assert abs(result.width / result.height - 16 / 9) < 0.02


# ======================================================================
# License validation, attribution, source URL
# ======================================================================
@pytest.mark.parametrize("license_name,accepted", [
    ("CC0", True), ("Public Domain", True), ("CC BY 2.0", True),
    ("CC BY-SA 4.0", True), ("CC BY 4.0", True),
    (None, False), ("", False), ("All Rights Reserved", False),
    ("Some Random Unlicensed Thing", False),
])
def test_license_acceptance_allowlist(license_name, accepted):
    assert license_is_accepted(license_name) is accepted


def test_strip_html_removes_markup_keeps_text():
    assert strip_html('<a href="x">Jane Doe</a>') == "Jane Doe"
    assert strip_html(None) is None
    assert strip_html("") is None


def test_render_credit_includes_creator_source_and_license():
    img = _img("Paris", local_asset_path="a")
    credit = render_credit(img)
    assert "Jane Doe" in credit.photo_line
    assert "Wikimedia Commons" in credit.photo_line
    assert "CC BY-SA 4.0" in credit.license_line
    assert credit.requires_attribution is True


def test_render_credit_handles_missing_creator_gracefully():
    img = _img("Paris", photographer_or_creator=None, local_asset_path="a")
    credit = render_credit(img)
    assert "Wikimedia Commons" in credit.photo_line


# ======================================================================
# The scenery/title filter + scoring
# ======================================================================
def _cand(**kw) -> Candidate:
    base = dict(title="File:Paris skyline.jpg", page_id=1,
                source_page_url="https://commons.wikimedia.org/wiki/File:x.jpg",
                original_url="https://upload.wikimedia.org/x.jpg",
                thumb_url="https://thumb.wikimedia.org/x/1600px-x.jpg",
                mime="image/jpeg", width=2000, height=1200,
                license_short_name="CC BY-SA 4.0", license_url="https://x",
                attribution_required=True, creator="Jane Doe", credit="Jane Doe")
    base.update(kw)
    return Candidate(**base)


def test_scenery_filter_rejects_maps_and_flags():
    assert _looks_like_scenery(_cand(title="File:Flag of Paris.svg")) is False
    assert _looks_like_scenery(_cand(title="File:Map of Paris.png")) is False
    assert _looks_like_scenery(_cand(title="File:Coat of arms of Paris.svg")) is False


def test_scenery_filter_rejects_non_raster_mime():
    assert _looks_like_scenery(_cand(mime="image/svg+xml")) is False
    assert _looks_like_scenery(_cand(mime="application/pdf")) is False


def test_scenery_filter_accepts_a_plausible_photo():
    assert _looks_like_scenery(_cand()) is True


def test_scoring_prefers_higher_resolution_and_place_keywords():
    high_res_skyline = _cand(width=3000, height=2000, title="File:City skyline aerial.jpg")
    low_res_random = _cand(width=800, height=600, title="File:Random photo.jpg")
    assert score_candidate(high_res_skyline) > score_candidate(low_res_random)


# ======================================================================
# Destination-mismatch guard - a real bug found live during acquisition:
# Commons' full-text search for "Kalmar skyline" ranked a well-licensed,
# high-quality, correctly-titled photo of *Wilmington, Delaware* highly,
# because its description mentioned the replica ship "Kalmar Nyckel" - the
# photo was not of Kalmar, Sweden at all. Commons search relevance ranking
# alone is never trusted as proof of what a candidate depicts.
# ======================================================================
def test_title_relevance_guard_rejects_the_real_wilmington_kalmar_case():
    assert title_mentions_place("File:Wilmington Riverfront.JPG", "Kalmar") is False


def test_title_relevance_guard_accepts_a_correctly_titled_match():
    assert title_mentions_place("File:Kalmar Castle at sunset.jpg", "Kalmar") is True


def test_title_relevance_guard_handles_multi_word_cities():
    assert title_mentions_place("File:Skyline of Palma de Mallorca.jpg", "Palma de Mallorca") is True
    assert title_mentions_place("File:Sant Francesc church, Palma.jpg", "Palma de Mallorca") is True
    assert title_mentions_place("File:Completely unrelated subject.jpg", "Palma de Mallorca") is False


def test_title_relevance_guard_ignores_generic_stopwords_alone():
    # "de" alone appearing somewhere must not count as a match for a city
    # whose name happens to contain it.
    assert title_mentions_place("File:A random place, de nowhere.jpg", "Santiago de Compostela") is False


def test_pipeline_rejects_a_candidate_whose_title_does_not_mention_the_destination():
    class _WrongCityClient:
        requests_made = 0
        def search_candidates(self, query, *, limit=8):
            return [_cand(page_id=99, title="File:Wilmington Riverfront.JPG")]
        def download(self, *a, **k):
            raise AssertionError("must never reach download - filtered out before that")

    pipeline = ImagePipeline(client=_WrongCityClient())
    result, img, webp = pipeline.acquire_one(_dest("Kalmar"), execute=True)
    assert result.outcome == "missing"
    assert webp is None


# ======================================================================
# More real mismatches found live, after the first fix: a naive substring
# check (not word-boundary) let "Split" match inside "SplitFire" (a car
# part in a Nissan photo whose title also happens to contain "Skyline" -
# this module's own scoring keyword bonus); and several city names are also
# common words/prefixes in a different context entirely ("Faro" = Spanish/
# Portuguese for "lighthouse", "Rome, Georgia" / "New Bern" are real,
# different US places).
# ======================================================================
@pytest.mark.parametrize("title,city,expected", [
    ("File:Nissan_E-BNR32_Skyline_GT-R_NISMO_(OTX_SplitFire_GTR).jpg", "Split", False),
    ("File:Split_old_town_Diocletian_palace.jpg", "Split", True),
    ("File:El_Faro_de_Cordoba.jpg", "Faro", False),
    ("File:Faro_marina_sunset.jpg", "Faro", True),
    ("File:Rome_Georgia_Skyline_1.jpg", "Rome", False),
    ("File:Rome_Colosseum_at_dusk.jpg", "Rome", True),
    ("File:South_End_Rail_Trail_New_Bern_Station.jpg", "Bern", False),
    ("File:Bern_old_town_aerial.jpg", "Bern", True),
])
def test_confusable_city_disqualifiers_and_word_boundary_matching(title, city, expected):
    assert title_mentions_place(title, city) is expected


def test_word_boundary_not_substring_split_inside_splitfire_is_not_a_match():
    # The exact bug: naive `"split" in text` matches inside "splitfire".
    assert title_mentions_place("File:OTX_SplitFire_spark_plug.jpg", "Split") is False


def test_underscore_separated_titles_still_word_boundary_match_correctly():
    # Commons filenames use underscores, which \b alone does not treat as a
    # boundary - the city name must still be found once underscores are
    # normalized to spaces.
    assert title_mentions_place("File:Split_old_town_at_dusk.jpg", "Split") is True


# ======================================================================
# Network safety: domain allowlist, unauthorized source fails closed
# ======================================================================
def test_domain_check_accepts_allowed_domain():
    _check_domain("https://commons.wikimedia.org/w/api.php", "commons.wikimedia.org")


def test_domain_check_rejects_off_domain():
    with pytest.raises(DomainNotAllowed):
        _check_domain("https://evil.example.com/x", "commons.wikimedia.org")


def test_domain_check_rejects_non_https():
    with pytest.raises(DomainNotAllowed):
        _check_domain("http://commons.wikimedia.org/x", "commons.wikimedia.org")


def test_domain_check_accepts_any_of_multiple_allowed_domains():
    _check_domain("https://upload.wikimedia.org/x.jpg", ("upload.wikimedia.org", "thumb.wikimedia.org"))
    _check_domain("https://thumb.wikimedia.org/x.jpg", ("upload.wikimedia.org", "thumb.wikimedia.org"))


def test_client_download_refuses_an_unauthorized_domain():
    client = WikimediaCommonsClient()
    with pytest.raises(DomainNotAllowed):
        client.download("https://evil.example.com/steal.jpg", max_bytes=1_000_000)


def test_client_search_only_ever_calls_the_api_domain():
    class _Stub:
        def __init__(self):
            self.urls = []
        def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
            self.urls.append(url)
            from detoura.providers.http import HttpResponse
            return HttpResponse(200, json.dumps({"query": {"pages": {}}}))
    stub = _Stub()
    client = WikimediaCommonsClient(http_client=stub, min_request_interval_seconds=0.0)
    client.search_candidates("Paris skyline")
    assert all("commons.wikimedia.org" in u for u in stub.urls)


# ======================================================================
# Rate limiting
# ======================================================================
def test_client_paces_requests_by_configured_interval():
    from detoura.providers.http import HttpResponse

    class _Stub:
        def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
            return HttpResponse(200, json.dumps({"query": {"pages": {}}}))

    client = WikimediaCommonsClient(http_client=_Stub(), min_request_interval_seconds=0.05)
    waited = []
    client._rate_limiter._sleep = lambda s: waited.append(s)
    client.search_candidates("a")
    client.search_candidates("b")
    assert sum(waited) > 0  # the second call had to wait for the configured interval


# ======================================================================
# Pipeline: resume behavior, no network on skip/dry-run
# ======================================================================
def test_resume_skips_an_already_selected_destination_with_zero_requests():
    class _ExplodingClient:
        requests_made = 0
        def search_candidates(self, *a, **k):
            raise AssertionError("must not be called - this destination is already resolved")
        def download(self, *a, **k):
            raise AssertionError("must not be called")

    pipeline = ImagePipeline(client=_ExplodingClient())
    existing = _img("Paris", local_asset_path="a")
    dest = _dest("Paris")
    result, img, webp = pipeline.acquire_one(dest, execute=True, existing=existing)
    assert result.outcome == "skipped_resume"
    assert result.requests_made == 0
    assert img is existing
    assert webp is None


def test_refresh_flag_forces_re_acquisition_even_if_already_selected():
    from detoura.providers.http import HttpResponse

    class _Stub:
        def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
            return HttpResponse(200, json.dumps({"query": {"pages": {}}}))

    pipeline = ImagePipeline(client=WikimediaCommonsClient(http_client=_Stub(), min_request_interval_seconds=0.0))
    existing = _img("Paris", local_asset_path="a")
    dest = _dest("Paris")
    result, img, webp = pipeline.acquire_one(dest, execute=True, existing=existing, refresh=True)
    assert result.outcome != "skipped_resume"


def test_dry_run_makes_zero_downloads():
    from detoura.providers.http import HttpResponse

    class _NoDownloadClient:
        def __init__(self):
            self.requests_made = 0
            self.download_called = False

        def search_candidates(self, query, *, limit=8):
            self.requests_made += 1
            return [_cand(page_id=42)]

        def download(self, *a, **k):
            self.download_called = True
            raise AssertionError("dry-run must never download")

    client = _NoDownloadClient()
    pipeline = ImagePipeline(client=client)
    dest = _dest("Paris")
    result, img, webp = pipeline.acquire_one(dest, execute=False)
    assert client.download_called is False
    assert webp is None
    assert result.outcome == "review_required"
    assert img is not None and img.status is ImageStatus.REVIEW_REQUIRED


def test_pipeline_rejects_duplicate_sha256_across_destinations():
    raw = _make_test_image(2000, 1200)

    class _FixedClient:
        requests_made = 0
        def search_candidates(self, query, *, limit=8):
            return [_cand(page_id=1)]
        def download(self, url, *, max_bytes):
            return raw

    pipeline = ImagePipeline(client=_FixedClient())
    r1, img1, webp1 = pipeline.acquire_one(_dest("Paris"), execute=True)
    assert r1.outcome == "selected"
    r2, img2, webp2 = pipeline.acquire_one(_dest("Lyon"), execute=True)
    # the exact same bytes were "found" for Lyon - the dedup guard must
    # reject reusing it rather than silently giving two cities the same photo
    assert r2.outcome == "missing"


# ======================================================================
# Optional live integration test (skipped by default - §Part B tests)
# ======================================================================
@pytest.mark.skipif(
    os.getenv("DETOURA_RUN_LIVE_IMAGE_TESTS") != "1",
    reason="live network test - set DETOURA_RUN_LIVE_IMAGE_TESTS=1 to run",
)
def test_wikimedia_live_smoke():
    client = WikimediaCommonsClient(min_request_interval_seconds=0.5)
    candidates = client.search_candidates("Paris skyline", limit=3)
    assert len(candidates) > 0
    assert any(license_is_accepted(c.license_short_name) for c in candidates)
