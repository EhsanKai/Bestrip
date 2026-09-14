"""Account authentication API (V9 Phase 2.6 §A4-§A7).

    POST /api/v1/auth/register
    POST /api/v1/auth/login
    POST /api/v1/auth/logout
    GET  /api/v1/auth/me

Server-side sessions in an ``HttpOnly`` cookie (§A5); a separate,
JS-readable CSRF cookie backs the double-submit check on mutating requests
(§A6). Anonymous callers of every other Detoura endpoint are entirely
unaffected — nothing here is on the search/booking path.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth_config import auth_config
from ..persistence import accounts as store
from ..persistence import get_db
from ..services import auth_service
from ..services.auth_service import AuthError, RateLimitedError, SessionContext
from ..services.client_ip import resolve_client_ip

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1000)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1000)


class MeResponse(BaseModel):
    user_id: str
    email_normalized: str
    status: str
    created_at: str
    last_login_at: str | None = None


def _set_auth_cookies(response: Response, *, raw_token: str, raw_csrf: str, max_age: int) -> None:
    cfg = auth_config()
    response.set_cookie(
        key=cfg.session_cookie_name, value=raw_token, max_age=max_age,
        httponly=True, secure=cfg.is_production, samesite="lax", path="/",
    )
    # Deliberately NOT HttpOnly: the double-submit CSRF pattern requires
    # client-side JS to be able to read this value and echo it back as a
    # header - the session cookie above is what actually authenticates, so
    # this one carries no bearer capability of its own (§A6).
    response.set_cookie(
        key=cfg.csrf_cookie_name, value=raw_csrf, max_age=max_age,
        httponly=False, secure=cfg.is_production, samesite="lax", path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    cfg = auth_config()
    response.delete_cookie(cfg.session_cookie_name, path="/")
    response.delete_cookie(cfg.csrf_cookie_name, path="/")


def _client_ip(request: Request) -> str:
    """See ``services/client_ip.py`` for the trust-boundary rationale.
    ``AUTH_TRUSTED_PROXY_HOPS`` defaults to 0 (don't trust
    ``X-Forwarded-For``) - correct for local dev and any deployment without
    a reverse proxy Detoura controls."""
    cfg = auth_config()
    direct = request.client.host if request.client else None
    return resolve_client_ip(
        direct_peer=direct,
        forwarded_for=request.headers.get("X-Forwarded-For"),
        trusted_proxy_hops=cfg.trusted_proxy_hops,
    )


def get_optional_session(request: Request) -> SessionContext | None:
    cfg = auth_config()
    raw_token = request.cookies.get(cfg.session_cookie_name)
    if not raw_token:
        return None
    return auth_service.validate_session(get_db(), raw_token=raw_token)


def require_session(request: Request) -> SessionContext:
    ctx = get_optional_session(request)
    if ctx is None:
        raise HTTPException(status_code=401, detail={"message": "Sign in required."})
    return ctx


def require_csrf(request: Request, session: SessionContext) -> None:
    """Double-submit CSRF check (§A6): the header must be present and its
    hash must match the *specific session's* stored CSRF token — not merely
    "some cookie was present", which SameSite alone would already mostly
    cover and which this deliberately does not rely on solely."""
    cfg = auth_config()
    header_value = request.headers.get("X-CSRF-Token", "")
    cookie_value = request.cookies.get(cfg.csrf_cookie_name, "")
    if not header_value or not cookie_value or header_value != cookie_value:
        raise HTTPException(status_code=403, detail={"message": "CSRF token missing or invalid."})
    if store.hash_token(header_value) != session.csrf_token_hash:
        raise HTTPException(status_code=403, detail={"message": "CSRF token missing or invalid."})


@router.post("/register")
def register(body: RegisterRequest, request: Request) -> dict:
    db = get_db()
    try:
        user_id = auth_service.register(
            db, email=body.email, password=body.password, client_ip=_client_ip(request),
        )
    except RateLimitedError as exc:
        raise HTTPException(status_code=429, detail={"message": str(exc)}) from exc
    except AuthError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc
    return {"user_id": user_id}


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict:
    db = get_db()
    try:
        result = auth_service.login(
            db, email=body.email, password=body.password, client_ip=_client_ip(request),
        )
    except RateLimitedError as exc:
        raise HTTPException(status_code=429, detail={"message": str(exc)}) from exc
    except AuthError as exc:
        raise HTTPException(status_code=401, detail={"message": str(exc)}) from exc
    cfg = auth_config()
    _set_auth_cookies(response, raw_token=result.raw_token, raw_csrf=result.raw_csrf_token,
                      max_age=cfg.session_ttl_seconds)
    return {"user_id": result.user_id}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    session = get_optional_session(request)
    if session is None:
        # Logging out with no valid session is not an error - the caller's
        # goal (no active session) is already true.
        _clear_auth_cookies(response)
        return {"ok": True}
    require_csrf(request, session)
    auth_service.logout(get_db(), session_id=session.session_id, user_id=session.user_id)
    _clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me", response_model=MeResponse | None)
def me(request: Request):
    session = get_optional_session(request)
    if session is None:
        return None
    user = store.get_user(get_db(), session.user_id)
    if user is None:
        return None
    return MeResponse(
        user_id=user["user_id"], email_normalized=user["email_normalized"],
        status=user["status"], created_at=user["created_at"], last_login_at=user["last_login_at"],
    )
