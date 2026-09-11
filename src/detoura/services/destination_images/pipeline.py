"""The deterministic, resumable acquisition pipeline (V9 Phase 2.6 §B7).

    catalog -> query strategy -> candidate discovery -> license validation
            -> quality validation -> candidate scoring -> chosen image
            -> download -> optimization -> manifest

Resumable: a destination already ``SELECTED`` (or explicitly ``REJECTED``
with no better candidate available) is skipped on a re-run unless
``refresh=True`` is passed for it. A dry run (``execute=False``) does the
whole discovery/scoring pass with zero downloads, for planning/estimation
before spending any network requests.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ...models.destination import Destination
from ...models.destination_image import DestinationImage, ImageStatus
from .optimizer import ImageValidationError, validate_and_optimize
from .wikimedia import Candidate, WikimediaCommonsClient, license_is_accepted

#: Search query templates, tried in order until one yields an accepted
#: candidate — deliberately biased toward wide, place-establishing shots
#: over a single monument close-up (§B2: "avoid making every city just a
#: famous monument close-up").
QUERY_TEMPLATES = (
    "{city} skyline",
    "{city} cityscape",
    "{city} old town",
    "{city} aerial view",
    "{city} panorama",
    "{city} historic centre",
    "{city}, {country}",
)

#: Filenames containing any of these are almost never a usable destination
#: photo (maps, flags, coats of arms, diagrams, portraits of unrelated
#: subjects) - a cheap, high-value filter before spending a download on them.
_TITLE_BLOCKLIST = re.compile(
    r"\b(map|flag|crest|coat of arms|logo|seal|emblem|diagram|chart|graph|"
    r"stamp|banknote|coin|signature|portrait of|postcard|poster|icon|route|"
    r"airshow|air show|aircraft|airliner|fighter jet|warbird|warplane|biplane)\b",
    re.IGNORECASE,
)
# The airshow/aircraft terms were added after a live acquisition mismatch:
# a "Turku" candidate titled "...Turku Airshow..." passed the plain
# title_mentions_place check (the city name genuinely appears in the title)
# but the photographed subject is an airplane on a tarmac, not the city -
# the title-guard only proves relevance to a *query term*, never to what
# the image actually depicts, so an explicit off-topic-subject blocklist is
# the cheapest correct fix for this specific failure mode.

ACCEPTED_MIME = ("image/jpeg", "image/png")

MAX_DOWNLOAD_BYTES = 30_000_000

#: Short/generic words that appear inside many city names ("de", "las", ...)
#: but are useless as an identity signal on their own - checking for one of
#: these alone in a title would accept almost anything.
_PLACE_STOPWORDS = frozenset({
    "de", "da", "do", "la", "le", "les", "las", "los", "del", "der", "den",
    "the", "of", "an", "am", "on", "sur", "et", "und", "and", "san", "santa",
})

#: English catalog name -> local/alternate name(s) Commons file titles
#: routinely use instead. Found necessary live: without this, "Turin"
#: (Commons overwhelmingly titles files "Torino" or by landmark name),
#: "Munich"/"München", "Nuremberg"/"Nürnberg" and similar English-exonym
#: cities were false-rejected by the title-relevance guard below, which
#: would have thrown away perfectly correct images. Deliberately explicit
#: and short - only the catalog cities actually observed to need it, not a
#: general-purpose gazetteer.
_LOCAL_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "turin": ("torino",),
    "munich": ("münchen", "muenchen", "munchen"),
    "nuremberg": ("nürnberg", "nuernberg", "nurnberg"),
    "cologne": ("köln", "koeln", "koln"),
    "vienna": ("wien",),
    "prague": ("praha",),
    "florence": ("firenze",),
    "naples": ("napoli",),
    "milan": ("milano",),
    "venice": ("venezia",),
    "rome": ("roma",),
    "genoa": ("genova",),
    "lisbon": ("lisboa",),
    "seville": ("sevilla",),
    "athens": ("athina", "athinai"),
    "copenhagen": ("kobenhavn", "københavn"),
    "gothenburg": ("goteborg", "göteborg"),
    "zurich": ("zürich", "zuerich"),
    "geneva": ("geneve", "genève"),
    "brussels": ("bruxelles", "brussel"),
    "the hague": ("den haag",),
    "hague": ("den haag",),
    "warsaw": ("warszawa",),
    "gdansk": ("gdańsk",),
    "cracow": ("krakow", "kraków"),
    "krakow": ("kraków",),
}

#: A city name that is ALSO a common English word, or a literal prefix of a
#: well-known different place, is not disambiguated by a plain substring
#: match. Found live: "Nice, France" matched "Nice view of Chicago skyline"
#: (the English adjective, not the city) and "Porto" matched "Porto Alegre
#: cityscape at night" (a real, different city in Brazil). Each entry lists
#: phrases that - if present - mean the match is almost certainly NOT our
#: destination, checked before accepting the plain substring/word match.
_CONFUSABLE_DISQUALIFIERS: dict[str, tuple[str, ...]] = {
    "nice": ("nice view", "a nice ", "very nice", "nice picture", "nice photo",
            "nice shot", "nice pic", "looks nice", "nice day"),
    "porto": ("porto alegre", "porto rico", "porto novo", "porto seguro", "porto velho"),
    "york": ("new york",),
    "bath": ("bath tub", "bathroom", "bathtub"),
    "rome": ("rome georgia", "rome ga", "rome new york", "rome ny", "rome ohio", "rome oh"),
    "bern": ("new bern",),
    "faro": ("el faro de", "el faro (", "faro de "),  # "el faro" = "the lighthouse" (es/pt)
    "split": ("skyline gt-r", "gt-r", "nissan", "splitfire", "spark plug", "split ring"),
}


def _fold(s: str) -> str:
    """ASCII-fold: strip diacritics (é -> e, ü -> u, ł -> l via decompose)
    so "Zürich" and "Zurich" compare equal without needing every accented
    variant spelled out explicitly."""
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _looks_like_scenery(candidate: Candidate) -> bool:
    if candidate.mime not in ACCEPTED_MIME:
        return False
    if _TITLE_BLOCKLIST.search(candidate.title):
        return False
    return True


def title_mentions_place(title: str, city: str) -> bool:
    """A **destination-mismatch guard** (found live during acquisition: a
    "Kalmar skyline" search returned a real, well-licensed, high-quality
    photo of Wilmington, Delaware — titled "Wilmington Riverfront" — whose
    Commons description happened to mention the replica ship *Kalmar
    Nyckel*, which is enough for Commons' full-text search to rank it highly
    for the query, but the photo is not of Kalmar at all; the same live run
    also produced Providence, MA for "Bari", Lindau for "Porto", New York
    for "Nice" and Boston for "Nuremberg").

    Requires the city name — or a known local/alternate name for an
    English-exonym city (see ``_LOCAL_NAME_ALIASES``), diacritic-folded, or
    one of its non-generic words — to literally appear in the candidate's
    own title. Commons' search relevance ranking alone is never treated as
    proof the result depicts the place asked for.

    A **word-boundary** match, not a plain substring one: a naive substring
    check let "Split" match inside "SplitFire" (a spark-plug brand in a
    Nissan Skyline GT-R car photo — "Skyline" is also this module's own
    place-keyword scoring bonus, which is exactly how a car photo outscored
    real candidates for "Split, Croatia"). A short or common-word city name
    can still match for the wrong reason even at a whole-word boundary —
    "Nice" the English adjective, "Porto" heading "Porto Alegre" (a real,
    different city in Brazil), "Faro" as the Spanish/Portuguese common noun
    for "lighthouse", "Rome, Georgia" and "New Bern" — so a known
    disqualifying phrase for that city (``_CONFUSABLE_DISQUALIFIERS``)
    overrides an otherwise-successful match."""
    # Commons filenames separate words with underscores ("Split_old_town"),
    # which \b does not treat as a boundary (underscore is a \w character) -
    # normalize to spaces first, or "Split" never word-boundary-matches
    # "Split_old_town" at all.
    t = _fold(title.lower()).replace("_", " ")
    c = _fold(city.lower())
    if any(phrase in t for phrase in _CONFUSABLE_DISQUALIFIERS.get(c, ())):
        return False
    names_to_check = (c, *_LOCAL_NAME_ALIASES.get(c, ()))
    if any(_word_boundary_match(_fold(n), t) for n in names_to_check):
        return True
    words = [w for w in re.findall(r"[a-z]+", c) if w not in _PLACE_STOPWORDS and len(w) >= 3]
    if not words:
        return True  # nothing distinctive to check (e.g. a 1-2 letter id) - do not block on it
    return any(_word_boundary_match(w, t) for w in words)


def _word_boundary_match(needle: str, haystack: str) -> bool:
    """``needle`` must appear in ``haystack`` at a word boundary on both
    sides — a plain substring ``in`` check would let "split" match inside
    "splitfire", which is exactly the bug this closes."""
    if not needle:
        return False
    return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None


def score_candidate(candidate: Candidate) -> float:
    """Higher is better. Interpretable, not an opaque model: resolution
    (bigger is more useful, with diminishing returns), a same-city-name
    query term unsurprisingly present, and a bonus for place-establishing
    keywords over an incidental crowd/interior shot."""
    if not candidate.width or not candidate.height:
        return -1.0
    score = min(candidate.width * candidate.height / (1920 * 1080), 2.0)
    aspect = candidate.width / candidate.height
    score += 0.5 if 1.2 <= aspect <= 2.4 else -0.3  # landscape-ish, croppable to 16:9
    lowered = candidate.title.lower()
    for good in ("skyline", "aerial", "panorama", "cityscape", "old town", "harbour",
                "harbor", "waterfront", "historic centre", "historic center"):
        if good in lowered:
            score += 0.4
            break
    return score


@dataclass
class PipelineResult:
    destination_id: str
    outcome: str  # "selected" | "review_required" | "missing" | "skipped_resume"
    requests_made: int = 0
    notes: str = ""


@dataclass
class ImagePipeline:
    client: WikimediaCommonsClient = field(default_factory=WikimediaCommonsClient)
    used_sha256: set[str] = field(default_factory=set)

    def acquire_one(
        self, destination: Destination, *, execute: bool = True, refresh: bool = False,
        existing: DestinationImage | None = None,
    ) -> tuple[PipelineResult, DestinationImage | None, bytes | None]:
        """Returns ``(result, image_record, webp_bytes)``. ``webp_bytes`` is
        only non-``None`` on a freshly (re-)selected image in ``execute``
        mode — a resumed/skipped/dry-run/missing outcome has nothing new to
        write to disk."""
        if existing is not None and existing.status is ImageStatus.SELECTED and not refresh:
            self.used_sha256.add(existing.sha256 or "")
            return PipelineResult(destination.id, "skipped_resume"), existing, None

        candidates: list[Candidate] = []
        requests_before = self.client.requests_made
        for template in QUERY_TEMPLATES:
            query = template.format(city=destination.id, country=destination.country)
            found = self.client.search_candidates(query, limit=6)
            candidates.extend(
                c for c in found
                if _looks_like_scenery(c) and title_mentions_place(c.title, destination.id)
            )
            if len(candidates) >= 4:
                break
        requests_made = self.client.requests_made - requests_before

        eligible = [c for c in candidates if license_is_accepted(c.license_short_name)]
        if not eligible:
            return PipelineResult(destination.id, "missing", requests_made,
                                  "no candidate with an accepted license"), None, None

        eligible.sort(key=score_candidate, reverse=True)

        if not execute:
            best = eligible[0]
            img = DestinationImage(
                destination_id=destination.id, city_name=destination.id,
                country_code=destination.country_code, image_id=f"dryrun:{best.page_id}",
                source_name="Wikimedia Commons", source_page_url=best.source_page_url,
                original_image_url=best.original_url, photographer_or_creator=best.creator,
                license_name=best.license_short_name, license_url=best.license_url,
                attribution_text=best.credit or best.creator,
                requires_attribution=best.attribution_required,
                original_width=best.width, original_height=best.height,
                status=ImageStatus.REVIEW_REQUIRED,
                selection_notes="dry-run candidate - not downloaded",
            )
            return PipelineResult(destination.id, "review_required", requests_made,
                                  "dry-run: candidate found, not downloaded"), img, None

        for candidate in eligible:
            try:
                # The rendered thumbnail (Commons' own iiurlwidth-bounded
                # size), never the raw multi-megapixel original - Commons'
                # media CDN rate-limits repeated original-resolution fetches
                # and its own error message asks automated callers to use
                # thumbnails instead (see Candidate.download_url).
                raw = self.client.download(candidate.download_url, max_bytes=MAX_DOWNLOAD_BYTES)
            except Exception as exc:  # noqa: BLE001 - a download failure just tries the next candidate
                continue
            requests_made = self.client.requests_made - requests_before
            try:
                optimized = validate_and_optimize(raw)
            except ImageValidationError:
                continue
            if optimized.sha256_original in self.used_sha256:
                continue  # exact duplicate of an image already used elsewhere (§B10)

            attribution_text = candidate.credit or candidate.creator
            requires_attribution = (
                candidate.attribution_required if candidate.attribution_required is not None else True
            )
            if requires_attribution and not attribution_text:
                # A license that demands credit but a candidate with no
                # creator/credit field to build that credit from must not be
                # selected - it would land SELECTED with an unmeetable
                # attribution requirement (found live: Eindhoven, where
                # Commons' own Artist field was empty). Try the next
                # candidate instead of silently shipping an un-attributable
                # image.
                continue
            self.used_sha256.add(optimized.sha256_original)

            img = DestinationImage(
                destination_id=destination.id, city_name=destination.id,
                country_code=destination.country_code,
                image_id=f"commons:{candidate.page_id}",
                source_name="Wikimedia Commons", source_page_url=candidate.source_page_url,
                original_image_url=candidate.original_url,
                photographer_or_creator=candidate.creator,
                license_name=candidate.license_short_name, license_url=candidate.license_url,
                attribution_text=attribution_text,
                requires_attribution=requires_attribution,
                retrieved_at=datetime.now(timezone.utc),
                original_width=optimized.original_width, original_height=optimized.original_height,
                optimized_width=optimized.width, optimized_height=optimized.height,
                file_format="WEBP", file_size_bytes=len(optimized.webp_bytes),
                sha256=optimized.sha256_optimized,
                status=ImageStatus.SELECTED,
                selection_notes=f"auto-selected: score={score_candidate(candidate):.2f}",
            )
            return PipelineResult(destination.id, "selected", requests_made), img, optimized.webp_bytes

        return PipelineResult(destination.id, "missing", requests_made,
                              "every eligible candidate failed download/validation"), None, None
