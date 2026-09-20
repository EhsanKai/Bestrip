"""Google Sign-In protocol layer (V9 Google auth): PKCE + the OAuth 2.0
Authorization Code flow + ID token (OpenID Connect) validation.

Nothing here is a custom protocol - every call follows Google's own
documented OAuth 2.0 / OpenID Connect web-server flow
(https://developers.google.com/identity/protocols/oauth2/web-server) with
PKCE (RFC 7636) on top, which Google's endpoint accepts unconditionally.

**ID token validation - an honest, documented tradeoff.** The
textbook-correct approach is local verification: fetch Google's JWKS, pick
the key by ``kid``, and verify the RS256 signature yourself (e.g. via
``google-auth`` or ``PyJWT`` + ``cryptography``). This sandbox's Python
3.14 interpreter has no prebuilt ``cryptography`` wheel on PyPI and no Rust
toolchain installed to build one from source (verified directly - see
docs/V9_GOOGLE_AUTH_ACCOUNT_LIFECYCLE_REPORT.md §16), so that dependency
cannot be installed or *tested* here today. Rather than add an
uninstallable dependency or silently skip verification, this module
delegates signature verification to Google's own ``tokeninfo`` endpoint
(https://oauth2.googleapis.com/tokeninfo?id_token=...) - a mechanism Google
documents and hosts itself, reached over a server-to-server TLS connection
this backend controls (never the browser). This is explicitly *not* "trust
the token because we got it over HTTPS": every claim Google's endpoint
returns - issuer, audience, expiry, nonce - is still independently checked
by :func:`verify_id_token` below and rejected on any mismatch (fail-closed,
§2/§7). The local-JWKS upgrade (``PyJWT`` + ``cryptography``, or
``google-auth``) is deliberately not added as a dependency here since it
cannot be installed *or tested* in this sandbox - adding an uninstallable,
untestable dependency would be worse than the documented tradeoff above.
On a real deployment target (``requires-python`` is 3.11+, where
``cryptography`` has prebuilt wheels and this is a non-issue), swapping it
in only ever touches :func:`verify_id_token`, never a caller.

**No Google access/refresh token is ever persisted.** The token endpoint's
response also carries an ``access_token`` - Detoura has no use for ongoing
Google API access on the user's behalf, so it is read out of the response
and immediately discarded, never logged, never stored (§3).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from ..providers.http import HttpClient

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"

#: Google issues both forms across its own documentation and live tokens.
VALID_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})


class GoogleOAuthError(Exception):
    """Any step of the OAuth/OIDC flow failed. The message is always a
    fixed, generic phrase - never Google's own response body, which could
    otherwise echo back the authorization code or id_token (§15 "no tokens
    logged")."""


@dataclass(frozen=True, slots=True)
class PkceChallenge:
    verifier: str
    challenge: str


def generate_pkce() -> PkceChallenge:
    """RFC 7636 S256: ``verifier`` is a high-entropy random string (never
    sent to Google until the token-exchange step, over the same TLS
    connection this backend controls); ``challenge`` is the URL-safe,
    unpadded base64 of its SHA-256 digest, sent up front in the
    authorization request. Google refuses the code exchange unless the
    verifier presented then hashes back to the challenge presented now -
    binding the two so an intercepted authorization code alone is
    unusable."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PkceChallenge(verifier=verifier, challenge=challenge)


def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_nonce() -> str:
    return secrets.token_urlsafe(32)


def build_authorization_url(
    *, client_id: str, redirect_uri: str, state: str, nonce: str, code_challenge: str,
) -> str:
    """The URL the browser is redirected to. ``prompt=select_account``
    avoids silently reusing whichever Google session happens to be active
    in the browser without the user seeing which account they are picking
    - relevant precisely because of the account-linking policy (§4)."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code_for_tokens(
    http_client: HttpClient, *, code: str, client_id: str, client_secret: str,
    redirect_uri: str, code_verifier: str,
) -> str:
    """Authorization-code + PKCE exchange. Returns the raw ``id_token``
    string only - the response's ``access_token`` (if any) is deliberately
    never returned to the caller (§3). Google's authorization codes are
    single-use by Google's own semantics; a failed exchange is never
    retried with the same code, so this deliberately uses a plain,
    non-retrying transport (mirrors ``stripe_payment.py``/``resend_email.py``'s
    own choice for the same reason)."""
    body = urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    })
    response = http_client.request(
        "POST", GOOGLE_TOKEN_ENDPOINT,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body,
    )
    if not response.ok:
        raise GoogleOAuthError(f"token exchange failed (status {response.status})")
    payload = response.json()
    id_token = payload.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise GoogleOAuthError("token response did not include an id_token")
    return id_token


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    subject: str
    email: str
    email_verified: bool


def verify_id_token(
    http_client: HttpClient, *, id_token: str, expected_audience: str,
    expected_nonce: str, now: datetime,
) -> GoogleIdentity:
    """Validates signature (via Google's tokeninfo endpoint - see module
    docstring), issuer, audience, expiration and nonce. Raises
    :class:`GoogleOAuthError` on any failure - there is no partial-trust
    path (§2 "no ID token treated as trusted without validation")."""
    response = http_client.request("GET", GOOGLE_TOKENINFO_ENDPOINT, params={"id_token": id_token})
    if not response.ok:
        raise GoogleOAuthError("id token failed validation")
    claims = response.json()

    if claims.get("iss") not in VALID_ISSUERS:
        raise GoogleOAuthError("unexpected token issuer")
    if claims.get("aud") != expected_audience:
        raise GoogleOAuthError("unexpected token audience")
    try:
        exp = int(claims.get("exp", 0))
    except (TypeError, ValueError):
        raise GoogleOAuthError("malformed token expiration") from None
    if exp <= now.timestamp():
        raise GoogleOAuthError("token has expired")
    if not expected_nonce or claims.get("nonce") != expected_nonce:
        raise GoogleOAuthError("nonce mismatch")

    subject = str(claims.get("sub") or "")
    if not subject:
        raise GoogleOAuthError("token has no subject claim")
    email = str(claims.get("email") or "").strip()
    email_verified = str(claims.get("email_verified", "")).strip().lower() == "true"
    return GoogleIdentity(subject=subject, email=email, email_verified=email_verified)
