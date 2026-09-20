"""Authenticated password change + email-based password reset (V9 Google
auth + account lifecycle §9/§10/§11).

Both flows end by calling the exact same :func:`password_hashing.hash_password`
+ :func:`persistence.accounts.set_password_hash` the registration path
uses - a changed/reset/first-set password is stored identically either
way, Argon2id, same column.

**Session revocation, decided explicitly (§12):**

* **Change** (the caller already holds a live session and just re-proved
  their current password): every session for the account is revoked, then
  one fresh session is minted for the *caller* - so the person who just
  authenticated is not logged out of their own request, but a stolen
  session sitting anywhere else dies immediately.
* **Reset** (the caller does not necessarily hold any session - they may
  have arrived here purely by clicking an emailed link): every session is
  revoked and none is re-issued. The point of a reset is precisely that
  the account may have been compromised; the person proves the new
  password by logging in with it afterwards, the same as anyone else.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..auth_config import AuthConfig, auth_config
from ..communication_config import resolve_communication_provider
from ..models.account import AccountStatus
from ..persistence import accounts as store
from ..persistence import audit
from ..persistence.db import Database
from ..providers.communication_provider import CommunicationProvider
from .auth_service import LoginResult, mint_session
from .client_ip import UNKNOWN as UNKNOWN_CLIENT_IP
from .email_normalization import InvalidEmail, normalize_email
from .password_hashing import PasswordPolicyError, hash_password, verify_password
from .rate_limit import rate_limiter

#: Never varies with whether the email exists - the whole point (§10 "no
#: user enumeration").
GENERIC_RESET_REQUEST_RESPONSE = "If an account exists for this email, a password reset link has been sent."


class PasswordServiceError(Exception):
    """A user-facing failure. Messages here are deliberately specific for
    *authenticated* actions (change) where the caller already proved who
    they are - contrast with reset-request, which is generic by design."""


class RateLimitedError(Exception):
    pass


def change_password(
    db: Database, *, user_id: str, current_password: str, new_password: str,
    now: datetime | None = None, cfg: AuthConfig | None = None,
) -> LoginResult:
    """Raises :class:`PasswordServiceError` for: no such/inactive account,
    an account with no password set yet (Google-only - directs to reset
    instead, §11), a wrong current password, or a new password outside
    policy. Returns the caller's freshly re-issued session on success."""
    cfg = cfg or auth_config()
    now = now or datetime.now(timezone.utc)
    limiter = rate_limiter()
    if not limiter.allow("password_change", user_id, max_calls=cfg.password_change_max_attempts,
                          window_seconds=cfg.password_change_window_seconds):
        raise RateLimitedError("Too many attempts. Try again later.")

    user = store.get_user(db, user_id)
    if user is None or user["status"] != AccountStatus.ACTIVE.value:
        raise PasswordServiceError("This account is not available.")
    if user["password_hash"] is None:
        raise PasswordServiceError(
            "This account has no password set yet. Use 'forgot password' to set one."
        )
    if not verify_password(current_password, user["password_hash"]):
        raise PasswordServiceError("Current password is incorrect.")
    try:
        new_hash = hash_password(new_password)
    except PasswordPolicyError as exc:
        raise PasswordServiceError(str(exc)) from exc

    store.set_password_hash(db, user_id, new_hash, now=now)
    store.revoke_all_sessions_for_user(db, user_id, now=now)
    limiter.reset("password_change", user_id)
    audit.record(db, actor=user_id, action="password_changed", target_type="user_account", target_id=user_id)
    return mint_session(db, user_id=user_id, now=now, cfg=cfg)


def request_password_reset(
    db: Database, *, email: str, client_ip: str = UNKNOWN_CLIENT_IP,
    now: datetime | None = None, cfg: AuthConfig | None = None,
    provider: CommunicationProvider | None = None,
) -> None:
    """Always completes successfully from the caller's point of view - the
    generic :data:`GENERIC_RESET_REQUEST_RESPONSE` covers every case
    (unknown email, disabled account, rate-limited) alike (§10 "no user
    enumeration"). An email is only ever actually sent for a real, ACTIVE
    account; nothing here reveals which case applied."""
    cfg = cfg or auth_config()
    now = now or datetime.now(timezone.utc)
    limiter = rate_limiter()

    if not limiter.allow("password_reset_ip", client_ip, max_calls=cfg.reset_request_ip_max_attempts,
                          window_seconds=cfg.reset_request_ip_window_seconds):
        return  # generic no-op from the caller's perspective - see docstring

    try:
        normalized = normalize_email(email)
    except InvalidEmail:
        return

    pair_key = f"{client_ip}\x1f{normalized}"
    if not limiter.allow("password_reset_pair", pair_key, max_calls=cfg.reset_request_max_attempts,
                          window_seconds=cfg.reset_request_window_seconds):
        return

    user = store.get_user_by_email(db, normalized)
    if user is None or user["status"] != AccountStatus.ACTIVE.value:
        return

    raw_token = store.new_reset_token()
    store.create_reset_token(
        db, user_id=user["user_id"], token_hash=store.hash_token(raw_token),
        ttl_seconds=cfg.reset_token_ttl_seconds, now=now,
    )
    audit.record(db, actor=user["user_id"], action="password_reset_requested",
                target_type="user_account", target_id=user["user_id"])

    provider = provider or resolve_communication_provider()
    body = (
        "A password reset was requested for your Detoura account.\n\n"
        f"Reset code: {raw_token}\n\n"
        f"This code expires in {cfg.reset_token_ttl_seconds // 60} minutes and can be used once. "
        "If you did not request this, you can ignore this message."
    )
    # Frontend contract (documented, not built here - out of scope per this
    # slice's ownership boundary): the reset page must collect this raw
    # token (e.g. from a query parameter on a reset-link URL it constructs
    # itself) and POST it, with the new password, to
    # /api/v1/auth/password/reset/confirm. Never logged past this point.
    provider.send(
        idempotency_key=store.hash_token(raw_token), recipient=normalized,
        subject="Reset your Detoura password", body_text=body, reference=f"password_reset:{user['user_id']}",
    )


def confirm_password_reset(
    db: Database, *, token: str, new_password: str, client_ip: str = UNKNOWN_CLIENT_IP,
    now: datetime | None = None, cfg: AuthConfig | None = None,
) -> None:
    """Raises :class:`PasswordServiceError` for an invalid/expired/already-used
    token or a new password outside policy - this endpoint's caller has not
    yet proven anything beyond "read the emailed token", so unlike
    :func:`change_password` these messages stay generic about *why* a token
    failed (never "this token was already used" vs "this token never
    existed" - both read the same to a caller probing tokens blindly, §10
    "single use")."""
    cfg = cfg or auth_config()
    now = now or datetime.now(timezone.utc)
    limiter = rate_limiter()
    if not limiter.allow("password_reset_confirm", client_ip, max_calls=cfg.reset_confirm_max_attempts,
                          window_seconds=cfg.reset_confirm_window_seconds):
        raise RateLimitedError("Too many attempts. Try again later.")

    token_hash = store.hash_token(token)
    row = store.get_reset_token(db, token_hash)
    if row is None or row["consumed_at"] is not None or datetime.fromisoformat(row["expires_at"]) <= now:
        raise PasswordServiceError("This reset link is invalid or has expired.")

    user = store.get_user(db, row["user_id"])
    if user is None or user["status"] != AccountStatus.ACTIVE.value:
        raise PasswordServiceError("This reset link is invalid or has expired.")

    try:
        new_hash = hash_password(new_password)
    except PasswordPolicyError as exc:
        raise PasswordServiceError(str(exc)) from exc

    if not store.consume_reset_token(db, token_hash, now=now):
        # Lost a race to a concurrent confirm using the same token - the
        # other caller's write wins; this one must not also apply.
        raise PasswordServiceError("This reset link is invalid or has expired.")

    store.set_password_hash(db, user["user_id"], new_hash, now=now)
    store.revoke_all_sessions_for_user(db, user["user_id"], now=now)
    audit.record(db, actor=user["user_id"], action="password_reset_completed",
                target_type="user_account", target_id=user["user_id"])
