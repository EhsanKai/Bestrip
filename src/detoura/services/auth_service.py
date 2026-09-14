"""Register / login / logout / session validation (V9 Phase 2.6 §A1-§A7;
login abuse controls hardened in V9 Phase 6 slice 1).

The business logic behind the auth API — kept separate from the FastAPI
router so it is testable without an HTTP client and so the enumeration-
resistance and rate-limit rules live in one place regardless of caller.

**Never reveals which half of "email or password" was wrong.** Both "no such
account" and "wrong password" raise the same :class:`AuthError` with the
same message; a disabled account also does, so a caller cannot distinguish
"this email is registered but disabled" from "this email does not exist"
(§A4, §A11).

**Login is never rate-limited by email alone.** An earlier version of this
module keyed the login limiter on the normalized email only, which let an
unauthenticated attacker who merely *knows* a victim's address deny that
victim's own logins by deliberately tripping the limiter against it from
anywhere. Two independent budgets replace that single key:

* a coarse budget per client IP (``"login_ip"`` scope) - bounds how many
  login attempts one source may make regardless of which email(s) it
  targets, catching credential-stuffing volume;
* a short, soft budget per ``(client IP, normalized email)`` pair
  (``"login_pair"`` scope) - bounds repeated targeted guesses against one
  account *from one source*.

Neither is a long-lived lockout (both are fixed, short windows - see
``auth_config.py``), and critically, a victim's own login attempts from
their own IP consume neither budget an attacker who lacks that IP has
touched. ``client_ip`` is caller-supplied rather than read from the request
here on purpose (see ``services/client_ip.py``): resolving *which* header or
socket field is trustworthy is a transport-layer decision the HTTP layer
must make, not this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..auth_config import AuthConfig, auth_config
from ..models.account import AccountStatus
from ..persistence import accounts as store
from ..persistence import audit
from ..persistence.db import Database
from .client_ip import UNKNOWN as UNKNOWN_CLIENT_IP
from .email_normalization import InvalidEmail, normalize_email
from .password_hashing import PasswordPolicyError, hash_password, verify_password
from .rate_limit import RateLimiter, rate_limiter

GENERIC_LOGIN_FAILURE = "Invalid email or password."


class AuthError(Exception):
    """A user-facing, intentionally generic auth failure."""


class RateLimitedError(Exception):
    """Too many attempts. Distinct from :class:`AuthError` so the API layer
    can return 429 rather than 401/400."""


@dataclass(frozen=True, slots=True)
class LoginResult:
    user_id: str
    session_id: str
    raw_token: str
    raw_csrf_token: str
    expires_at: datetime


def register(
    db: Database, *, email: str, password: str, client_ip: str = UNKNOWN_CLIENT_IP,
    now: datetime | None = None, cfg: AuthConfig | None = None,
) -> str:
    """Returns the new ``user_id``. Raises :class:`AuthError` for a bad
    email/password or a duplicate account, :class:`RateLimitedError` for too
    many attempts from this caller.

    Scoped per ``(client_ip, email)``, not email alone (V9 Phase 6 slice 1
    follow-up - an independent adversarial review of the login fix in this
    same slice correctly pointed out that ``register`` still had the exact
    bug class the login limiter was just fixed for: an attacker who only
    knows a victim's email could burn that email's entire registration
    budget with throwaway requests and keep the real person from ever
    signing up with it, for as long as the attacker cares to repeat it).
    """
    cfg = cfg or auth_config()
    now = now or datetime.now(timezone.utc)
    limiter = rate_limiter()
    try:
        normalized = normalize_email(email)
    except InvalidEmail as exc:
        raise AuthError("Invalid email address.") from exc

    pair_key = f"{client_ip}\x1f{normalized}"
    if not limiter.allow("register_pair", pair_key, max_calls=cfg.register_max_attempts,
                          window_seconds=cfg.register_window_seconds):
        raise RateLimitedError("Too many registration attempts. Try again later.")

    try:
        password_hash = hash_password(password)
    except PasswordPolicyError as exc:
        raise AuthError(str(exc)) from exc

    try:
        user_id = store.create_user(db, email_normalized=normalized, password_hash=password_hash, now=now)
    except store.DuplicateEmail as exc:
        # Deliberately the same shape of error as any other registration
        # failure from the API's point of view - see the router, which does
        # not echo "this email is taken" back to the caller either way
        # beyond what a reasonable API must (§A4 note: register is the one
        # endpoint where *some* signal is unavoidable - a client needs to
        # know whether to move on to login - so this is intentionally an
        # explicit AuthError, not silently generic, unlike login).
        raise AuthError("An account with this email already exists.") from exc

    audit.record(db, actor=user_id, action="account_created", target_type="user_account",
                target_id=user_id, note=f"email domain: {normalized.rpartition('@')[2]}")
    return user_id


def login(
    db: Database, *, email: str, password: str, client_ip: str = UNKNOWN_CLIENT_IP,
    now: datetime | None = None, cfg: AuthConfig | None = None,
) -> LoginResult:
    """Raises :class:`AuthError` (always the same generic message) for any
    invalid-credentials/disabled-account case, :class:`RateLimitedError` for
    too many attempts.

    ``client_ip`` should be a trustworthy address from
    ``services.client_ip.resolve_client_ip`` (the API layer's job — see the
    module docstring). Defaulting it to a fixed sentinel rather than making
    it required keeps every existing non-HTTP caller (tests, scripts)
    working unchanged, at the cost of those callers sharing one IP-shaped
    bucket - harmless, since it only affects rate-limit grouping, never
    correctness of who can log in as whom.
    """
    cfg = cfg or auth_config()
    now = now or datetime.now(timezone.utc)
    limiter = rate_limiter()

    # 1) Coarse per-IP volume control, checked first and independent of the
    #    target email: bounds one source cycling through many identifiers
    #    (credential stuffing) before we even look at which account it's
    #    aimed at.
    if not limiter.allow("login_ip", client_ip, max_calls=cfg.login_ip_max_attempts,
                          window_seconds=cfg.login_ip_window_seconds):
        raise RateLimitedError("Too many login attempts. Try again later.")

    try:
        normalized = normalize_email(email)
    except InvalidEmail:
        # Not "invalid email format" here - that would leak information
        # about the input relative to a real registered address. Fall
        # through to the same generic failure as a wrong password.
        _record_login_failure(db, actor="unknown", note="malformed email")
        raise AuthError(GENERIC_LOGIN_FAILURE) from None

    # 2) Short, soft per-(IP, email) throttle. Deliberately never keyed by
    #    email alone - see the module docstring - so an attacker without
    #    the victim's IP can throttle only themselves, never the victim's
    #    own attempts from their own device. `\x1f` (ASCII unit separator)
    #    joins the two halves: it cannot appear in a parsed IP or a
    #    normalized email, so no (ip, email) pair can collide with another.
    pair_key = f"{client_ip}\x1f{normalized}"
    if not limiter.allow("login_pair", pair_key, max_calls=cfg.login_max_attempts,
                          window_seconds=cfg.login_window_seconds):
        raise RateLimitedError("Too many login attempts. Try again later.")

    user = store.get_user_by_email(db, normalized)
    if user is None:
        # Constant-shape failure: verify against a fixed dummy hash so a
        # "no such account" login takes about as long as a "wrong password"
        # one, rather than returning instantly and leaking existence via
        # timing (§A4, §A11 "generic login failure").
        verify_password(password, _DUMMY_HASH)
        _record_login_failure(db, actor="unknown", note="no such account")
        raise AuthError(GENERIC_LOGIN_FAILURE)

    if user["status"] != AccountStatus.ACTIVE.value:
        verify_password(password, user["password_hash"])  # constant-shape, see above
        _record_login_failure(db, actor=user["user_id"], note="account disabled")
        raise AuthError(GENERIC_LOGIN_FAILURE)

    if not verify_password(password, user["password_hash"]):
        _record_login_failure(db, actor=user["user_id"], note="wrong password")
        raise AuthError(GENERIC_LOGIN_FAILURE)

    # Only the targeted-guess budget resets on success - the per-IP budget
    # is a volume control unrelated to whether any one attempt succeeded,
    # and must keep counting so an attacker who eventually guesses right
    # against one of many accounts cannot use that to reset their own
    # room to keep guessing against the rest.
    limiter.reset("login_pair", pair_key)
    store.set_last_login(db, user["user_id"], now=now)

    raw_token = store.new_session_token()
    raw_csrf = store.new_csrf_token()
    session_id = store.create_session(
        db, user_id=user["user_id"], token_hash=store.hash_token(raw_token),
        csrf_token_hash=store.hash_token(raw_csrf), ttl_seconds=cfg.session_ttl_seconds, now=now,
    )
    audit.record(db, actor=user["user_id"], action="login_success",
                target_type="user_account", target_id=user["user_id"])

    from datetime import timedelta
    return LoginResult(
        user_id=user["user_id"], session_id=session_id, raw_token=raw_token,
        raw_csrf_token=raw_csrf, expires_at=now + timedelta(seconds=cfg.session_ttl_seconds),
    )


def logout(db: Database, *, session_id: str, user_id: str) -> None:
    store.revoke_session(db, session_id)
    audit.record(db, actor=user_id, action="logout", target_type="user_account", target_id=user_id)


@dataclass(frozen=True, slots=True)
class SessionContext:
    user_id: str
    session_id: str
    csrf_token_hash: str


def validate_session(db: Database, *, raw_token: str, now: datetime | None = None) -> SessionContext | None:
    """``None`` for anything not a currently-valid session: unknown token,
    expired, revoked, or an account that has since been disabled — a
    session for a disabled account is never honoured even if it has not
    expired (§A4, §A11 "disabled account")."""
    if not raw_token:
        return None
    now = now or datetime.now(timezone.utc)
    session = store.get_session_by_token_hash(db, store.hash_token(raw_token))
    if session is None:
        return None
    if session["revoked_at"] is not None:
        return None
    if datetime.fromisoformat(session["expires_at"]) <= now:
        return None
    user = store.get_user(db, session["user_id"])
    if user is None or user["status"] != AccountStatus.ACTIVE.value:
        return None
    store.touch_session(db, session["session_id"], now=now)
    return SessionContext(user_id=user["user_id"], session_id=session["session_id"],
                          csrf_token_hash=session["csrf_token_hash"])


def _record_login_failure(db: Database, *, actor: str, note: str) -> None:
    audit.record(db, actor=actor, action="login_failure", target_type="user_account",
                target_id=actor, note=note)


# A fixed, precomputed Argon2id hash of an unguessable value - used only to
# keep a "no such account" login's timing shaped like a real verify (§A4).
_DUMMY_HASH = hash_password("correct-horse-battery-staple-unused")
