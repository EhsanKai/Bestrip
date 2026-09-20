"""Google Sign-In business logic: the account-linking policy (V9 Google
auth §4 - the critical section of this slice).

Google identity is bound to the provider's stable subject (``sub``), never
to email alone (§3, §5) - a Google account's email can change; its
``sub`` never does. Cases, matching the audit brief exactly:

* **A - brand-new identity, no existing account**: a new ``UserAccount``
  (no password) and its ``AuthIdentity`` are created together.
* **B - returning identity**: :func:`_complete_signin` finds the existing
  ``AuthIdentity`` by ``(provider, subject)`` and logs in as its
  ``user_id`` unconditionally - never re-checked against email.
* **C - existing password (or any other) account shares this email, but
  this Google identity has never been linked to it, and the caller is not
  already authenticated**: refused outright. A short-lived
  ``pending_google_links`` row is created and :class:`GoogleLinkRequired`
  is raised - the caller must separately authenticate as that account and
  call :func:`confirm_pending_link` to complete the link explicitly. This
  never happens silently (the exact risk the audit brief calls out).
* **D - a linked identity's Google email changes**: transparent under B;
  ``provider_email`` is refreshed as metadata only, never used to re-key.
* **E - multiple Google identities**: each is its own ``AuthIdentity`` row;
  a second identity sharing an email with an already-linked account still
  goes through C/F, never auto-merged into one account.
* **F - an already-authenticated user deliberately connecting Google**:
  :func:`build_start_url` accepts ``link_user_id``; the callback commits
  straight to that account once the identity is free.
* **G - the identity is already bound to a *different* account** than the
  one now trying to claim it (via F or via confirming a C-flow): always
  :class:`GoogleLinkConflict`, never a silent ownership transfer.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..google_auth_config import GoogleAuthConfig, client_secret, google_auth_config
from ..models.account import AccountStatus
from ..persistence import accounts as store
from ..persistence import audit
from ..persistence.db import Database
from ..providers.http import HttpClient, UrllibHttpClient
from . import google_oauth
from .auth_service import LoginResult, mint_session
from .email_normalization import normalize_email


class GoogleAuthDisabled(Exception):
    """Google Sign-In has no usable configuration - fail closed (§7)."""


class GoogleAuthError(Exception):
    """A user-facing, generic OAuth/OIDC failure. Never carries Google's
    own error detail (§15)."""


class GoogleLinkRequired(Exception):
    """Case C. Carries the opaque ``link_id`` the frontend must have the
    user redeem (via :func:`confirm_pending_link`, after authenticating
    with their existing credential) to complete the link explicitly."""

    def __init__(self, link_id: str) -> None:
        super().__init__("an account with this email already exists")
        self.link_id = link_id


class GoogleLinkConflict(Exception):
    """Case G: this identity is already bound to a different account."""


def _require_enabled(cfg: GoogleAuthConfig) -> None:
    if not cfg.enabled:
        raise GoogleAuthDisabled("Google Sign-In is not configured")


def build_start_url(
    db: Database, *, link_user_id: str | None = None,
    cfg: GoogleAuthConfig | None = None, now: datetime | None = None,
) -> str:
    """Begins one authorization-code/PKCE round trip and returns the URL to
    redirect the browser to. ``link_user_id`` set means Case F: an
    authenticated user deliberately connecting their Google account, not a
    plain sign-in."""
    cfg = cfg or google_auth_config()
    _require_enabled(cfg)
    now = now or datetime.now(timezone.utc)
    pkce = google_oauth.generate_pkce()
    state = google_oauth.new_state()
    nonce = google_oauth.new_nonce()
    store.create_pending_oauth(
        db, state_hash=store.hash_token(state), code_verifier=pkce.verifier, nonce=nonce,
        link_user_id=link_user_id, ttl_seconds=cfg.pending_ttl_seconds, now=now,
    )
    return google_oauth.build_authorization_url(
        client_id=cfg.client_id, redirect_uri=cfg.redirect_uri, state=state,
        nonce=nonce, code_challenge=pkce.challenge,
    )


def complete_google_callback(
    db: Database, *, code: str, state: str, cfg: GoogleAuthConfig | None = None,
    http_client: HttpClient | None = None, now: datetime | None = None,
) -> LoginResult:
    """Exchanges the code, validates the ID token, and applies the
    account-linking policy above. Raises :class:`GoogleAuthError` for any
    protocol failure, :class:`GoogleLinkRequired` for Case C,
    :class:`GoogleLinkConflict` for Case G."""
    cfg = cfg or google_auth_config()
    _require_enabled(cfg)
    now = now or datetime.now(timezone.utc)
    http_client = http_client or UrllibHttpClient()

    pending = store.consume_pending_oauth(db, store.hash_token(state), now=now)
    if pending is None:
        raise GoogleAuthError("invalid or expired authorization state")
    if datetime.fromisoformat(pending["expires_at"]) <= now:
        raise GoogleAuthError("authorization state has expired")

    try:
        id_token = google_oauth.exchange_code_for_tokens(
            http_client, code=code, client_id=cfg.client_id, client_secret=client_secret(),
            redirect_uri=cfg.redirect_uri, code_verifier=pending["code_verifier"],
        )
        identity = google_oauth.verify_id_token(
            http_client, id_token=id_token, expected_audience=cfg.client_id,
            expected_nonce=pending["nonce"], now=now,
        )
    except google_oauth.GoogleOAuthError as exc:
        raise GoogleAuthError(str(exc)) from exc

    if not identity.email_verified or not identity.email:
        raise GoogleAuthError("Google did not return a verified email address")

    link_user_id = pending["link_user_id"]
    if link_user_id:
        return _complete_link(db, user_id=link_user_id, identity=identity, cfg=cfg, now=now)
    return _complete_signin(db, identity=identity, cfg=cfg, now=now)


def _complete_signin(db: Database, *, identity, cfg: GoogleAuthConfig, now: datetime) -> LoginResult:
    existing = store.get_identity_by_subject(db, provider="google", provider_subject=identity.subject)
    if existing is not None:
        # Case B: returning identity - refresh cached email metadata only.
        store.touch_identity_email(db, existing["identity_id"], provider_email=identity.email, now=now)
        user = store.get_user(db, existing["user_id"])
        if user is None or user["status"] != AccountStatus.ACTIVE.value:
            raise GoogleAuthError("this account is not available")
        audit.record(db, actor=user["user_id"], action="google_login_returning_identity",
                     target_type="user_account", target_id=user["user_id"])
        store.set_last_login(db, user["user_id"], now=now)
        return mint_session(db, user_id=user["user_id"], now=now)

    normalized_email = normalize_email(identity.email)
    existing_user = store.get_user_by_email(db, normalized_email)
    if existing_user is not None:
        # Case C: never silently linked - see the module docstring.
        link_id = store.create_pending_link(
            db, provider_subject=identity.subject, provider_email=identity.email,
            ttl_seconds=cfg.link_ttl_seconds, now=now,
        )
        audit.record(db, actor="unknown", action="google_link_required",
                     target_type="user_account", target_id=existing_user["user_id"])
        raise GoogleLinkRequired(link_id)

    # Case A: brand-new identity, brand-new (password-less) account - user
    # + identity are created together, atomically (see
    # create_google_user_with_identity's own docstring for why).
    try:
        user_id = store.create_google_user_with_identity(
            db, email_normalized=normalized_email, provider="google",
            provider_subject=identity.subject, provider_email=identity.email, now=now,
        )
    except store.DuplicateEmail:
        # A concurrent registration/login won the race between the read
        # above and this insert - re-run against the row that now exists
        # rather than surfacing a raw integrity error.
        return _complete_signin(db, identity=identity, cfg=cfg, now=now)
    audit.record(db, actor=user_id, action="google_login_new_identity",
                 target_type="user_account", target_id=user_id)
    store.set_last_login(db, user_id, now=now)
    return mint_session(db, user_id=user_id, now=now)


def _complete_link(db: Database, *, user_id: str, identity, cfg: GoogleAuthConfig, now: datetime) -> LoginResult:
    user = store.get_user(db, user_id)
    if user is None or user["status"] != AccountStatus.ACTIVE.value:
        raise GoogleAuthError("this account is not available")

    existing = store.get_identity_by_subject(db, provider="google", provider_subject=identity.subject)
    if existing is not None and existing["user_id"] != user_id:
        audit.record(db, actor=user_id, action="google_link_conflict",
                     target_type="user_account", target_id=user_id)
        raise GoogleLinkConflict("this Google account is already linked to a different Detoura account")
    if existing is not None:
        store.touch_identity_email(db, existing["identity_id"], provider_email=identity.email, now=now)
    else:
        try:
            store.create_identity(db, user_id=user_id, provider="google", provider_subject=identity.subject,
                                  provider_email=identity.email, now=now)
        except Exception as exc:  # sqlite3.IntegrityError on the UNIQUE constraint
            if "UNIQUE" not in str(exc).upper():
                raise
            existing = store.get_identity_by_subject(db, provider="google", provider_subject=identity.subject)
            if existing is None or existing["user_id"] != user_id:
                raise GoogleLinkConflict(
                    "this Google account is already linked to a different Detoura account"
                ) from exc
        audit.record(db, actor=user_id, action="google_link_confirmed",
                     target_type="user_account", target_id=user_id)
    store.set_last_login(db, user_id, now=now)
    return mint_session(db, user_id=user_id, now=now)


def confirm_pending_link(
    db: Database, *, link_id: str, session_user_id: str, now: datetime | None = None,
) -> None:
    """Completes Case C. The caller must already hold an active Detoura
    session for the account whose email matches the pending Google
    identity - re-checked here against the live account row, never assumed
    from how the pending row was created (§4 "avoid silent account
    takeover")."""
    now = now or datetime.now(timezone.utc)
    pending = store.consume_pending_link(db, link_id, now=now)
    if pending is None:
        raise GoogleAuthError("this link request is invalid or has expired")
    if datetime.fromisoformat(pending["expires_at"]) <= now:
        raise GoogleAuthError("this link request has expired")

    user = store.get_user(db, session_user_id)
    if user is None or user["status"] != AccountStatus.ACTIVE.value:
        raise GoogleAuthError("this account is not available")
    if normalize_email(pending["provider_email"]) != user["email_normalized"]:
        raise GoogleAuthError("this Google account's email does not match your account")

    existing = store.get_identity_by_subject(db, provider="google", provider_subject=pending["provider_subject"])
    if existing is not None and existing["user_id"] != session_user_id:
        raise GoogleLinkConflict("this Google account is already linked to a different Detoura account")
    if existing is None:
        try:
            store.create_identity(
                db, user_id=session_user_id, provider="google",
                provider_subject=pending["provider_subject"], provider_email=pending["provider_email"], now=now,
            )
        except Exception as exc:  # sqlite3.IntegrityError on the UNIQUE constraint
            if "UNIQUE" not in str(exc).upper():
                raise
            existing = store.get_identity_by_subject(db, provider="google", provider_subject=pending["provider_subject"])
            if existing is None or existing["user_id"] != session_user_id:
                raise GoogleLinkConflict(
                    "this Google account is already linked to a different Detoura account"
                ) from exc
    audit.record(db, actor=session_user_id, action="google_link_confirmed",
                 target_type="user_account", target_id=session_user_id)
