"""The person taking the journey, entered once (V8 Phase 4).

Provider-neutral by construction. A `Traveler` here is not a Duffel passenger
and not an Amadeus traveller - it is what Detoura asks for, once, and reuses
server-side for every underlying booking. The mapping to a provider's passenger
object happens at the provider boundary and nowhere else.

Two rules this module holds:

**Collect only what the booked flow needs.** First/last name, date of birth,
email and phone are always required to issue a ticket. Gender, nationality and
travel-document detail are collected *only* when a provider flow actually
demands them, so the fields exist but default to absent and a caller decides.
Over-collecting passport data "in case" is how a demo becomes a breach.

**Every field is classified.** `SENSITIVITY` maps each field to a data class,
and the API layer uses it to keep PERSONAL/SENSITIVE values out of logs,
analytics, URLs and provider metrics.
"""

from __future__ import annotations

import re
from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DataSensitivity(str, Enum):
    """How carefully one field has to be handled."""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    PERSONAL = "PERSONAL"
    """Identifies a person: name, email, phone. No logs, no analytics, no URLs."""
    SENSITIVE = "SENSITIVE"
    """Protected-category or document data: DOB, nationality, passport. As
    PERSONAL, plus: never in provider metrics, minimised, retained only as long
    as the booking needs it."""
    SECRET = "SECRET"
    """Credentials. None on this model - travellers do not carry secrets."""


#: field name -> data class. The API redaction layer reads this.
SENSITIVITY: dict[str, DataSensitivity] = {
    "given_name": DataSensitivity.PERSONAL,
    "family_name": DataSensitivity.PERSONAL,
    "email": DataSensitivity.PERSONAL,
    "phone": DataSensitivity.PERSONAL,
    "born_on": DataSensitivity.SENSITIVE,
    "gender": DataSensitivity.SENSITIVE,
    "title": DataSensitivity.PERSONAL,
    "nationality": DataSensitivity.SENSITIVE,
    "passport_number": DataSensitivity.SENSITIVE,
    "passport_expiry": DataSensitivity.SENSITIVE,
}

_NAME_RE = re.compile(r"^[\w' .\-]{1,60}$", re.UNICODE)
_PHONE_RE = re.compile(r"^\+?[0-9 .\-()]{6,20}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_IATA2_RE = re.compile(r"^[A-Za-z]{2}$")


class TravelerTitle(str, Enum):
    MR = "mr"
    MS = "ms"
    MRS = "mrs"
    MISS = "miss"
    DR = "dr"


class TravelerGender(str, Enum):
    MALE = "m"
    FEMALE = "f"
    X = "x"


class Traveler(BaseModel):
    """One person on the journey. Frozen once accepted."""

    model_config = ConfigDict(frozen=True)

    given_name: str = Field(min_length=1, max_length=60)
    family_name: str = Field(min_length=1, max_length=60)
    born_on: date
    email: str = Field(max_length=120)
    phone: str = Field(max_length=20)

    # Collected only when the tested provider flow needs them.
    title: TravelerTitle | None = None
    gender: TravelerGender | None = None
    nationality: str | None = Field(default=None, description="ISO 3166-1 alpha-2")
    passport_number: str | None = Field(default=None, max_length=20)
    passport_expiry: date | None = None

    @field_validator("given_name", "family_name")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        value = value.strip()
        if not _NAME_RE.match(value):
            raise ValueError("name contains characters we cannot put on a ticket")
        return value

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str) -> str:
        value = value.strip()
        if not _EMAIL_RE.match(value):
            raise ValueError("that does not look like an email address")
        return value

    @field_validator("phone")
    @classmethod
    def _phone_shape(cls, value: str) -> str:
        value = value.strip()
        if not _PHONE_RE.match(value):
            raise ValueError("that does not look like a phone number")
        return value

    @field_validator("nationality")
    @classmethod
    def _nationality_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if not _IATA2_RE.match(value):
            raise ValueError("nationality must be a two-letter country code")
        return value

    @field_validator("born_on", "passport_expiry")
    @classmethod
    def _plausible_date(cls, value: date | None) -> date | None:
        if value is None:
            return None
        if value.year < 1900 or value.year > 2100:
            raise ValueError("that date is not plausible")
        return value

    @property
    def full_name(self) -> str:
        return f"{self.given_name} {self.family_name}"

    def public_summary(self) -> dict[str, str]:
        """Only what a pass or a progress screen may show. No DOB, no document."""
        return {"name": self.full_name}


class TravelerParty(BaseModel):
    """Everyone on one journey. Size is fixed by the selected trip."""

    model_config = ConfigDict(frozen=True)

    travelers: tuple[Traveler, ...] = Field(min_length=1, max_length=9)

    @property
    def size(self) -> int:
        return len(self.travelers)

    @property
    def lead(self) -> Traveler:
        return self.travelers[0]


def requires_documents(*, international: bool) -> tuple[str, ...]:
    """Which extra fields a flow needs. Kept tiny and explicit.

    Duffel Test Mode issues an Order from name + DOB + gender + contact; it does
    not require passport data for the sandbox flows this build exercises. So the
    only conditional field is ``gender``, and passport detail stays off unless a
    future real-provider flow proves it necessary.
    """
    return ("gender", "title") if not international else ("gender", "title", "nationality")
