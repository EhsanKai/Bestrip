"""Persistence for accounts, sessions and trip ownership (V9 Phase 2.6 §A1, §A5, §A8).

Three tables: ``user_accounts``, ``auth_sessions``, ``trip_ownership``. None
of this joins into ``bookings``' passenger/traveler fields, and nothing here
is reachable from the search or booking path except through the explicit
``claim_trip`` call a caller makes after a booking exists — ownership is
additive metadata, never a precondition booking creation depends on.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Sequence

from .db import Database


class DuplicateEmail(Exception):
    """The normalized email is already registered. Never includes the email
    itself in the message that might get logged verbatim by a generic error
    handler beyond what the caller already knows."""


def new_user_id() -> str:
    return "usr_" + secrets.token_urlsafe(16)


def new_session_id() -> str:
    return "sess_" + secrets.token_urlsafe(12)


def new_session_token() -> str:
    """The raw, opaque value that goes in the cookie. Cryptographically
    random, never derived from anything guessable (§A5)."""
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def hash_token(token: str) -> str:
    """SHA-256 of an opaque random token is an appropriate "hash at rest"
    here — unlike a password, a session/CSRF token already has full
    cryptographic entropy, so there is no offline-guessing risk a slow KDF
    would defend against; the only property needed is that stealing the
    database does not hand out live session tokens directly (§A5)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ======================================================================
# Accounts
# ======================================================================
def create_user(
    db: Database, *, email_normalized: str, password_hash: str, now: datetime | None = None,
) -> str:
    """Raises :class:`DuplicateEmail` if the normalized email is already
    registered. Returns the new ``user_id``."""
    now = now or datetime.now(timezone.utc)
    ts = now.isoformat()
    user_id = new_user_id()
    try:
        with db.write() as conn:
            conn.execute(
                "INSERT INTO user_accounts (user_id, email_normalized, password_hash,"
                " status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (user_id, email_normalized, password_hash, "ACTIVE", ts, ts),
            )
    except Exception as exc:  # sqlite3.IntegrityError on the UNIQUE constraint
        if "UNIQUE" in str(exc).upper():
            raise DuplicateEmail("an account with this email already exists") from exc
        raise
    return user_id


def get_user(db: Database, user_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM user_accounts WHERE user_id=?", (user_id,))
    return dict(row) if row else None


def get_user_by_email(db: Database, email_normalized: str) -> dict | None:
    row = db.query_one("SELECT * FROM user_accounts WHERE email_normalized=?", (email_normalized,))
    return dict(row) if row else None


def set_last_login(db: Database, user_id: str, *, now: datetime | None = None) -> None:
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        conn.execute(
            "UPDATE user_accounts SET last_login_at=?, updated_at=? WHERE user_id=?",
            (ts, ts, user_id),
        )


def set_password_hash(db: Database, user_id: str, password_hash: str, *, now: datetime | None = None) -> None:
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        conn.execute(
            "UPDATE user_accounts SET password_hash=?, updated_at=? WHERE user_id=?",
            (password_hash, ts, user_id),
        )


def set_status(db: Database, user_id: str, status: str, *, now: datetime | None = None) -> None:
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        conn.execute(
            "UPDATE user_accounts SET status=?, updated_at=? WHERE user_id=?",
            (status, ts, user_id),
        )


# ======================================================================
# Sessions
# ======================================================================
def create_session(
    db: Database, *, user_id: str, token_hash: str, csrf_token_hash: str,
    ttl_seconds: int, now: datetime | None = None,
) -> str:
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    session_id = new_session_id()
    expires_at = now + timedelta(seconds=ttl_seconds)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (session_id, user_id, token_hash, csrf_token_hash,"
            " created_at, expires_at) VALUES (?,?,?,?,?,?)",
            (session_id, user_id, token_hash, csrf_token_hash, now.isoformat(), expires_at.isoformat()),
        )
    return session_id


def get_session_by_token_hash(db: Database, token_hash: str) -> dict | None:
    row = db.query_one("SELECT * FROM auth_sessions WHERE token_hash=?", (token_hash,))
    return dict(row) if row else None


def touch_session(db: Database, session_id: str, *, now: datetime | None = None) -> None:
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        conn.execute("UPDATE auth_sessions SET last_seen_at=? WHERE session_id=?", (ts, session_id))


def revoke_session(db: Database, session_id: str, *, now: datetime | None = None) -> bool:
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
            (ts, session_id),
        )
        return cur.rowcount == 1


def revoke_all_sessions_for_user(db: Database, user_id: str, *, now: datetime | None = None) -> int:
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (ts, user_id),
        )
        return cur.rowcount


# ======================================================================
# Trip ownership
# ======================================================================
def claim_trip(
    db: Database, *, user_id: str, booking_id: str, journey_reference: str = "",
    now: datetime | None = None,
) -> bool:
    """Associates ``booking_id`` with ``user_id``. Idempotent for the same
    user; returns ``False`` (never overwrites) if the booking is already
    claimed by a *different* user — ownership, once established, does not
    silently transfer.

    The existence check and the insert happen inside one ``db.write()``
    transaction (V9 Phase 6) - not read-then-separately-write. The two
    previously ran as independent calls, each taking and releasing
    ``Database``'s lock on its own, leaving a gap between them where two
    concurrent callers could both observe "unclaimed" and both proceed to
    insert; the second would then hit ``trip_ownership``'s own
    ``booking_id`` primary key and raise a raw ``sqlite3.IntegrityError``
    instead of returning a clean ``False``/idempotent ``True``. One
    transaction closes that window: ``db.write()`` holds the same lock for
    the whole read-decide-write sequence, so a second caller simply waits
    for the first to finish and then sees its result already committed."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    with db.write() as conn:
        existing = conn.execute(
            "SELECT user_id FROM trip_ownership WHERE booking_id=?", (booking_id,)
        ).fetchone()
        if existing is not None:
            return existing["user_id"] == user_id
        conn.execute(
            "INSERT INTO trip_ownership (booking_id, user_id, journey_reference, claimed_at)"
            " VALUES (?,?,?,?)",
            (booking_id, user_id, journey_reference, ts),
        )
    return True


def get_trip_owner(db: Database, booking_id: str) -> str | None:
    row = db.query_one("SELECT user_id FROM trip_ownership WHERE booking_id=?", (booking_id,))
    return row["user_id"] if row else None


def list_trip_ids_for_user(db: Database, user_id: str) -> Sequence[str]:
    rows = db.query(
        "SELECT booking_id FROM trip_ownership WHERE user_id=? ORDER BY claimed_at DESC", (user_id,),
    )
    return [r["booking_id"] for r in rows]
