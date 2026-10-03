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
#: Per-(IP, email) login attempts per window - a short, soft throttle, never
#: a long-lived lock (V9 Phase 6 slice 1). Deliberately never keyed by email
#: alone: see auth_service.login and docs/V9_PHASE6_AUTH_HARDENING.md.
DEFAULT_LOGIN_MAX_ATTEMPTS = 8
DEFAULT_LOGIN_WINDOW_SECONDS = 300.0
#: Coarse per-IP login-attempt volume control, independent of which email(s)
#: it targets - catches one source cycling through many identifiers
#: (credential stuffing) rather than any single targeted account. Generous
#: enough that a shared NAT/office network is not routinely blocked; if
#: X-Forwarded-For is not trusted (AUTH_TRUSTED_PROXY_HOPS=0, the default),
#: every request behind one shared proxy collapses onto its single address,
#: so this budget is effectively shared by everyone behind it - keep it
#: generous unless proxy trust is configured.
DEFAULT_LOGIN_IP_MAX_ATTEMPTS = 30
DEFAULT_LOGIN_IP_WINDOW_SECONDS = 300.0
DEFAULT_REGISTER_MAX_ATTEMPTS = 5
DEFAULT_REGISTER_WINDOW_SECONDS = 3600.0
#: Password reset: opaque token TTL, and per-(IP, email)/per-IP request
#: budgets mirroring register_pair/login_ip exactly (V9 Google auth §10/§14).
DEFAULT_RESET_TOKEN_TTL_SECONDS = 30 * 60
DEFAULT_RESET_REQUEST_MAX_ATTEMPTS = 5
DEFAULT_RESET_REQUEST_WINDOW_SECONDS = 3600.0
DEFAULT_RESET_REQUEST_IP_MAX_ATTEMPTS = 20
DEFAULT_RESET_REQUEST_IP_WINDOW_SECONDS = 3600.0
#: Confirming a reset token is a per-IP volume control only - the token
#: itself is a 32-byte random value, not a guessable secret, so this is
#: defense-in-depth against blind brute-forcing, not the primary control.
DEFAULT_RESET_CONFIRM_MAX_ATTEMPTS = 20
DEFAULT_RESET_CONFIRM_WINDOW_SECONDS = 3600.0
#: Password change: an authenticated action, keyed per user_id - bounds a
#: caller with a stolen/live session from brute-forcing "current password".
DEFAULT_PASSWORD_CHANGE_MAX_ATTEMPTS = 10
DEFAULT_PASSWORD_CHANGE_WINDOW_SECONDS = 3600.0
#: Number of reverse-proxy hops in front of this service whose
#: X-Forwarded-For entry is trusted. 0 (default) = trust nothing, use the
#: raw TCP peer. See services/client_ip.py for the exact semantics and why
#: a wrong value here is a spoofing hole, not a convenience knob.
DEFAULT_TRUSTED_PROXY_HOPS = 0


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
    #: Independent of ``is_production`` on purpose (V9 staging hardening): a
    #: real deployment was found serving `DETOURA_ENV=production` (needed for
    #: Secure cookies) on a host that was meant to stay unindexed, which the
    #: old single `is_production` gate in app.py's robots middleware could not
    #: express - that flag could only buy noindex by giving up Secure cookies.
    #: Set `DETOURA_FORCE_NOINDEX=true` to keep a production-grade, HTTPS,
    #: Secure-cookie deployment out of search indexes regardless of
    #: `DETOURA_ENV`. Defaulting to False means a real production cutover
    #: that already has `DETOURA_ENV=production` set stays indexable exactly
    #: as before unless this is *also* explicitly set - no silent behavior
    #: change for existing production config.
    force_noindex: bool = False
    session_cookie_name: str = "detoura_session"
    csrf_cookie_name: str = "detoura_csrf"
    #: Login attempts per (IP, email) pair per window, and per-IP registration
    #: attempts - generous enough for a real user who mistypes a password a
    #: few times, tight enough to blunt a credential-stuffing loop (§A7).
    login_max_attempts: int = DEFAULT_LOGIN_MAX_ATTEMPTS
    login_window_seconds: float = DEFAULT_LOGIN_WINDOW_SECONDS
    login_ip_max_attempts: int = DEFAULT_LOGIN_IP_MAX_ATTEMPTS
    login_ip_window_seconds: float = DEFAULT_LOGIN_IP_WINDOW_SECONDS
    register_max_attempts: int = DEFAULT_REGISTER_MAX_ATTEMPTS
    register_window_seconds: float = DEFAULT_REGISTER_WINDOW_SECONDS
    trusted_proxy_hops: int = DEFAULT_TRUSTED_PROXY_HOPS
    reset_token_ttl_seconds: int = DEFAULT_RESET_TOKEN_TTL_SECONDS
    reset_request_max_attempts: int = DEFAULT_RESET_REQUEST_MAX_ATTEMPTS
    reset_request_window_seconds: float = DEFAULT_RESET_REQUEST_WINDOW_SECONDS
    reset_request_ip_max_attempts: int = DEFAULT_RESET_REQUEST_IP_MAX_ATTEMPTS
    reset_request_ip_window_seconds: float = DEFAULT_RESET_REQUEST_IP_WINDOW_SECONDS
    reset_confirm_max_attempts: int = DEFAULT_RESET_CONFIRM_MAX_ATTEMPTS
    reset_confirm_window_seconds: float = DEFAULT_RESET_CONFIRM_WINDOW_SECONDS
    password_change_max_attempts: int = DEFAULT_PASSWORD_CHANGE_MAX_ATTEMPTS
    password_change_window_seconds: float = DEFAULT_PASSWORD_CHANGE_WINDOW_SECONDS

    @classmethod
    def from_env(cls) -> "AuthConfig":
        return cls(
            session_ttl_seconds=max(60, _int("AUTH_SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS)),
            is_production=_bool("DETOURA_ENV_PRODUCTION", False) or os.getenv("DETOURA_ENV", "").strip().lower() == "production",
            force_noindex=_bool("DETOURA_FORCE_NOINDEX", False),
            login_max_attempts=max(1, _int("AUTH_LOGIN_MAX_ATTEMPTS", DEFAULT_LOGIN_MAX_ATTEMPTS)),
            login_window_seconds=float(max(1, _int("AUTH_LOGIN_WINDOW_SECONDS", int(DEFAULT_LOGIN_WINDOW_SECONDS)))),
            login_ip_max_attempts=max(1, _int("AUTH_LOGIN_IP_MAX_ATTEMPTS", DEFAULT_LOGIN_IP_MAX_ATTEMPTS)),
            login_ip_window_seconds=float(max(1, _int("AUTH_LOGIN_IP_WINDOW_SECONDS", int(DEFAULT_LOGIN_IP_WINDOW_SECONDS)))),
            register_max_attempts=max(1, _int("AUTH_REGISTER_MAX_ATTEMPTS", DEFAULT_REGISTER_MAX_ATTEMPTS)),
            register_window_seconds=float(max(1, _int("AUTH_REGISTER_WINDOW_SECONDS", int(DEFAULT_REGISTER_WINDOW_SECONDS)))),
            trusted_proxy_hops=max(0, _int("AUTH_TRUSTED_PROXY_HOPS", DEFAULT_TRUSTED_PROXY_HOPS)),
            reset_token_ttl_seconds=max(60, _int("AUTH_RESET_TOKEN_TTL_SECONDS", DEFAULT_RESET_TOKEN_TTL_SECONDS)),
            reset_request_max_attempts=max(1, _int("AUTH_RESET_REQUEST_MAX_ATTEMPTS", DEFAULT_RESET_REQUEST_MAX_ATTEMPTS)),
            reset_request_window_seconds=float(max(1, _int("AUTH_RESET_REQUEST_WINDOW_SECONDS", int(DEFAULT_RESET_REQUEST_WINDOW_SECONDS)))),
            reset_request_ip_max_attempts=max(1, _int("AUTH_RESET_REQUEST_IP_MAX_ATTEMPTS", DEFAULT_RESET_REQUEST_IP_MAX_ATTEMPTS)),
            reset_request_ip_window_seconds=float(max(1, _int("AUTH_RESET_REQUEST_IP_WINDOW_SECONDS", int(DEFAULT_RESET_REQUEST_IP_WINDOW_SECONDS)))),
            reset_confirm_max_attempts=max(1, _int("AUTH_RESET_CONFIRM_MAX_ATTEMPTS", DEFAULT_RESET_CONFIRM_MAX_ATTEMPTS)),
            reset_confirm_window_seconds=float(max(1, _int("AUTH_RESET_CONFIRM_WINDOW_SECONDS", int(DEFAULT_RESET_CONFIRM_WINDOW_SECONDS)))),
            password_change_max_attempts=max(1, _int("AUTH_PASSWORD_CHANGE_MAX_ATTEMPTS", DEFAULT_PASSWORD_CHANGE_MAX_ATTEMPTS)),
            password_change_window_seconds=float(max(1, _int("AUTH_PASSWORD_CHANGE_WINDOW_SECONDS", int(DEFAULT_PASSWORD_CHANGE_WINDOW_SECONDS)))),
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
