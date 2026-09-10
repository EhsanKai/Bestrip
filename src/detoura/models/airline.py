"""Centralized airline metadata (V8.5 C3).

An airline is more than a two-letter code, and provider data keeps *two*
carriers apart on every segment: the **marketing** carrier (whose code is on
the ticket) and the **operating** carrier (whose aircraft you actually board).
They are frequently the same and frequently not, and merging them loses a fact
travellers and analytics both need.

This module holds:

* :class:`AirlineMetadata` - one carrier's identity, provider-neutral.
* :class:`SegmentCarriers` - the marketing/operating pair for one flight.
* :func:`airline_for` - a small catalogue keyed by IATA code, so the frontend
  never hardcodes an airline name or logo. Absent from the catalogue is not an
  error: a caller renders the code and a generic icon.

The catalogue is intentionally minimal - the carriers the synthetic supply and
the Duffel sandbox actually produce. A logo is referenced by a stable key, not
embedded; the frontend resolves the key (or falls back). A missing logo never
hides a ticket.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

#: A plausible IATA airline designator: two or three letters/digits. Anything
#: else (a synthetic label, a full name, empty) resolves to the unknown code.
_IATA_RE = re.compile(r"^[A-Z0-9]{2,3}$")


class AirlineMetadata(BaseModel):
    """One carrier's identity. Provider-neutral - assembled from whatever the
    provider supplied plus the local catalogue, with gaps left as ``None``/""
    rather than guessed."""

    model_config = ConfigDict(frozen=True)

    iata_code: str = Field(min_length=2, max_length=3)
    name: str = ""
    icao_code: str | None = None
    #: A stable key the frontend maps to a bundled asset. Never a URL - external
    #: images are blocked and a broken one must not break the ticket.
    logo_key: str = ""
    #: The provider's own raw identifier for this carrier, kept only when it is
    #: safe to surface (a public code, never a token).
    provider_ref: str | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.iata_code


class SegmentCarriers(BaseModel):
    """The marketing / operating carrier pair for one flight segment. Kept
    distinct - never silently merged."""

    model_config = ConfigDict(frozen=True)

    marketing: AirlineMetadata
    operating: AirlineMetadata | None = None
    marketing_flight_number: str = ""
    operating_flight_number: str = ""

    @property
    def codeshare(self) -> bool:
        return (
            self.operating is not None
            and self.operating.iata_code != self.marketing.iata_code
        )


# --- a tiny provider-neutral catalogue -------------------------------------
# IATA -> (name, icao, logo_key). Only carriers the synthetic catalogue and the
# Duffel sandbox actually return. Everything else resolves to a code + generic
# icon, which is a correct answer, not a failure.
_CATALOGUE: dict[str, tuple[str, str | None, str]] = {
    "LH": ("Lufthansa", "DLH", "lufthansa"),
    "BA": ("British Airways", "BAW", "british-airways"),
    "AF": ("Air France", "AFR", "air-france"),
    "KL": ("KLM", "KLM", "klm"),
    "IB": ("Iberia", "IBE", "iberia"),
    "VY": ("Vueling", "VLG", "vueling"),
    "FR": ("Ryanair", "RYR", "ryanair"),
    "U2": ("easyJet", "EZY", "easyjet"),
    "EW": ("Eurowings", "EWG", "eurowings"),
    "AZ": ("ITA Airways", "ITY", "ita-airways"),
    "OS": ("Austrian Airlines", "AUA", "austrian"),
    "LX": ("SWISS", "SWR", "swiss"),
    "SN": ("Brussels Airlines", "BEL", "brussels-airlines"),
    "SK": ("SAS", "SAS", "sas"),
    "TP": ("TAP Air Portugal", "TAP", "tap"),
    "A3": ("Aegean Airlines", "AEE", "aegean"),
    "DY": ("Norwegian", "NAX", "norwegian"),
    "W6": ("Wizz Air", "WZZ", "wizz-air"),
    "ZZ": ("Duffel Airways", "DFL", "duffel-airways"),  # Duffel sandbox carrier
}


def airline_for(iata_code: str | None, *, name: str | None = None,
                icao: str | None = None, provider_ref: str | None = None) -> AirlineMetadata:
    """Resolve a carrier. Provider-supplied ``name``/``icao`` win over the
    catalogue; the catalogue fills the gaps; an unknown code still yields a
    usable object (code as name, empty logo key)."""
    raw = (iata_code or "").strip().upper()
    code = raw if _IATA_RE.match(raw) else "??"
    cat = _CATALOGUE.get(code)
    cat_name, cat_icao, logo_key = cat if cat else ("", None, "")
    # A non-IATA carrier string (e.g. a synthetic label) is not a name either -
    # keep it out of display unless a real name was supplied.
    fallback_name = raw if (raw and code == "??" and " " in raw) else ""
    return AirlineMetadata(
        iata_code=code,
        name=(name or "").strip() or cat_name or fallback_name,
        icao_code=(icao or "").strip().upper() or cat_icao,
        logo_key=logo_key,
        provider_ref=provider_ref,
    )


def carriers_from_duffel_segment(segment: dict) -> SegmentCarriers:
    """Pull the marketing/operating pair out of one Duffel segment dict.

    Duffel gives ``marketing_carrier`` / ``operating_carrier`` objects (each
    with ``iata_code``, ``name``, ``icao_code``) and separate flight numbers.
    Operating is omitted when it equals marketing; we keep that as ``None``
    rather than duplicating.
    """
    mk = segment.get("marketing_carrier") or {}
    op = segment.get("operating_carrier") or {}
    marketing = airline_for(
        mk.get("iata_code"), name=mk.get("name"), icao=mk.get("icao_code"),
        provider_ref=mk.get("id"),
    )
    operating = None
    if op.get("iata_code") and op.get("iata_code") != mk.get("iata_code"):
        operating = airline_for(
            op.get("iata_code"), name=op.get("name"), icao=op.get("icao_code"),
            provider_ref=op.get("id"),
        )
    return SegmentCarriers(
        marketing=marketing,
        operating=operating,
        marketing_flight_number=str(
            segment.get("marketing_carrier_flight_number") or ""
        ),
        operating_flight_number=str(
            segment.get("operating_carrier_flight_number") or ""
        ),
    )
