"""The Detoura Journey Reference a traveller quotes back to us (V8 Phase 4).

`DTR-V8-7K4M2P`. Server-generated, always. It is not a Duffel order id, not a
ticket number, and it is never derived from anything - not a name, not an
email, not a token, not the offer ids. It is random, from a small unambiguous
alphabet, and its only job is to be a handle a person can read down a phone.

Distinct from:
- the Duffel Test Order id (`ord_...`), which is a provider handle shown only
  in a labelled technical section;
- a real airline ticket number, which this build never produces.
"""

from __future__ import annotations

import secrets

#: No I/O/1/0 - a reference is read aloud and typed by hand.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_PREFIX = "DTR-V8-"
_BODY_LEN = 6


def new_journey_reference() -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_BODY_LEN))
    return f"{_PREFIX}{body}"


def is_journey_reference(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return False
    body = value[len(_PREFIX):]
    return len(body) == _BODY_LEN and all(ch in _ALPHABET for ch in body)
