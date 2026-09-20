"""Google Sign-In HTTP API (V9 Google auth).

    GET  /api/v1/auth/google/start        - begin sign-in, 302 to Google
    GET  /api/v1/auth/google/callback     - Google's redirect target
    POST /api/v1/auth/google/link/start   - authenticated: begin connecting Google
    POST /api/v1/auth/google/link/confirm - authenticated: complete a Case-C link

The two GETs are full-page browser navigations (Google itself only ever
redirects a browser, never issues a fetch/XHR), so there is no CSRF check
on them - the OAuth ``state`` parameter is exactly the mechanism that
plays that role here, standard for this flow. Both authenticated POSTs use
the same session+CSRF machinery every other mutating account endpoint
does.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..auth_config import auth_config
from ..google_auth_config import google_auth_config
from ..observability import log_event
from ..persistence import get_db
from ..providers.http import UrllibHttpClient
from ..services import google_auth_service
from ..services.google_auth_service import (
    GoogleAuthDisabled,
    GoogleAuthError,
    GoogleLinkConflict,
    GoogleLinkRequired,
)
from ..services.rate_limit import rate_limiter
from .auth import _client_ip, _set_auth_cookies, require_csrf, require_session

router = APIRouter(prefix="/api/v1/auth/google", tags=["auth", "google"])
_logger = logging.getLogger("detoura.api.auth_google")

#: Coarse per-IP volume controls on the two Google-facing hops - not the
#: primary security control (state/PKCE/nonce are), but cheap
#: defense-in-depth against blind flooding (§14).
_START_MAX_ATTEMPTS = 30
_START_WINDOW_SECONDS = 300.0
_CALLBACK_MAX_ATTEMPTS = 30
_CALLBACK_WINDOW_SECONDS = 300.0
#: The linking endpoints are authenticated (session required), so these are
#: keyed per user_id rather than per IP - bounding a caller with a live
#: session from hammering either operation, even though `link_id` (128 bits
#: of entropy) is not realistically brute-forceable regardless (§14).
_LINK_START_MAX_ATTEMPTS = 20
_LINK_START_WINDOW_SECONDS = 3600.0
_LINK_CONFIRM_MAX_ATTEMPTS = 20
_LINK_CONFIRM_WINDOW_SECONDS = 3600.0


def _rate_limited(request: Request, *, scope: str, max_calls: int, window_seconds: float) -> bool:
    return not rate_limiter().allow(scope, _client_ip(request), max_calls=max_calls, window_seconds=window_seconds)


def _user_rate_limited(user_id: str, *, scope: str, max_calls: int, window_seconds: float) -> bool:
    return not rate_limiter().allow(scope, user_id, max_calls=max_calls, window_seconds=window_seconds)


@router.get("/start")
def google_start(request: Request):
    if _rate_limited(request, scope="google_oauth_start", max_calls=_START_MAX_ATTEMPTS,
                      window_seconds=_START_WINDOW_SECONDS):
        raise HTTPException(status_code=429, detail={"message": "Too many attempts. Try again later."})
    try:
        url = google_auth_service.build_start_url(get_db())
    except GoogleAuthDisabled as exc:
        raise HTTPException(status_code=503, detail={"message": "Google Sign-In is not available."}) from exc
    return RedirectResponse(url, status_code=302)


def _redirect_with(base_url: str, **params: str) -> RedirectResponse:
    from urllib.parse import urlencode

    sep = "&" if "?" in base_url else "?"
    return RedirectResponse(f"{base_url}{sep}{urlencode(params)}", status_code=302)


@router.get("/callback")
def google_callback(request: Request, response: Response, code: str = "", state: str = ""):
    cfg = google_auth_config()
    if _rate_limited(request, scope="google_oauth_callback", max_calls=_CALLBACK_MAX_ATTEMPTS,
                      window_seconds=_CALLBACK_WINDOW_SECONDS):
        raise HTTPException(status_code=429, detail={"message": "Too many attempts. Try again later."})
    if not code or not state:
        return _redirect_with(cfg.post_login_redirect_url, google_auth="error", reason="missing_parameters")

    db = get_db()
    try:
        result = google_auth_service.complete_google_callback(
            db, code=code, state=state, cfg=cfg, http_client=UrllibHttpClient(),
        )
    except GoogleLinkRequired as exc:
        return _redirect_with(cfg.post_login_redirect_url, google_link_required="1", link_id=exc.link_id)
    except GoogleLinkConflict:
        log_event(_logger, "google_auth_outcome", level=logging.WARNING, outcome="link_conflict")
        return _redirect_with(cfg.post_login_redirect_url, google_auth="error", reason="link_conflict")
    except (GoogleAuthError, GoogleAuthDisabled) as exc:
        log_event(_logger, "google_auth_outcome", level=logging.WARNING, outcome="failed")
        return _redirect_with(cfg.post_login_redirect_url, google_auth="error", reason="failed")

    redirect = _redirect_with(cfg.post_login_redirect_url, google_auth="success")
    _set_auth_cookies(redirect, raw_token=result.raw_token, raw_csrf=result.raw_csrf_token,
                      max_age=auth_config().session_ttl_seconds)
    return redirect


class LinkConfirmRequest(BaseModel):
    link_id: str = Field(min_length=1, max_length=64)


@router.post("/link/start")
def google_link_start(request: Request):
    session = require_session(request)
    require_csrf(request, session)
    if _user_rate_limited(session.user_id, scope="google_link_start",
                          max_calls=_LINK_START_MAX_ATTEMPTS, window_seconds=_LINK_START_WINDOW_SECONDS):
        raise HTTPException(status_code=429, detail={"message": "Too many attempts. Try again later."})
    try:
        url = google_auth_service.build_start_url(get_db(), link_user_id=session.user_id)
    except GoogleAuthDisabled as exc:
        raise HTTPException(status_code=503, detail={"message": "Google Sign-In is not available."}) from exc
    return {"authorization_url": url}


@router.post("/link/confirm")
def google_link_confirm(body: LinkConfirmRequest, request: Request):
    session = require_session(request)
    require_csrf(request, session)
    if _user_rate_limited(session.user_id, scope="google_link_confirm",
                          max_calls=_LINK_CONFIRM_MAX_ATTEMPTS, window_seconds=_LINK_CONFIRM_WINDOW_SECONDS):
        raise HTTPException(status_code=429, detail={"message": "Too many attempts. Try again later."})
    try:
        google_auth_service.confirm_pending_link(get_db(), link_id=body.link_id, session_user_id=session.user_id)
    except GoogleLinkConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc)}) from exc
    except GoogleAuthError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc
    return {"ok": True}
