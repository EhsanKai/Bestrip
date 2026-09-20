"""The account domain (V9 Phase 2.6 Part A).

A :class:`UserAccount` is deliberately thin and separate from
:class:`~detoura.models.booking.PassengerInfo`/Traveler concepts elsewhere in
the codebase: an account is *who is logged in*, a traveler is *who is
flying*. The person who books a trip is not necessarily one of the
passengers on it, and this module never assumes otherwise — nothing here
carries a date of birth, passport, phone, address or nationality. That data
belongs to booking-time traveler records, not the account.

**Never a plaintext password.** ``password_hash`` is always the output of
:mod:`detoura.services.password_hashing` (Argon2id) — this module holds no
password logic itself, so there is nowhere for a shortcut to creep in.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    #: Self-service account deletion (V9 Google auth + account lifecycle).
    #: Sessions/identities are gone and the email is scrubbed to a tombstone
    #: value, but the row itself is never deleted - trip_ownership and every
    #: financial table (checkout_snapshots, payment_transactions,
    #: financial_documents, ...) key on this user_id, and none of them are
    #: ever cascade-deleted (see docs/V9_GOOGLE_AUTH_ACCOUNT_LIFECYCLE_REPORT.md §13).
    DELETED = "DELETED"


class UserAccount(BaseModel):
    """A row of ``user_accounts``. Frozen — callers update by writing a new
    value through :mod:`detoura.persistence.accounts`, never by mutating a
    loaded instance."""

    model_config = ConfigDict(frozen=True)

    user_id: str = Field(min_length=1, max_length=64)
    email_normalized: str = Field(min_length=3, max_length=320)
    #: ``None`` for an account created entirely through Google Sign-In that
    #: has never gone through the password-reset "set a password" flow
    #: (V9 Google auth §11) - never an empty string, never a random hidden
    #: value standing in for "no password".
    password_hash: str | None = Field(default=None, max_length=1000)
    status: AccountStatus = AccountStatus.ACTIVE
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.status is AccountStatus.ACTIVE

    @property
    def has_password(self) -> bool:
        return self.password_hash is not None


class AuthIdentity(BaseModel):
    """A row of ``auth_identities`` (V9 Google auth): one external-provider
    identity bound to one Detoura account. Keyed on ``(provider,
    provider_subject)`` - the provider's own stable subject identifier,
    never on email (see the module-level linking-policy discussion in
    :mod:`detoura.services.google_auth_service`). No provider access/refresh
    token is ever carried here; Detoura only needs the one-time identity
    claim at login."""

    model_config = ConfigDict(frozen=True)

    identity_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=32)
    provider_subject: str = Field(min_length=1, max_length=255)
    provider_email: str = Field(default="", max_length=320)
    created_at: datetime
    updated_at: datetime


class SessionRecord(BaseModel):
    """A row of ``auth_sessions``, as returned to internal callers. The raw
    session token is never stored — only ``token_hash`` — and this model
    never carries the raw token either; it is generated once at login and
    handed straight to the caller for the cookie, never persisted or logged
    (V9 Phase 2.6 §A5, §A10)."""

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    last_seen_at: datetime | None = None

    def is_valid(self, *, now: datetime) -> bool:
        if self.revoked_at is not None:
            return False
        return now < self.expires_at


class TripOwnership(BaseModel):
    """Associates one ``booking_id`` with the account that made it. A
    booking with no row here is an anonymous/historical journey — that is a
    valid, permanent state, not a migration to backfill (V9 Phase 2.6 §A8)."""

    model_config = ConfigDict(frozen=True)

    booking_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=64)
    journey_reference: str = ""
    claimed_at: datetime
