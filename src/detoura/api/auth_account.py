"""Password change/reset + account export/deletion HTTP API (V9 Google auth
+ account lifecycle §9/§10/§13).

    POST /api/v1/auth/password/change         - authenticated
    POST /api/v1/auth/password/reset/request  - anonymous, always generic
    POST /api/v1/auth/password/reset/confirm  - anonymous (holds a token)
    GET  /api/v1/auth/account/export          - authenticated
    POST /api/v1/auth/account/delete          - authenticated

Every mutating, cookie-authenticated endpoint here goes through the same
``require_session`` + ``require_csrf`` pair ``/auth/logout`` already uses -
nothing new invented for this slice.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth_config import auth_config
from ..persistence import accounts as store
from ..persistence import get_db
from ..services import account_lifecycle_service, password_service
from ..services.account_lifecycle_service import AccountLifecycleError
from ..services.password_hashing import verify_password
from ..services.password_service import GENERIC_RESET_REQUEST_RESPONSE, PasswordServiceError, RateLimitedError
from .auth import _clear_auth_cookies, _client_ip, _set_auth_cookies, require_csrf, require_session

router = APIRouter(prefix="/api/v1/auth", tags=["auth", "account"])


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1000)
    new_password: str = Field(min_length=1, max_length=1000)


class ResetRequestRequest(BaseModel):
    email: str = Field(min_length=1, max_length=320)


class ResetConfirmRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=1000)


class DeleteAccountRequest(BaseModel):
    #: Required to confirm deletion for an account that has a password
    #: credential; ignored (and not required) for a Google-only account,
    #: which has none to check (§11).
    current_password: str | None = Field(default=None, max_length=1000)


@router.post("/password/change")
def change_password(body: ChangePasswordRequest, request: Request, response: Response):
    session = require_session(request)
    require_csrf(request, session)
    try:
        result = password_service.change_password(
            get_db(), user_id=session.user_id,
            current_password=body.current_password, new_password=body.new_password,
        )
    except RateLimitedError as exc:
        raise HTTPException(status_code=429, detail={"message": str(exc)}) from exc
    except PasswordServiceError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc
    _set_auth_cookies(response, raw_token=result.raw_token, raw_csrf=result.raw_csrf_token,
                      max_age=auth_config().session_ttl_seconds)
    return {"ok": True}


@router.post("/password/reset/request")
def request_password_reset(body: ResetRequestRequest, request: Request):
    password_service.request_password_reset(get_db(), email=body.email, client_ip=_client_ip(request))
    return {"message": GENERIC_RESET_REQUEST_RESPONSE}


@router.post("/password/reset/confirm")
def confirm_password_reset(body: ResetConfirmRequest, request: Request, response: Response):
    try:
        password_service.confirm_password_reset(
            get_db(), token=body.token, new_password=body.new_password, client_ip=_client_ip(request),
        )
    except RateLimitedError as exc:
        raise HTTPException(status_code=429, detail={"message": str(exc)}) from exc
    except PasswordServiceError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc
    # No new session is issued (§12 - see password_service module docstring);
    # clear any stale cookie the caller happened to still be holding.
    _clear_auth_cookies(response)
    return {"ok": True}


@router.get("/account/export")
def export_account(request: Request):
    session = require_session(request)
    try:
        return account_lifecycle_service.export_account_data(get_db(), user_id=session.user_id)
    except AccountLifecycleError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc


@router.post("/account/delete")
def delete_account(body: DeleteAccountRequest, request: Request, response: Response):
    session = require_session(request)
    require_csrf(request, session)
    db = get_db()
    user = store.get_user(db, session.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail={"message": "This account is not available."})
    if user["password_hash"] is not None:
        if not body.current_password or not verify_password(body.current_password, user["password_hash"]):
            raise HTTPException(status_code=400, detail={"message": "Current password is incorrect."})
    try:
        account_lifecycle_service.delete_account(db, user_id=session.user_id)
    except AccountLifecycleError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc
    _clear_auth_cookies(response)
    return {"ok": True}
