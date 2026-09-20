"""Configuration for Google Sign-In (V9 Google auth + account lifecycle).

Mirrors ``communication_config.py``'s fail-closed shape: nothing here ever
invents a placeholder client id/secret. With no configuration, Google
Sign-In is simply disabled - ``/api/v1/auth/google/start`` and
``/callback`` return a clear 503 rather than proceeding with an empty
client id (§7 "fail-closed configuration").

``client_secret`` is read directly from the environment at call time in
:mod:`detoura.services.google_oauth`, never held on this dataclass -
exactly the same reasoning ``communication_config.py`` gives for
``RESEND_API_KEY``: a diagnostics dump of this config object must never be
able to leak a secret.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


#: How long a single authorization-code/PKCE round trip may stay pending
#: before it is no longer honoured - generous for a human clicking through
#: Google's consent screen, tight enough that a stale/abandoned state value
#: is not usable indefinitely.
DEFAULT_PENDING_TTL_SECONDS = 10 * 60
#: How long an unauthenticated "existing password account, same email"
#: pending link (Case C) stays claimable - see pending_google_links.
DEFAULT_LINK_TTL_SECONDS = 10 * 60


@dataclass(frozen=True, slots=True)
class GoogleAuthConfig:
    client_id: str = ""
    #: Registered with Google for this exact backend callback route - never
    #: a frontend URL. Must exactly match what is registered in the Google
    #: Cloud console, or Google itself refuses the request (redirect URI
    #: validation is Google's job here, not ours to second-guess).
    redirect_uri: str = ""
    #: Where the browser is sent back to after the callback finishes -
    #: always a path/URL on Detoura's own frontend, never Google's.
    post_login_redirect_url: str = "/"
    pending_ttl_seconds: int = DEFAULT_PENDING_TTL_SECONDS
    link_ttl_seconds: int = DEFAULT_LINK_TTL_SECONDS

    @property
    def enabled(self) -> bool:
        """``False`` unless a client id, redirect URI, and (checked
        separately, since the secret itself never lives on this object) a
        client secret are all configured. Every caller must check this and
        fail closed rather than proceed with an empty client id."""
        return bool(self.client_id and self.redirect_uri and os.getenv("GOOGLE_CLIENT_SECRET", "").strip())

    @classmethod
    def from_env(cls) -> "GoogleAuthConfig":
        return cls(
            client_id=os.getenv("GOOGLE_CLIENT_ID", "").strip(),
            redirect_uri=os.getenv("GOOGLE_REDIRECT_URI", "").strip(),
            post_login_redirect_url=os.getenv("GOOGLE_POST_LOGIN_REDIRECT_URL", "").strip() or "/",
            pending_ttl_seconds=max(60, _int("GOOGLE_OAUTH_PENDING_TTL_SECONDS", DEFAULT_PENDING_TTL_SECONDS)),
            link_ttl_seconds=max(60, _int("GOOGLE_OAUTH_LINK_TTL_SECONDS", DEFAULT_LINK_TTL_SECONDS)),
        )


_CONFIG: GoogleAuthConfig | None = None


def google_auth_config() -> GoogleAuthConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = GoogleAuthConfig.from_env()
    return _CONFIG


def reset_google_auth_config() -> None:
    global _CONFIG
    _CONFIG = None


def client_secret() -> str:
    """Read fresh from the environment every call - never cached on
    :class:`GoogleAuthConfig` (see the module docstring)."""
    return os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
