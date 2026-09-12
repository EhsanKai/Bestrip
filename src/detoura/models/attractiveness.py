"""Destination Attractiveness domain (V9 Phase 3 §B).

Answers **"how compelling is this destination as a travel experience?"** —
never "how cheap is it right now?". Price lives entirely in Price Memory /
Market Prior / live offers (:mod:`detoura.models.search_intel`,
:mod:`detoura.models.market_prior`); nothing in this module has a monetary
field, and nothing here may be blended into a customer-facing price.

**Not popularity.** A :class:`DestinationAttractivenessProfile` is never
derived from visitor counts, social-media mentions, airport traffic or hotel
volume (§B3) — those measure fame, not experience quality, and the whole
point of this system is that a quiet, excellent destination should be able to
outscore a famous, over-exposed one on the dimensions that matter for a short
Detoura trip.

**Global vs. user-specific (§B5).** A profile here is one destination's
*global* attractiveness — the same number for every traveler. A traveler's
personal fit against their own stated preferences
(:func:`detoura.services.acquisition.preference_affinity`) is a completely
separate signal, computed fresh per search, and the two are never merged
into a single stored number. Both survive independently into the
explainability trace (§D2) so a ranking can always be explained as
"attractive to everyone" vs. "attractive to *this* traveler".

**Honesty about the data source (§B2).** No authorized external
tourism-quality dataset was integrated this phase. Every profile's
``provenance`` says exactly where its numbers came from:

* ``CURATED``   — a human-authored ``Destination`` attribute profile (the 16
                  hand-tuned core cities in ``data/destinations.py``, each
                  attribute individually chosen to make cities genuinely
                  different from one another).
* ``DERIVED``   — mechanically computed from a destination's broad, truthful
                  ``tags`` via ``profile_from_tags`` (the ~187-city discovery
                  catalog in ``data/european_catalog.py``, V9 Phase 2 §17).
* ``MEASURED``  — reserved for a genuine external data source, not used this
                  phase. A future integration would report real provenance
                  here, never silently reuse ``CURATED``/``DERIVED``.
* ``UNKNOWN``   — no basis exists at all; every score is ``None`` and
                  ``confidence`` is ``NONE``. Never defaulted to a "neutral"
                  50 that would read as real data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

#: The current attractiveness model. Bump this — never mutate the meaning of
#: an existing version — whenever the derivation formula changes; old rows at
#: earlier versions remain in the table, untouched and separately queryable
#: (see persistence/attractiveness.py's versioned primary key).
CURRENT_MODEL_VERSION = 1


class AttractivenessProvenance(str, Enum):
    MEASURED = "MEASURED"
    """From a genuine, authorized external data source. Not used this phase."""
    CURATED = "CURATED"
    """Human-authored per-destination attribute profile."""
    DERIVED = "DERIVED"
    """Mechanically computed from broad, truthful tags."""
    UNKNOWN = "UNKNOWN"
    """No basis exists. Every score is ``None``."""


class AttractivenessConfidence(str, Enum):
    """Interpretable, not an ML score — mirrors the Phase 1/2
    :class:`~detoura.models.search_intel.MarketConfidence` /
    :class:`~detoura.models.market_prior.PriorConfidence` shape so Ops reads
    one consistent vocabulary across every V9 intelligence layer."""

    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


#: The dimension field names, in a stable order — used wherever a caller
#: needs "all the dimensions" without hand-listing them (the experience
#: feature vector, Ops serialization, tests).
DIMENSION_FIELDS: tuple[str, ...] = (
    "sightseeing_score",
    "culture_score",
    "food_score",
    "nightlife_score",
    "nature_score",
    "uniqueness_score",
    "short_trip_score",
    "experience_density_score",
)


class DestinationAttractivenessProfile(BaseModel):
    """One destination's global, provider-independent travel-experience
    profile, at one model version.

    Every dimension is ``None`` (UNKNOWN) rather than a fabricated neutral
    value when there is genuinely no basis for it (§B2) — a caller that reads
    ``None`` as 0.5 is choosing to be neutral, not being told the destination
    scored average. :attr:`aggregate_score` is likewise ``None`` when too few
    dimensions are known to average meaningfully.
    """

    model_config = ConfigDict(frozen=True)

    destination_id: str = Field(min_length=1, max_length=200)
    model_version: int = Field(ge=1)

    # --- dimensions, each in [0, 100], None = UNKNOWN --------------------
    sightseeing_score: float | None = Field(default=None, ge=0.0, le=100.0)
    culture_score: float | None = Field(default=None, ge=0.0, le=100.0)
    food_score: float | None = Field(default=None, ge=0.0, le=100.0)
    nightlife_score: float | None = Field(default=None, ge=0.0, le=100.0)
    nature_score: float | None = Field(default=None, ge=0.0, le=100.0)
    uniqueness_score: float | None = Field(default=None, ge=0.0, le=100.0)
    """How distinctive this destination is relative to the rest of the
    catalog — deliberately NOT a popularity proxy (§B3): a small, unusual city
    with a sharply different tag/attribute profile from its neighbours scores
    high here even with modest visitor numbers, and a famous but
    generic-profile city does not automatically score high just for being
    famous."""
    short_trip_score: float | None = Field(default=None, ge=0.0, le=100.0)
    """How well this destination suits a 1-2 day Detoura trip specifically
    (§B4) — not a discount for being "merely a weekend city". Derived from
    the catalog's own ``recommended_min_days``/``recommended_max_days``, never
    penalising a genuinely excellent short-trip destination for lacking
    week-long depth."""
    experience_density_score: float | None = Field(default=None, ge=0.0, le=100.0)
    """How much there is to do without needing to leave the city — the
    catalog's own multi-attribute richness, rescaled."""

    aggregate_score: float | None = Field(default=None, ge=0.0, le=100.0)
    """The single interpretable summary (mean of the known dimensions) for
    ranking. Component scores are always preserved alongside it (§D) — no
    caller has to trust an unexplained aggregate."""

    confidence: AttractivenessConfidence = AttractivenessConfidence.NONE
    provenance: AttractivenessProvenance = AttractivenessProvenance.UNKNOWN
    source: str = Field(default="", max_length=120)
    """Free-text: which catalog attribute profile or tag set produced this
    row, e.g. ``"catalog:CORE_DESTINATIONS"`` or
    ``"catalog:profile_from_tags"``. Never a claim of an external dataset
    unless ``provenance`` is ``MEASURED``."""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_known(self) -> bool:
        return self.aggregate_score is not None

    def dimension_vector(self) -> dict[str, float | None]:
        """``{dimension_name: score_or_None}`` in :data:`DIMENSION_FIELDS`
        order — the feature vector experience-similarity scoring reads."""
        return {name: getattr(self, name) for name in DIMENSION_FIELDS}


def unknown_profile(destination_id: str, *, model_version: int = CURRENT_MODEL_VERSION) -> DestinationAttractivenessProfile:
    """The explicit "we have nothing" profile — never fabricated data, and
    never silently substituted for a missing catalog entry elsewhere."""
    return DestinationAttractivenessProfile(
        destination_id=destination_id, model_version=model_version,
        confidence=AttractivenessConfidence.NONE,
        provenance=AttractivenessProvenance.UNKNOWN, source="",
    )
