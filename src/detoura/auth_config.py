"""Configuration for account/session security (V9 Phase 2.6 Part A).

Every tunable comes from the environment, read once, mirroring the pattern
in :mod:`detoura.search_intel_config` and
:mod:`detoura.acquisition_config`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Module-level defaults, referenced by both the dataclass fields and
#: from_env() - a @dataclass(slots=True) classmethod cannot read cls.field
#: for its own default (slots replace it with a member_descriptor), so
#: from_env() must not do `cls.session_ttl_seconds` etc.
DEFAULT_SESSION_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 days
DEFAULT_LOGIN_MAX_ATTEMPTS = 8
DEFAULT_LOGIN_WINDOW_SECONDS = 300.0
DEFAULT_REGISTER_MAX_ATTEMPTS = 5
DEFAULT_REGISTER_WINDOW_SECONDS = 3600.0


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no")


@dataclass(frozen=True, slots=True)
class AuthConfig:
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    #: A worker/reverse-proxy sets DETOURA_ENV=production; only then are
    #: cookies marked Secure (a plain-HTTP local dev server cannot set a
    #: Secure cookie the browser will actually send back - documented
    #: development exception, §A5).
    is_production: bool = False
    session_cookie_name: str = "detoura_session"
    csrf_cookie_name: str = "detoura_csrf"
    #: Login attempts per email per window, and per-IP registration attempts
    #: - generous enough for a real user who mistypes a password a few
    #: times, tight enough to blunt a credential-stuffing loop (§A7).
    login_max_attempts: int = DEFAULT_LOGIN_MAX_ATTEMPTS
    login_window_seconds: float = DEFAULT_LOGIN_WINDOW_SECONDS
    register_max_attempts: int = DEFAULT_REGISTER_MAX_ATTEMPTS
    register_window_seconds: float = DEFAULT_REGISTER_WINDOW_SECONDS

    @classmethod
    def from_env(cls) -> "AuthConfig":
        return cls(
            session_ttl_seconds=max(60, _int("AUTH_SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS)),
            is_production=_bool("DETOURA_ENV_PRODUCTION", False) or os.getenv("DETOURA_ENV", "").strip().lower() == "production",
            login_max_attempts=max(1, _int("AUTH_LOGIN_MAX_ATTEMPTS", DEFAULT_LOGIN_MAX_ATTEMPTS)),
            login_window_seconds=float(max(1, _int("AUTH_LOGIN_WINDOW_SECONDS", int(DEFAULT_LOGIN_WINDOW_SECONDS)))),
            register_max_attempts=max(1, _int("AUTH_REGISTER_MAX_ATTEMPTS", DEFAULT_REGISTER_MAX_ATTEMPTS)),
            register_window_seconds=float(max(1, _int("AUTH_REGISTER_WINDOW_SECONDS", int(DEFAULT_REGISTER_WINDOW_SECONDS)))),
        )


_CONFIG: AuthConfig | None = None


def auth_config() -> AuthConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = AuthConfig.from_env()
    return _CONFIG


def reset_auth_config() -> None:
    global _CONFIG
    _CONFIG = None
