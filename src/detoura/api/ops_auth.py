"""Detoura Ops authentication (V8.5 Phase B; brute-force guard added V9
Phase 6 slice 9).

A single shared secret, ``DETOURA_OPS_TOKEN``, supplied at runtime. If it is
unset, the ops console is **disabled** - every ops route returns 503 and no
data is exposed. That is fail-closed by construction.

An operator exchanges the shared token for a short-lived opaque session token
(held in memory, 8h TTL). Every other ops route requires
``Authorization: Bearer <session-token>``. There are no individual operator
identities in this model (that was the RBAC option, not chosen), so ops audit
entries are attributed to ``"ops"``.

The shared token is compared in constant time and is never logged or
returned. Constant-time comparison defeats a *timing* attack, not a *volume*
one - ``verify_ops_login_rate_limit`` bounds how many token guesses one
source may make, the same generic ``services.rate_limit.RateLimiter`` the
account-login endpoint uses, so a short/weak operator-chosen token is not
purely one comparison away from being guessable at network speed.
"""

from __future__ import annotations

import os
import secrets
import threading
import time

from fastapi import Header, HTTPException

from ..services.rate_limit import rate_limiter

_SESSION_TTL = 8 * 60 * 60
_MAX_SESSIONS = 50


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


_DEFAULT_OPS_LOGIN_MAX_ATTEMPTS = 10
_DEFAULT_OPS_LOGIN_WINDOW_SECONDS = 300

_sessions: dict[str, float] = {}
_lock = threading.Lock()


def check_ops_login_rate_limit(client_ip: str) -> bool:
    """``True`` if this IP may attempt another shared-token exchange right
    now. Every call that reaches ``verify_shared_token`` - success or
    failure - should be gated on this first; a denied call does not count
    against a later window (see ``RateLimiter.allow``).

    Generous for a human operator (a mistyped token a few times), tight
    enough to blunt an automated guessing loop against a weak token - the
    shared secret has no username/email half to also scope this by, so this
    is a plain per-IP volume cap, not a per-(ip, identity) pair. Read from
    the environment on every call, not once at import time, so tests (and
    an operator changing the env without a code deploy) see the current
    value rather than whatever was configured when this module first
    loaded - the same reason ``auth_config()`` is a function, not a
    module-level constant.
    """
    max_attempts = _int_env("OPS_LOGIN_MAX_ATTEMPTS", _DEFAULT_OPS_LOGIN_MAX_ATTEMPTS)
    window_seconds = float(_int_env("OPS_LOGIN_WINDOW_SECONDS", _DEFAULT_OPS_LOGIN_WINDOW_SECONDS))
    return rate_limiter().allow(
        "ops_login", client_ip, max_calls=max_attempts, window_seconds=window_seconds,
    )


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
