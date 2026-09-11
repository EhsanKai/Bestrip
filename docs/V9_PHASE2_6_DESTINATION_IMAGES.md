# V9 Phase 2.6 Part B — Destination Image Library

Branch `claude/travel-planner-mvp-nvb267`. A real-photography hero-image
library for every destination in the live catalog
(`data.destinations.acquisition_catalog()` — never a hardcoded count), with
full provenance/license metadata and a static manifest seam the frontend
owner can consume later.

## Architecture

```
acquisition_catalog()  (source of truth for "which destinations exist")
      |
query strategy   (services/destination_images/pipeline.py: QUERY_TEMPLATES)
      |
Wikimedia Commons search + imageinfo API   (services/destination_images/wikimedia.py)
      |
scenery/mime/title filter -> license validation -> candidate scoring
      |
download (Commons' own size-bounded thumbnail, not the raw original)
      |
decode/validate/crop-to-16:9/resize/WebP-encode   (services/destination_images/optimizer.py)
      |
sha256 dedup check across the whole run
      |
data/destination_images/manifest.json  +  data/destination_images/assets/<id>.webp
      |
services/destination_images/quality_checks.py (offline audit)
services/destination_images/coverage.py (catalog-vs-manifest report)
services/destination_images/gallery.py (human review contact sheet)
      |
api/destination_images.py  ->  GET /api/v1/destinations/images
                                GET /api/v1/destinations/{id}/image
                                static assets at /destination-images/*.webp
```

## Source and licensing

**Wikimedia Commons only**, first in the source-priority order: every file
carries structured, machine-readable license/creator metadata via its
public `action=query` API — documented, intended for exactly this kind of
programmatic retrieval (`Commons:API`), not scraping the wiki's rendered
HTML. Two domains are allowlisted **by explicit name**, never a wildcard:
`commons.wikimedia.org` (search/metadata) and `upload.wikimedia.org` /
`thumb.wikimedia.org` (the media CDN — see "the rate-limit fix" below).

**Accepted licenses** (`wikimedia.license_is_accepted`): CC0, Public
Domain, and any CC BY / CC BY-SA variant. Anything else — including
Commons content under a license this project has not evaluated — is
rejected, not defaulted to "probably fine."

**No other source was used.** Google Images, Google Travel, Instagram,
Pinterest, blogs, OTAs, airline sites and arbitrary tourism pages were not
queried, scraped, or otherwise touched.

## The rate-limit fix (found and fixed mid-acquisition)

The first live run downloaded each candidate's **raw original** image
(`info.url`, sometimes 10-30 MB, tens of megapixels). Wikimedia's media CDN
began returning `HTTP 429` after a handful of these, with an error message
that explicitly says: *"please contact noc@wikimedia.org to discuss a less
disruptive approach or instead use thumbnail images."* This project follows
that instruction rather than finding a way around it (§B3 "do not bypass
rate limits") — the fix downloads Commons' own **rendered thumbnail**
(`iiurlwidth=1600`, served from `thumb.wikimedia.org`) instead, which is
both the officially-sanctioned way to fetch an appropriately-sized image for
this exact use case and lighter on both sides. `original_image_url` in the
manifest still records the true original URL for provenance/re-acquisition;
only the *download* target changed.

## The destination-mismatch guard (found and fixed mid-acquisition)

Commons' full-text search relevance was initially trusted implicitly: a
candidate that matched the *query* was assumed to actually depict the
*destination*. A manual visual spot-check after the first full run found
**"Kalmar" had selected a photo of the Wilmington, Delaware riverfront** —
the only connection was that the file's description mentioned a museum
ship named "Kalmar Nyckel"; the image itself has nothing to do with the
Swedish city. Commons search relevance is not the same thing as "this file
depicts this place," and nothing in the pipeline had checked that before.

This was treated as a real, systemic bug class, not a one-off, and was
investigated and fixed in several rounds, each caught by either the
offline audit or a further manual sample:

1. **Title-mentions-place filter added.** `pipeline.title_mentions_place(title, city)`
   became a hard filter at candidate-collection time (also run permanently,
   offline, by `quality_checks.py::audit_manifest` as
   `destination_mismatch_suspected`), and every destination was
   re-acquired against the new check.
2. **Diacritics and exonyms.** A plain ASCII substring check false-negatived
   on real, correct matches: "Zürich" vs. `zurich`, or an Italian/German
   Commons title using the local name — "Torino" for Turin, "München" for
   Munich, "Nürnberg" for Nuremberg. Fixed with NFKD diacritic-folding
   (`_fold`) plus a small, explicit `_LOCAL_NAME_ALIASES` table (~25
   entries) — not a general gazetteer, just the exonym/endonym pairs this
   catalog actually contains.
3. **The audit's own encoding bug.** `quality_checks.py` was comparing
   against the raw, percent-encoded `source_page_url` (e.g. `%C3%85lesund`
   for Ålesund), which false-positived on every non-ASCII city name.
   Fixed by `urllib.parse.unquote`-decoding the URL before the check.
4. **Confusable place names / common words**, found by a further manual
   visual sample scan *after* the above fixes were in place — these are
   genuine wrong-city selections, not text-normalization bugs:
   - **Split → a Nissan Skyline GT-R photo** titled "...SplitFire GTR...".
     A naive substring check matched "split" *inside* "splitfire" (and the
     candidate had also scored artificially well because the scoring
     function's own "skyline" keyword bonus coincidentally matches the car
     model name "Nissan Skyline"). Root-caused as a **substring-vs-word-
     boundary** bug, not fixable by a phrase list alone — `title_mentions_place`
     was rewritten to require a regex word-boundary match
     (`\bword\b`) for both the primary-name/alias check and the
     stopword-filtered fallback check.
   - **Porto → "Porto Alegre" (Brazil)**, **Faro → "El Faro de Córdoba"**
     (Spanish/Portuguese for "lighthouse", not the city), **Rome → "Rome,
     Georgia" (USA)**, **Bern → "New Bern" (North Carolina, USA)**, **Nice
     → "a nice view of the Chicago skyline"** (the English adjective, not
     the city). Fixed with an explicit `_CONFUSABLE_DISQUALIFIERS` table —
     phrases that, if present, disqualify an otherwise-matching title
     (checked *before* the positive match).
   - Building the word-boundary fix surfaced one more normalization bug:
     Commons filenames use underscores as word separators
     (`Split_old_town_Diocletian_palace.jpg`), and regex `\b` does **not**
     treat `_` as a boundary character. Fixed by normalizing
     `title.lower().replace("_", " ")` before matching (and simplifying the
     disqualifier phrases to match, since comma-containing phrases like
     `"rome, ga"` no longer appear once commas are also absent from
     filenames).
5. **Stale entries from before the word-boundary fix.** Re-running the
   offline audit with the final matching logic surfaced 6 destinations
   acquired *before* the word-boundary rewrite and never refreshed: Bremen,
   Erfurt, Kavala, Lisbon, Stuttgart, Turku. (Bremen's flagged title,
   `LandgerichtBremen.jpg`, turned out to be a genuine Commons filename with
   no separator at all between the two words — confirmed directly against
   the API — so this one is a true positive for "the old logic couldn't
   have matched it," not a data error.) All 6 were refreshed individually
   and re-verified clean.

After all of the above, the offline audit (`audit_manifest`) reports **zero**
`destination_mismatch_suspected` issues across the library.

6. **Textually-correct but visually-wrong: Turku.** An independent
   adversarial QA pass (run specifically to press on this bug class again,
   after the fixes above) found `title_mentions_place` still isn't a
   content check — it only proves a place name appears in the title, never
   that the photo depicts that place. `Turku`'s selected candidate was
   titled `Short_Skyvan_(OH-SBA)_Turku_Airshow_2015_01.JPG`: "Turku"
   genuinely appears (correctly passing the guard), but the photo is of an
   aircraft parked on a tarmac at an airshow, not the city. Fixed by adding
   an explicit off-topic-subject blocklist (`airshow`, `aircraft`,
   `airliner`, `fighter jet`, `warbird`, `warplane`, `biplane`) to
   `_TITLE_BLOCKLIST` — the same mechanism already used to reject
   maps/flags/logos/stamps — and re-acquiring Turku, which now selects a
   genuine Aurajoki riverfront photo. This is a narrower, targeted fix, not
   a claim that every possible content-vs-title mismatch is now closed;
   see "Known limitations."

## License and attribution re-validated after acquisition, not just trusted from it

The same adversarial QA pass constructed a hand-edited `DestinationImage`
record with a fabricated license string (`"All Rights Reserved"`) and found
it was accepted as `production_eligible` — the accepted-license allowlist
(`license_is_accepted`) was previously enforced only inside the acquisition
pipeline, never re-checked afterward. Fixed by moving the allowlist into
the domain model (`models/destination_image.py`) so
`DestinationImage.has_understood_license` itself calls `license_is_accepted`
— a record's license is only "understood" if it is also on the accepted
list, not merely present — and by adding the identical check to
`quality_checks.audit_manifest` (`unaccepted_license`) as a second,
independent line of defense against a hand-edited or future-buggy manifest.

The same pass also found a live acquisition-time gap this closed a
different way: `Eindhoven`'s selected candidate required attribution but
Commons had no creator/credit metadata to build that attribution text from,
so the record was `SELECTED` with `attribution_text=None` — caught by
`production_eligible`'s existing rule (so it never reached the API) but
left a semantically wrong `SELECTED` status sitting in the manifest.
`pipeline.py`'s acquisition loop now refuses to select any candidate that
requires attribution but has no creator/credit text to build it from,
trying the next candidate instead — re-running acquisition for Eindhoven
with this fix correctly reported no eligible candidate at all, and the
stale record was manually corrected to `REVIEW_REQUIRED`.

**Known, accepted gap (manifest tamper resistance):** the same QA pass also
constructed a *self-consistent* tampered record — a valid, accepted license
and a `local_asset_path`/`sha256` pair copied from a *different* real,
already-used image — and confirmed `audit_manifest` does not catch it
unless the original record still exists elsewhere in the same manifest (the
cross-manifest `duplicate_image_reused` check only fires by coincidence in
that case). This is a real limitation, not fixed this phase: sha256
checks prove a file matches what its own manifest entry claims, never that
the claim was true in the first place, and nothing in this pipeline
re-verifies a record against its original source after acquisition. Closing
this would need either a periodic re-fetch-and-compare against
`original_image_url` (network-dependent, contradicting the "no network
during a dry-run/CI audit" requirement) or a separate integrity-signing
step — out of scope for this phase. `manifest.json` should be treated as a
build-time artifact produced only by this codebase's own tooling, not as
safe to hand-edit.

## Full-library visual audit (the most significant finding this phase)

Every fix above was reactive — found one destination at a time, either by a
manual spot-check or a QA agent's random sample, then patched narrowly.
After the fourth or fifth wrong-place discovery it was clear that pattern
could not, by itself, establish confidence in the other ~190 destinations
never individually looked at. Rather than declare the library production-
ready on the strength of spot-checks, every one of the 195 then-`SELECTED`
images was visually opened and judged against its claimed city (split
across five parallel review passes, ~39 destinations each, each pass primed
with the wrong-place cases already found so far as calibration examples but
required to independently judge every image's actual pixels rather than
re-trust `title_mentions_place` or any manifest text field).

**Result: 51 further destinations were wrong-place, wrong-subject, or
non-photographic — roughly one in four of the audited library**, on top of
the 4 already caught (Athens, Bremen, Eindhoven, Split). The dominant
failure modes, all variations on "the text matched, the picture didn't":

* **A person's surname coincides with a city name.** *Brest* selected an
  Orientalist painting by the painter Fabius Brest (depicting Istanbul);
  *Shannon* selected a Doha, Qatar skyline credited "photo by Shannon."
* **A product/plant/vehicle model shares the word "skyline."** *Stockholm*
  and *Toulouse* both selected macro photos of garden plant cultivars named
  "Skyline Stockholm" / "Skyline" (Salvia); *Friedrichshafen* and *Graz*
  both selected car-show/bus photos where "Skyline"/"Skyliner" is a vehicle
  model name, not a city view.
* **A same-named place in a different country.** *Geneva* (Geneva, New
  York), *Genoa* (Columbus, Ohio's "Genoa Park"), *Naples* (Naples,
  Florida), *Palermo* (the Palermo neighbourhood of Buenos Aires), *Rhodes*
  (a Sydney, Australia suburb — an IKEA sign is visible), *Bergen* (Bergen
  County, New Jersey), *Birmingham* (Birmingham, Alabama — the "Regions"
  tower is a giveaway), *Southampton* (a Boston, MA street named
  "Southampton Street"), *Rennes* (Rochester, NY's "Pont de Rennes"
  bridge), *Trieste* (a Naples, Italy square named "Piazza Trieste e
  Trento"), *Asturias* (a Madrid street, "Avenida de Asturias"), *Tenerife*
  (Breña Alta, on the different Canary Island of La Palma).
* **Historical engravings, paintings, maps, and postcards mistaken for
  photographs** (a category §B1 explicitly excludes, but nothing before
  this phase checked for): Bari, Biarritz, Charleroi, Klagenfurt, Lille,
  Memmingen, Murcia, Perpignan, Preveza, Santander, Inverness (a blank
  antique postcard back).
* **Object/vehicle/interior close-ups and near-blank crops with no place
  content**: Amsterdam, Berlin (a basketball game), Cork, Leeds, Madrid,
  Marseille, Munich, Palanga, Paphos, Rovaniemi, San Sebastian, Timisoara,
  Vigo, Zadar, Chisinau, Karlsruhe (extreme color-cast), Strasbourg,
  Thessaloniki, Burgas, Clermont-Ferrand, Brindisi.

None of this was caught by `title_mentions_place` or the offline
`destination_mismatch_suspected` audit check, because both operate purely
on text: the city name genuinely does appear in the source title/URL in
every one of these cases (that's exactly why they were selected) — the
guard has no way to know a title's word is a painter's surname, a plant
cultivar, a different country's suburb, or that the file is a painting
rather than a photograph. Closing this properly would need real image
content understanding (a vision model in the pipeline, or exhaustive human
review of every candidate before selection), which is out of scope for
this phase. **All 51 were left in the manifest with `status` manually
changed to `REVIEW_REQUIRED`** (never deleted, never silently swapped for
another guess) and a `selection_notes` explaining the specific problem, the
same pattern already used for Athens/Bremen/Eindhoven/Split. Because
`production_eligible` is `False` for any non-`SELECTED` record, none of
these 55 total review-required images can reach `GET
/api/v1/destinations/images` or the frontend — verified by the API test
suite, not just asserted.

**Practical consequence**: the coverage this phase can honestly claim as
production-ready is far below the ~195/203 headline figure the automated
pipeline first produced — see "Coverage / quality reporting" for the exact
final numbers. This is the correct outcome per §B9 ("do not silently
substitute a poor or legally unclear image just to reach 100%... for
unresolved cities, report them explicitly"), even though it means most of
the catalog needs a further, human-driven acquisition/review pass before
this library is fully populated. The review gallery
(`data/destination_images/reports/review_gallery.html`) lists all 55 with
their notes as the starting point for that pass.

## Image quality / art direction

Query templates deliberately favour place-establishing shots over a single
monument close-up: `"{city} skyline"`, `"cityscape"`, `"old town"`,
`"aerial view"`, `"panorama"`, `"historic centre"`, falling back to
`"{city}, {country}"`. A title/filename blocklist rejects maps, flags,
coats of arms, logos, diagrams, stamps, portraits and postcards before a
candidate is ever downloaded. `score_candidate` is a small, interpretable,
documented formula — resolution (diminishing returns), a landscape-ish
aspect ratio bonus, and a keyword bonus for skyline/aerial/panorama/harbour
terms — never an opaque model.

**This is automated shortlisting, not hand-curated art direction.**
Aesthetic judgment is explicitly subjective (§B2's own framing) — the
pipeline's job is to clear a quality/license bar and produce a
human-reviewable shortlist, not to replace a person's eye. See "Human
review" below for how a person adjusts a selection afterward.

## Optimizer

`services/destination_images/optimizer.py` (Pillow, a new optional
dependency — see `pyproject.toml`'s `images` extra, never imported by the
running application). Decodes and fully loads the image (a truncated file
raises here), rejects anything under 1200×675, crops to exactly 16:9
(biased slightly upward-of-centre — a plain centre crop routinely beheads a
skyline), downsizes to 1600px wide when larger, re-encodes as WebP (quality
82). Every optimized asset therefore has an *identical* aspect ratio,
letting a frontend rely on it without per-image special-casing. A
`sha256` is computed on both the original download and the optimized
output for provenance and cross-destination duplicate detection.

## Manifest

`data/destination_images/manifest.json` — one JSON file, keys sorted,
committed to the repository (not a database table — this is a build-time
asset artifact). `services/destination_images/manifest.py::save_manifest`
is deterministic: an unchanged re-run produces a byte-identical file.

## Credit / attribution

`services/destination_images/credit.py::render_credit` renders whatever a
given image's license actually requires — a CC0 image needs no credit
line; a CC BY-SA one needs creator + license + link — rather than one
hardcoded template for every license.

## Pipeline resumability

`scripts/acquire_destination_images.py` skips any destination already
`SELECTED` in the manifest unless `--refresh <id>` / `--refresh-all` is
given, checkpoints the manifest every 10 destinations (safe to interrupt),
and `--dry-run` performs the entire discovery/scoring pass with **zero**
downloads (proven offline in the test suite, not just by convention).

## Data quality / security boundary

* Response size capped (`MAX_DOWNLOAD_BYTES` = 30 MB) before any bytes are
  even fully read from the socket.
* Corrupt/truncated/zero-byte files, too-small images, and unexpected
  aspect-ratio output are all rejected outright — never silently degraded.
* Exact-duplicate detection by SHA-256 prevents two different destinations
  from silently sharing one photo.
* No traveler/account PII is ever sent to or read from an image source.
* `services/destination_images/quality_checks.py::audit_manifest` runs the
  same checks offline, after acquisition, against the manifest + files on
  disk — usable as a standalone CI gate with zero network.

**This is engineering provenance infrastructure, not a legal conclusion.**
`DestinationImage.production_eligible` is
`PRODUCTION_ELIGIBLE_BY_CONFIGURED_POLICY` — the pipeline's own policy
considers the recorded metadata sufficient (a status of `SELECTED`, an
optimized asset on disk, an understood license, and attribution text
whenever the license requires it). Nothing in this codebase claims
`LEGAL_APPROVED`, and none of this software work substitutes for a
separate legal review before any of these images are actually shipped to
consumers.

## Frontend integration seam

`GET /api/v1/destinations/images` — the full manifest of
production-eligible images, keyed by `destination_id`. `GET
/api/v1/destinations/{destination_id}/image` — one destination, `null` if
none. Both are entirely new, additive routes — no existing response model
changed. Each entry exposes exactly `primary` (served asset URL),
`credit`, `license_name`/`license_url`/`requires_attribution`,
`aspect_ratio`, `focal_point`, `width`/`height` — the seam described in
§B11, deliberately not tied to any one screen (search cards, journey
cards, city headers, My Trips, a PDF guide, share cards are all future,
unbuilt consumers of the same manifest).

## Human review

`scripts/generate_image_review_gallery.py` produces a static, dark-themed
HTML contact sheet (`data/destination_images/reports/review_gallery.html`,
gitignored — regenerable, not a permanent artifact) showing every
destination's thumbnail, status, creator, license, source link and local
path in one scrollable page. This is a review tool for a human to scan and
adjust selections; it is not, and must never be mistaken for, a Detoura
consumer page.

## Coverage / quality reporting

`scripts/report_image_coverage.py` — zero network, reports exactly the
§B9 numbers (catalog total, image found, production-license-eligible,
missing, review-required) plus the §B10 quality audit and total optimized
byte size, against the live catalog every time (never a hardcoded 203).

## Known limitations

* Selection is automated (license + quality-heuristic shortlisting), not
  individually hand-reviewed for artistic merit or subject correctness per
  city — see "Full-library visual audit" above for the major finding this
  phase: after every `SELECTED` image was manually opened and judged, 55 of
  199 (Athens, Bremen, Eindhoven, Split, plus 51 more) turned out to be
  wrong-place, wrong-subject, non-photographic, or content-free, and were
  manually set to `REVIEW_REQUIRED` with a `selection_notes` explanation for
  each. In every case the record was left in the manifest (never deleted)
  with an honest status and a human-readable reason, so the review gallery
  and the coverage report both surface it instead of hiding it — none of
  the 55 can reach the public API (`production_eligible` is `False` for any
  non-`SELECTED` record). This means a genuinely production-ready,
  hand-verified library exists today only for the 144 destinations still
  `SELECTED`; the other 55 need a further human-driven acquisition/review
  pass (the review gallery is the starting point for that work), and the
  automated pipeline's own scoring/relevance heuristics should not be
  trusted to self-certify a future destination as correct without at least
  a quick human glance at the actual pixels — text-only checks
  (`title_mentions_place`, the license allowlist, resolution/aspect
  validation) have now demonstrably let a person's surname, a plant
  cultivar, a same-named city in another country, and a painting/engraving
  each pass every automated gate at least once.
* A handful of destinations may have no image at all if no Commons
  candidate cleared the license/quality bar — reported explicitly as
  `missing`, never silently substituted with a lower-quality or
  legally-unclear image.
* Focal point is a fixed default (`0.5, 0.42`) for every automatically
  selected image, not per-image human-tuned — the field exists and is
  wired through the whole model/manifest/API for a future manual pass to
  fill in.
* No other source category (§B3's tourism-board/licensed-API/permitted-
  dataset options) was evaluated this phase — Commons alone was sufficient
  to reach the coverage reported in the release-gate report.
