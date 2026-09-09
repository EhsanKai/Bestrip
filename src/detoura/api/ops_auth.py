"""Detoura Ops authentication (V8.5 Phase B).

A single shared secret, ``DETOURA_OPS_TOKEN``, supplied at runtime. If it is
unset, the ops console is **disabled** - every ops route returns 503 and no
data is exposed. That is fail-closed by construction.

An operator exchanges the shared token for a short-lived opaque session token
(held in memory, 8h TTL). Every other ops route requires
``Authorization: Bearer <session-token>``. There are no individual operator
identities in this model (that was the RBAC option, not chosen), so ops audit
entries are attributed to ``"ops"``.

The shared token is compared in constant time and is never logged or returned.
"""

from __future__ import annotations

import os
import secrets
import threading
import time

from fastapi import Header, HTTPException

_SESSION_TTL = 8 * 60 * 60
_MAX_SESSIONS = 50

_sessions: dict[str, float] = {}
_lock = threading.Lock()


def ops_token() -> str:
    return os.getenv("DETOURA_OPS_TOKEN", "").strip()


def ops_enabled() -> bool:
    return bool(ops_token())


def _prune(now: float) -> None:
    for tok in [t for t, exp in _sessions.items() if exp <= now]:
        _sessions.pop(tok, None)


def verify_shared_token(candidate: str) -> bool:
    configured = ops_token()
    if not configured:
        return False
    return secrets.compare_digest(configured, (candidate or "").strip())


def create_session() -> tuple[str, float]:
    now = time.monotonic()
    token = "ops_" + secrets.token_urlsafe(30)
    with _lock:
        _prune(now)
        while len(_sessions) >= _MAX_SESSIONS:
            _sessions.pop(next(iter(_sessions)), None)
        _sessions[token] = now + _SESSION_TTL
    return token, float(_SESSION_TTL)


def validate_session(token: str) -> bool:
    now = time.monotonic()
    with _lock:
        exp = _sessions.get(token or "")
        if exp is None:
            return False
        if exp <= now:
            _sessions.pop(token, None)
            return False
        return True


def revoke_session(token: str) -> None:
    with _lock:
        _sessions.pop(token or "", None)


def require_ops(authorization: str = Header(default="")) -> str:
    """FastAPI dependency for every protected ops route. Returns the ops actor
    string on success."""
    if not ops_enabled():
        raise HTTPException(
            status_code=503,
            detail={"message": "Detoura Ops is not configured on this deployment."},
        )
    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not validate_session(token):
        raise HTTPException(
            status_code=401,
            detail={"message": "Sign in to Detoura Ops."},
        )
    return "ops"
