"""Password hashing (V9 Phase 2.6 §A2).

Argon2id via ``argon2-cffi`` — the OWASP-recommended default, the Password
Hashing Competition winner, and a library, not hand-rolled cryptography.
Every per-password salt and the KDF parameters live inside the encoded hash
string the library produces; this module never manages a salt itself.

**Defends against pathological input.** A password is capped in *bytes*
(UTF-8 can expand a short string a lot) before it ever reaches the KDF —
Argon2's cost is a function of input size as well as its own work factor, so
an unbounded password is a cheap way to burn CPU on the server. The cap is
generous for any real password and rejects only abuse.

**Fails closed.** Any malformed/foreign hash, or a verify against an
oversized password, returns ``False`` — never raises past this module for a
caller to forget to catch.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerificationError

#: Bytes, not characters - UTF-8 can be up to 4 bytes/char. Generous for any
#: real password (a 1000-character passphrase is still under this), and a
#: hard stop against a multi-megabyte "password" used to burn CPU.
MAX_PASSWORD_BYTES = 256
MIN_PASSWORD_LENGTH = 8

_hasher = PasswordHasher()  # argon2-cffi's default type is Argon2id


class PasswordPolicyError(ValueError):
    """The password itself is unacceptable - too short or too long. Never
    raised from :func:`verify_password`, which fails closed instead."""


def _encoded_length(password: str) -> int:
    return len(password.encode("utf-8", errors="ignore"))


def hash_password(password: str) -> str:
    """Raises :class:`PasswordPolicyError` for a password outside policy;
    otherwise returns an Argon2id-encoded hash string safe to store as-is."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if _encoded_length(password) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    """``True`` iff ``password`` matches ``stored_hash``. Never raises: a
    malformed hash, a library error, or an oversized password all verify as
    ``False`` rather than propagating (fail closed, §A2, §A4)."""
    if not stored_hash or _encoded_length(password) > MAX_PASSWORD_BYTES:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError, HashingError):
        return False
    except Exception:  # noqa: BLE001 - a hash from a future/foreign format must not crash login
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Whether the hash was produced with older parameters than this
    module's current ``PasswordHasher`` — a caller may re-hash on next
    successful login. Never raises; an unparseable hash reads as "yes"."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except Exception:  # noqa: BLE001
        return True
