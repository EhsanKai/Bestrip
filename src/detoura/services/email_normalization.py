"""Deterministic email normalization (V9 Phase 2.6 §A3).

Minimal and reversible in spirit: an operator reading ``email_normalized``
can always tell what the user actually typed, modulo whitespace and domain
case. Deliberately does **not** invent provider-specific rewriting —
Gmail's dot-insensitivity and plus-addressing are real behaviours of one
mail provider, not a property of email addresses in general, and folding
them in would silently merge addresses a user may consider distinct at
providers that treat them literally.

Two addresses normalize equally iff they are equal after:

* trimming surrounding whitespace,
* lower-casing the domain (domains are case-insensitive per RFC 1035;
  local parts are technically case-sensitive per RFC 5321, but in practice
  every real mail provider treats them case-insensitively, and lower-casing
  both here is what makes "duplicate account" detection actually work
  rather than depending on how a user happened to type their own address).
"""

from __future__ import annotations

import re

#: Deliberately permissive - this is normalization + a sanity check, not a
#: full RFC 5322 validator. A borderline-valid address a mail provider would
#: accept should not be rejected here; obvious garbage should be.
_EMAIL_SHAPE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

MAX_EMAIL_LENGTH = 320  # RFC 5321 practical ceiling (local 64 + '@' + domain 255)


class InvalidEmail(ValueError):
    pass


def normalize_email(raw: str) -> str:
    """Raises :class:`InvalidEmail` for something that is not shaped like an
    email address; otherwise returns the deterministic normalized form."""
    candidate = (raw or "").strip()
    if not candidate or len(candidate) > MAX_EMAIL_LENGTH:
        raise InvalidEmail("email is empty or too long")
    if not _EMAIL_SHAPE.match(candidate):
        raise InvalidEmail("not a valid email address")
    local, _, domain = candidate.rpartition("@")
    return f"{local.lower()}@{domain.lower()}"
