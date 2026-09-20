"""V9 Google Sign-In + Account Lifecycle: OAuth/OIDC protocol validation,
the account-linking policy (Cases A-G), password change/reset, and account
export/deletion.

No real Google credentials exist in this sandbox (see the final report) -
every Google-facing call is exercised through an injected fake
:class:`~detoura.providers.http.HttpClient`, never the network. This
mirrors exactly how this repository already tests Stripe/Resend/Duffel:
architecture and integration logic verified directly, live-provider E2E
honestly classified as blocked by credentials.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from detoura.api.app import create_app
from detoura.google_auth_config import GoogleAuthConfig
from detoura.models.account import AccountStatus
from detoura.persistence import accounts as store
from detoura.persistence.db import Database
from detoura.providers.communication_provider import CommunicationSendResult
from detoura.providers.http import HttpResponse
from detoura.services import account_lifecycle_service, google_auth_service, password_service
from detoura.services.account_lifecycle_service import AccountLifecycleError
from detoura.services.google_auth_service import (
    GoogleAuthDisabled,
    GoogleAuthError,
    GoogleLinkConflict,
    GoogleLinkRequired,
)
from detoura.services.password_service import PasswordServiceError, RateLimitedError
from detoura.services.rate_limit import rate_limiter

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
CLIENT_ID = "test-client-id.apps.googleusercontent.com"


def _db() -> Database:
    return Database(":memory:")


def _cfg(**overrides) -> GoogleAuthConfig:
    base = dict(client_id=CLIENT_ID, redirect_uri="https://app.example.com/api/v1/auth/google/callback")
    base.update(overrides)
    return GoogleAuthConfig(**base)


@pytest.fixture(autouse=True)
def _google_secret(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret-not-real")
    yield


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    rate_limiter().clear()
    yield
    rate_limiter().clear()


class FakeGoogleHttp:
    """A fake :class:`HttpClient` standing in for Google's token + tokeninfo
    endpoints. ``claims`` is whatever the tokeninfo endpoint should return
    for the id_token this fake hands back from the token endpoint."""

    def __init__(self, *, claims: dict | None = None, token_status: int = 200, tokeninfo_status: int = 200):
        self.claims = claims or {}
        self.token_status = token_status
        self.tokeninfo_status = tokeninfo_status
        self.requests: list[tuple[str, str]] = []

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=10.0):
        self.requests.append((method, url))
        if "token" == url.rsplit("/", 1)[-1] and method == "POST":
            if self.token_status != 200:
                return HttpResponse(status=self.token_status, body="{}")
            return HttpResponse(status=200, body=json.dumps({"id_token": "fake.id.token", "access_token": "unused"}))
        if "tokeninfo" in url:
            if self.tokeninfo_status != 200:
                return HttpResponse(status=self.tokeninfo_status, body="{}")
            return HttpResponse(status=200, body=json.dumps(self.claims))
        raise AssertionError(f"unexpected call to {url}")


def _claims(*, sub="google-sub-1", email="ada@example.com", nonce="n1", exp=None, verified=True, aud=None, iss=None):
    exp = exp or int((NOW + timedelta(minutes=5)).timestamp())
    return {
        "iss": iss if iss is not None else "https://accounts.google.com",
        "aud": aud if aud is not None else CLIENT_ID,
        "sub": sub,
        "email": email,
        "email_verified": "true" if verified else "false",
        "nonce": nonce,
        "exp": exp,
    }


def _run_signin(db, *, claims, cfg=None, link_user_id=None):
    """Drives one full start->callback round trip against a fake Google.
    The nonce ``build_start_url`` actually generates always wins - nonce
    mismatch itself is covered directly against ``verify_id_token`` above,
    not through this end-to-end helper."""
    cfg = cfg or _cfg()
    url = google_auth_service.build_start_url(db, link_user_id=link_user_id, cfg=cfg, now=NOW)
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    state = query["state"][0]
    nonce = query["nonce"][0]
    http = FakeGoogleHttp(claims={**claims, "nonce": nonce})
    return google_auth_service.complete_google_callback(
        db, code="fake-code", state=state, cfg=cfg, http_client=http, now=NOW,
    )


# ======================================================================
# services/google_oauth.py — protocol-level validation
# ======================================================================
from detoura.services import google_oauth


def test_pkce_challenge_is_sha256_of_verifier():
    import base64
    import hashlib

    pair = google_oauth.generate_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(pair.verifier.encode()).digest()).rstrip(b"=").decode()
    assert pair.challenge == expected


def test_authorization_url_carries_pkce_state_and_nonce():
    url = google_oauth.build_authorization_url(
        client_id=CLIENT_ID, redirect_uri="https://x/cb", state="s1", nonce="n1", code_challenge="c1",
    )
    assert "code_challenge=c1" in url
    assert "code_challenge_method=S256" in url
    assert "state=s1" in url and "nonce=n1" in url
    assert "response_type=code" in url


def test_exchange_code_failure_status_raises():
    http = FakeGoogleHttp(token_status=400)
    with pytest.raises(google_oauth.GoogleOAuthError):
        google_oauth.exchange_code_for_tokens(
            http, code="c", client_id=CLIENT_ID, client_secret="s",
            redirect_uri="https://x/cb", code_verifier="v",
        )


def test_verify_id_token_rejects_wrong_issuer():
    http = FakeGoogleHttp(claims=_claims(iss="https://evil.example.com"))
    with pytest.raises(google_oauth.GoogleOAuthError, match="issuer"):
        google_oauth.verify_id_token(http, id_token="t", expected_audience=CLIENT_ID, expected_nonce="n1", now=NOW)


def test_verify_id_token_rejects_wrong_audience():
    http = FakeGoogleHttp(claims=_claims(aud="someone-elses-client-id"))
    with pytest.raises(google_oauth.GoogleOAuthError, match="audience"):
        google_oauth.verify_id_token(http, id_token="t", expected_audience=CLIENT_ID, expected_nonce="n1", now=NOW)


def test_verify_id_token_rejects_expired_token():
    http = FakeGoogleHttp(claims=_claims(exp=int((NOW - timedelta(minutes=1)).timestamp())))
    with pytest.raises(google_oauth.GoogleOAuthError, match="expired"):
        google_oauth.verify_id_token(http, id_token="t", expected_audience=CLIENT_ID, expected_nonce="n1", now=NOW)


def test_verify_id_token_rejects_nonce_mismatch():
    http = FakeGoogleHttp(claims=_claims(nonce="wrong-nonce"))
    with pytest.raises(google_oauth.GoogleOAuthError, match="nonce"):
        google_oauth.verify_id_token(http, id_token="t", expected_audience=CLIENT_ID, expected_nonce="n1", now=NOW)


def test_verify_id_token_rejects_missing_subject():
    claims = _claims()
    claims["sub"] = ""
    http = FakeGoogleHttp(claims=claims)
    with pytest.raises(google_oauth.GoogleOAuthError, match="subject"):
        google_oauth.verify_id_token(http, id_token="t", expected_audience=CLIENT_ID, expected_nonce="n1", now=NOW)


def test_verify_id_token_accepts_valid_claims():
    http = FakeGoogleHttp(claims=_claims())
    identity = google_oauth.verify_id_token(http, id_token="t", expected_audience=CLIENT_ID, expected_nonce="n1", now=NOW)
    assert identity.subject == "google-sub-1"
    assert identity.email == "ada@example.com"
    assert identity.email_verified is True


def test_tokeninfo_failure_status_raises():
    http = FakeGoogleHttp(tokeninfo_status=400)
    with pytest.raises(google_oauth.GoogleOAuthError):
        google_oauth.verify_id_token(http, id_token="t", expected_audience=CLIENT_ID, expected_nonce="n1", now=NOW)


# ======================================================================
# services/google_auth_service.py — the account-linking policy (Cases A-G)
# ======================================================================
def test_google_auth_disabled_without_configuration():
    d = _db()
    with pytest.raises(GoogleAuthDisabled):
        google_auth_service.build_start_url(d, cfg=GoogleAuthConfig())  # no client_id/redirect_uri


def test_case_a_brand_new_identity_creates_passwordless_account():
    d = _db()
    result = _run_signin(d, claims=_claims())
    user = store.get_user(d, result.user_id)
    assert user["password_hash"] is None
    assert user["email_normalized"] == "ada@example.com"
    identity = store.get_identity_by_subject(d, provider="google", provider_subject="google-sub-1")
    assert identity is not None and identity["user_id"] == result.user_id


def test_case_b_returning_identity_logs_into_same_account_and_refreshes_email():
    d = _db()
    first = _run_signin(d, claims=_claims())
    second = _run_signin(d, claims=_claims(email="ada-new@example.com"))
    assert first.user_id == second.user_id
    # exactly one identity row - never a second account for the same subject
    identities = store.list_identities_for_user(d, first.user_id)
    assert len(identities) == 1
    assert identities[0]["provider_email"] == "ada-new@example.com"
    # the account's own email (the login key) is untouched by a Google email change
    assert store.get_user(d, first.user_id)["email_normalized"] == "ada@example.com"


def test_case_c_existing_password_account_same_email_is_never_silently_linked():
    d = _db()
    from detoura.services import auth_service

    uid = auth_service.register(d, email="ada@example.com", password="correct horse battery", now=NOW)
    with pytest.raises(GoogleLinkRequired) as excinfo:
        _run_signin(d, claims=_claims(email="ada@example.com"))
    assert excinfo.value.link_id
    # no identity was created, and the password account is unaffected
    assert store.get_identity_by_subject(d, provider="google", provider_subject="google-sub-1") is None
    assert store.get_user(d, uid)["password_hash"] is not None


def test_case_c_confirm_link_requires_matching_authenticated_email():
    d = _db()
    from detoura.services import auth_service

    auth_service.register(d, email="ada@example.com", password="correct horse battery", now=NOW)
    other_uid = auth_service.register(d, email="mallory@example.com", password="another password 1", now=NOW)
    with pytest.raises(GoogleLinkRequired) as excinfo:
        _run_signin(d, claims=_claims(email="ada@example.com"))
    link_id = excinfo.value.link_id

    # Mallory (a different, currently-authenticated account) may NOT redeem
    # a link ticket meant for ada@example.com.
    with pytest.raises(GoogleAuthError, match="does not match"):
        google_auth_service.confirm_pending_link(d, link_id=link_id, session_user_id=other_uid, now=NOW)


def test_case_c_confirm_link_succeeds_for_the_matching_account_then_google_login_reaches_it():
    d = _db()
    from detoura.services import auth_service

    uid = auth_service.register(d, email="ada@example.com", password="correct horse battery", now=NOW)
    with pytest.raises(GoogleLinkRequired) as excinfo:
        _run_signin(d, claims=_claims(email="ada@example.com"))
    link_id = excinfo.value.link_id

    google_auth_service.confirm_pending_link(d, link_id=link_id, session_user_id=uid, now=NOW)
    identity = store.get_identity_by_subject(d, provider="google", provider_subject="google-sub-1")
    assert identity is not None and identity["user_id"] == uid

    # a link ticket is single-use
    with pytest.raises(GoogleAuthError):
        google_auth_service.confirm_pending_link(d, link_id=link_id, session_user_id=uid, now=NOW)

    # Google sign-in now reaches the SAME password account (Case B from here on)
    result = _run_signin(d, claims=_claims(email="ada@example.com", sub="google-sub-1"))
    assert result.user_id == uid


def test_case_f_authenticated_user_connects_google_directly_no_email_check():
    d = _db()
    from detoura.services import auth_service

    uid = auth_service.register(d, email="ada@example.com", password="correct horse battery", now=NOW)
    # Deliberately a different Google email than the account's own - Case F
    # is an explicit authenticated action, not an email-matching decision.
    result = _run_signin(d, claims=_claims(email="totally-different@example.com"), link_user_id=uid)
    assert result.user_id == uid
    identity = store.get_identity_by_subject(d, provider="google", provider_subject="google-sub-1")
    assert identity["user_id"] == uid


def test_case_g_conflict_never_transfers_ownership():
    d = _db()
    from detoura.services import auth_service

    user_a = auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    user_b = auth_service.register(d, email="b@example.com", password="another password 1", now=NOW)
    _run_signin(d, claims=_claims(sub="shared-sub"), link_user_id=user_a)

    with pytest.raises(GoogleLinkConflict):
        _run_signin(d, claims=_claims(sub="shared-sub", email="b@example.com"), link_user_id=user_b)

    identity = store.get_identity_by_subject(d, provider="google", provider_subject="shared-sub")
    assert identity["user_id"] == user_a  # unchanged


def test_unverified_email_is_rejected():
    d = _db()
    with pytest.raises(GoogleAuthError, match="verified email"):
        _run_signin(d, claims=_claims(verified=False))
    assert store.get_identity_by_subject(d, provider="google", provider_subject="google-sub-1") is None


def test_disabled_account_cannot_google_login_even_with_valid_identity():
    d = _db()
    result = _run_signin(d, claims=_claims())
    store.set_status(d, result.user_id, "DISABLED", now=NOW)
    with pytest.raises(GoogleAuthError):
        _run_signin(d, claims=_claims())


def test_oauth_state_is_single_use():
    d = _db()
    cfg = _cfg()
    url = google_auth_service.build_start_url(d, cfg=cfg, now=NOW)
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    state, nonce = query["state"][0], query["nonce"][0]
    http = FakeGoogleHttp(claims=_claims(nonce=nonce))
    google_auth_service.complete_google_callback(d, code="c", state=state, cfg=cfg, http_client=http, now=NOW)
    with pytest.raises(GoogleAuthError, match="invalid or expired"):
        google_auth_service.complete_google_callback(d, code="c", state=state, cfg=cfg, http_client=http, now=NOW)


def test_oauth_state_expires():
    d = _db()
    cfg = _cfg(pending_ttl_seconds=60)
    url = google_auth_service.build_start_url(d, cfg=cfg, now=NOW)
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    state, nonce = query["state"][0], query["nonce"][0]
    http = FakeGoogleHttp(claims=_claims(nonce=nonce))
    later = NOW + timedelta(seconds=120)
    with pytest.raises(GoogleAuthError, match="expired"):
        google_auth_service.complete_google_callback(d, code="c", state=state, cfg=cfg, http_client=http, now=later)


def test_invalid_state_is_rejected():
    d = _db()
    cfg = _cfg()
    http = FakeGoogleHttp(claims=_claims())
    with pytest.raises(GoogleAuthError, match="invalid or expired"):
        google_auth_service.complete_google_callback(d, code="c", state="not-a-real-state", cfg=cfg, http_client=http, now=NOW)


def test_password_login_never_reveals_google_only_account_exists():
    """A Google-only account attempting password login must fail with the
    exact same generic message as any other invalid-credentials case."""
    d = _db()
    from detoura.services import auth_service

    result = _run_signin(d, claims=_claims())
    with pytest.raises(auth_service.AuthError, match=auth_service.GENERIC_LOGIN_FAILURE):
        auth_service.login(d, email="ada@example.com", password="whatever-guess-1", now=NOW)
    # identical shape to a wrong-password case against a real password account
    other = auth_service.register(d, email="other@example.com", password="correct horse battery", now=NOW)
    msg = None
    try:
        auth_service.login(d, email="other@example.com", password="wrong-one-here", now=NOW)
    except auth_service.AuthError as e:
        msg = str(e)
    assert msg == auth_service.GENERIC_LOGIN_FAILURE


# ======================================================================
# services/password_service.py — change / reset (§9, §10, §11)
# ======================================================================
class RecordingProvider:
    name = "recording"

    def __init__(self):
        self.sent = []

    def capabilities(self):
        from detoura.providers.communication_provider import CommunicationProviderCapabilities

        return CommunicationProviderCapabilities(supports_delivery_events=False, supports_idempotency_keys=True, max_retries_recommended=0)

    def send(self, *, idempotency_key, recipient, subject, body_text, reference):
        self.sent.append({"recipient": recipient, "subject": subject, "body_text": body_text})
        return CommunicationSendResult(ok=True, unknown=False, provider_message_id="rec_1", status="success")

    def retrieve(self, *, provider_message_id):
        raise NotImplementedError


def test_change_password_success_revokes_other_sessions_and_reissues_current():
    d = _db()
    from detoura.services import auth_service

    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    login1 = auth_service.login(d, email="a@example.com", password="correct horse battery", now=NOW)
    login2 = auth_service.login(d, email="a@example.com", password="correct horse battery", now=NOW)

    new_login = password_service.change_password(
        d, user_id=login1.user_id, current_password="correct horse battery",
        new_password="a brand new password 99", now=NOW,
    )
    assert auth_service.validate_session(d, raw_token=login1.raw_token, now=NOW) is None
    assert auth_service.validate_session(d, raw_token=login2.raw_token, now=NOW) is None
    assert auth_service.validate_session(d, raw_token=new_login.raw_token, now=NOW) is not None
    assert auth_service.login(d, email="a@example.com", password="a brand new password 99", now=NOW)


def test_change_password_wrong_current_password_rejected():
    d = _db()
    from detoura.services import auth_service

    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    uid = auth_service.login(d, email="a@example.com", password="correct horse battery", now=NOW).user_id
    with pytest.raises(PasswordServiceError, match="incorrect"):
        password_service.change_password(d, user_id=uid, current_password="wrong", new_password="another one 123", now=NOW)


def test_change_password_on_google_only_account_gives_distinct_error():
    d = _db()
    result = _run_signin(d, claims=_claims())
    with pytest.raises(PasswordServiceError, match="forgot password"):
        password_service.change_password(
            d, user_id=result.user_id, current_password="anything", new_password="a new password 123", now=NOW,
        )


def test_change_password_rate_limited():
    d = _db()
    from detoura.services import auth_service

    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    uid = auth_service.login(d, email="a@example.com", password="correct horse battery", now=NOW).user_id
    cfg = auth_config_with(password_change_max_attempts=3)
    for _ in range(3):
        with pytest.raises(PasswordServiceError):
            password_service.change_password(d, user_id=uid, current_password="wrong", new_password="another one 123", now=NOW, cfg=cfg)
    with pytest.raises(RateLimitedError):
        password_service.change_password(d, user_id=uid, current_password="wrong", new_password="another one 123", now=NOW, cfg=cfg)


def auth_config_with(**overrides):
    from detoura.auth_config import AuthConfig

    return AuthConfig(**overrides)


def test_reset_request_unknown_email_sends_nothing_and_creates_no_token():
    d = _db()
    provider = RecordingProvider()
    password_service.request_password_reset(d, email="nobody@example.com", now=NOW, provider=provider)
    assert provider.sent == []


def test_reset_request_known_account_sends_email_and_creates_hashed_token():
    d = _db()
    from detoura.services import auth_service

    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    provider = RecordingProvider()
    password_service.request_password_reset(d, email="A@Example.com", now=NOW, provider=provider)
    assert len(provider.sent) == 1
    assert provider.sent[0]["recipient"] == "a@example.com"
    # the raw token is embedded in the body but never persisted in plaintext
    body = provider.sent[0]["body_text"]
    assert "Reset code:" in body
    raw_token = body.split("Reset code:")[1].split("\n")[0].strip()
    row = store.get_reset_token(d, store.hash_token(raw_token))
    assert row is not None
    assert raw_token not in json.dumps(dict(row))


def test_reset_request_disabled_account_sends_nothing():
    d = _db()
    from detoura.services import auth_service

    uid = auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    store.set_status(d, uid, "DISABLED", now=NOW)
    provider = RecordingProvider()
    password_service.request_password_reset(d, email="a@example.com", now=NOW, provider=provider)
    assert provider.sent == []


def _request_and_get_token(d, email, now=NOW) -> str:
    provider = RecordingProvider()
    password_service.request_password_reset(d, email=email, now=now, provider=provider)
    body = provider.sent[0]["body_text"]
    return body.split("Reset code:")[1].split("\n")[0].strip()


def test_reset_confirm_success_updates_password_and_revokes_sessions_without_new_login():
    d = _db()
    from detoura.services import auth_service

    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    login = auth_service.login(d, email="a@example.com", password="correct horse battery", now=NOW)
    token = _request_and_get_token(d, "a@example.com")

    password_service.confirm_password_reset(d, token=token, new_password="brand new password 42", now=NOW)
    assert auth_service.validate_session(d, raw_token=login.raw_token, now=NOW) is None
    assert auth_service.login(d, email="a@example.com", password="brand new password 42", now=NOW)


def test_reset_confirm_sets_first_password_for_google_only_account():
    d = _db()
    from detoura.services import auth_service

    result = _run_signin(d, claims=_claims())
    token = _request_and_get_token(d, "ada@example.com")
    password_service.confirm_password_reset(d, token=token, new_password="a first password 1", now=NOW)
    assert auth_service.login(d, email="ada@example.com", password="a first password 1", now=NOW)


def test_reset_token_is_single_use():
    d = _db()
    from detoura.services import auth_service

    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    token = _request_and_get_token(d, "a@example.com")
    password_service.confirm_password_reset(d, token=token, new_password="brand new password 42", now=NOW)
    with pytest.raises(PasswordServiceError, match="invalid or has expired"):
        password_service.confirm_password_reset(d, token=token, new_password="another password 43", now=NOW)


def test_reset_token_expires():
    d = _db()
    from detoura.services import auth_service

    auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    cfg = auth_config_with(reset_token_ttl_seconds=60)
    provider = RecordingProvider()
    password_service.request_password_reset(d, email="a@example.com", now=NOW, cfg=cfg, provider=provider)
    token = provider.sent[0]["body_text"].split("Reset code:")[1].split("\n")[0].strip()
    later = NOW + timedelta(seconds=120)
    with pytest.raises(PasswordServiceError, match="invalid or has expired"):
        password_service.confirm_password_reset(d, token=token, new_password="brand new password 42", now=later)


def test_reset_confirm_garbage_token_rejected():
    d = _db()
    with pytest.raises(PasswordServiceError):
        password_service.confirm_password_reset(d, token="not-a-real-token", new_password="brand new password 42", now=NOW)


# ======================================================================
# services/account_lifecycle_service.py — export / deletion (§13)
# ======================================================================
def test_export_returns_account_identities_and_owned_trips():
    d = _db()
    from detoura.services import auth_service

    uid = auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    store.claim_trip(d, user_id=uid, booking_id="bkg_1", now=NOW)
    result = _run_signin(d, claims=_claims(email="a@example.com"), link_user_id=uid)
    data = account_lifecycle_service.export_account_data(d, user_id=uid)
    assert data["account"]["user_id"] == uid
    assert data["account"]["has_password"] is True
    assert data["owned_booking_ids"] == ["bkg_1"]
    assert data["linked_identities"][0]["provider"] == "google"
    assert "provider_subject" not in json.dumps(data)


def test_delete_account_scrubs_email_and_revokes_sessions_but_keeps_trip_ownership():
    d = _db()
    from detoura.services import auth_service

    uid = auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    login = auth_service.login(d, email="a@example.com", password="correct horse battery", now=NOW)
    store.claim_trip(d, user_id=uid, booking_id="bkg_1", now=NOW)
    _run_signin(d, claims=_claims(email="a@example.com"), link_user_id=uid)

    account_lifecycle_service.delete_account(d, user_id=uid, now=NOW)

    user = store.get_user(d, uid)
    assert user["status"] == AccountStatus.DELETED.value
    assert user["email_normalized"] != "a@example.com"
    assert user["password_hash"] is None
    assert auth_service.validate_session(d, raw_token=login.raw_token, now=NOW) is None
    assert store.list_identities_for_user(d, uid) == []
    # financial/booking truth is never touched by deletion
    assert store.list_trip_ids_for_user(d, uid) == ["bkg_1"]

    # re-registering with the same email now works (deletion actually freed it)
    new_uid = auth_service.register(d, email="a@example.com", password="another password 123", now=NOW)
    assert new_uid != uid

    # the old, deleted account can never log in again, generically
    with pytest.raises(auth_service.AuthError):
        auth_service.login(d, email="a@example.com", password="correct horse battery", now=NOW)


def test_export_and_delete_reject_already_deleted_account():
    d = _db()
    from detoura.services import auth_service

    uid = auth_service.register(d, email="a@example.com", password="correct horse battery", now=NOW)
    account_lifecycle_service.delete_account(d, user_id=uid, now=NOW)
    with pytest.raises(AccountLifecycleError):
        account_lifecycle_service.delete_account(d, user_id=uid, now=NOW)
    with pytest.raises(AccountLifecycleError):
        account_lifecycle_service.export_account_data(d, user_id=uid)


# ======================================================================
# HTTP API — cookies, CSRF, session integration
# ======================================================================
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret-not-real")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://app.example.com/api/v1/auth/google/callback")
    import detoura.google_auth_config as gcfg
    gcfg.reset_google_auth_config()
    import detoura.persistence.db as _db_mod
    monkeypatch.setattr(_db_mod, "_DB", None)
    return TestClient(create_app())


def _extract_state_nonce(location: str) -> tuple[str, str]:
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(location).query)
    return q["state"][0], q["nonce"][0]


def test_google_start_disabled_returns_503(tmp_path, monkeypatch):
    monkeypatch.setenv("DETOURA_DB_PATH", str(tmp_path / "auth2.db"))
    import detoura.google_auth_config as gcfg
    gcfg.reset_google_auth_config()
    import detoura.persistence.db as _db_mod
    monkeypatch.setattr(_db_mod, "_DB", None)
    c = TestClient(create_app())
    assert c.get("/api/v1/auth/google/start", follow_redirects=False).status_code == 503


def test_google_start_redirects_to_google(client):
    r = client.get("/api/v1/auth/google/start", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://accounts.google.com/")


def test_google_callback_new_account_sets_cookies_and_logs_in(client, monkeypatch):
    r = client.get("/api/v1/auth/google/start", follow_redirects=False)
    state, nonce = _extract_state_nonce(r.headers["location"])
    # The API layer resolves `now` itself (real wall-clock time), unlike the
    # direct-service tests above which inject a fixed NOW - the token's exp
    # must be relative to the real clock here.
    real_exp = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    fake = FakeGoogleHttp(claims=_claims(nonce=nonce, exp=real_exp))
    import detoura.api.auth_google as auth_google_module

    monkeypatch.setattr(auth_google_module, "UrllibHttpClient", lambda: fake)
    cb = client.get(f"/api/v1/auth/google/callback?code=fake&state={state}", follow_redirects=False)
    assert cb.status_code == 302
    assert "google_auth=success" in cb.headers["location"]
    assert client.cookies.get("detoura_session") is not None
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["email_normalized"] == "ada@example.com"


def test_google_callback_link_required_redirects_with_link_id(client, monkeypatch):
    client.post("/api/v1/auth/register", json={"email": "ada@example.com", "password": "correct horse battery"})
    r = client.get("/api/v1/auth/google/start", follow_redirects=False)
    state, nonce = _extract_state_nonce(r.headers["location"])
    real_exp = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    fake = FakeGoogleHttp(claims=_claims(nonce=nonce, email="ada@example.com", exp=real_exp))
    import detoura.api.auth_google as auth_google_module

    monkeypatch.setattr(auth_google_module, "UrllibHttpClient", lambda: fake)
    cb = client.get(f"/api/v1/auth/google/callback?code=fake&state={state}", follow_redirects=False)
    assert "google_link_required=1" in cb.headers["location"]
    assert "link_id=" in cb.headers["location"]
    assert client.cookies.get("detoura_session") is None


def test_google_link_start_and_confirm_require_session_and_csrf(client, monkeypatch):
    assert client.post("/api/v1/auth/google/link/start").status_code == 401
    client.post("/api/v1/auth/register", json={"email": "ada@example.com", "password": "correct horse battery"})
    client.post("/api/v1/auth/login", json={"email": "ada@example.com", "password": "correct horse battery"})
    assert client.post("/api/v1/auth/google/link/start").status_code == 403  # no CSRF header
    csrf = client.cookies.get("detoura_csrf")
    r = client.post("/api/v1/auth/google/link/start", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and "authorization_url" in r.json()


def test_password_change_api_reissues_cookies(client):
    client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "correct horse battery"})
    client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "correct horse battery"})
    old_session = client.cookies.get("detoura_session")
    csrf = client.cookies.get("detoura_csrf")
    r = client.post("/api/v1/auth/password/change", headers={"X-CSRF-Token": csrf},
                    json={"current_password": "correct horse battery", "new_password": "a brand new password 9"})
    assert r.status_code == 200
    assert client.cookies.get("detoura_session") != old_session
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json() is not None


def test_password_change_api_requires_csrf(client):
    client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "correct horse battery"})
    client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "correct horse battery"})
    r = client.post("/api/v1/auth/password/change",
                    json={"current_password": "correct horse battery", "new_password": "a brand new password 9"})
    assert r.status_code == 403


def test_password_reset_request_api_always_200_same_shape(client):
    r1 = client.post("/api/v1/auth/password/reset/request", json={"email": "nobody@example.com"})
    r2 = client.post("/api/v1/auth/password/reset/request", json={"email": "also-nobody@example.com"})
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()


def test_account_export_and_delete_api(client):
    client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "correct horse battery"})
    client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "correct horse battery"})
    export = client.get("/api/v1/auth/account/export")
    assert export.status_code == 200 and export.json()["account"]["email"] == "a@example.com"

    csrf = client.cookies.get("detoura_csrf")
    # wrong password confirmation refused
    bad = client.post("/api/v1/auth/account/delete", headers={"X-CSRF-Token": csrf},
                      json={"current_password": "wrong"})
    assert bad.status_code == 400

    ok = client.post("/api/v1/auth/account/delete", headers={"X-CSRF-Token": csrf},
                     json={"current_password": "correct horse battery"})
    assert ok.status_code == 200
    assert client.get("/api/v1/auth/me").json() is None


def test_account_delete_api_requires_csrf(client):
    client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "correct horse battery"})
    client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "correct horse battery"})
    r = client.post("/api/v1/auth/account/delete", json={"current_password": "correct horse battery"})
    assert r.status_code == 403


# ======================================================================
# Schema migration — an existing (pre-Google-auth) database has
# `password_hash NOT NULL`; this must upgrade in place without data loss.
# ======================================================================
def test_existing_database_with_not_null_password_hash_migrates_cleanly(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE user_accounts ("
        " user_id TEXT PRIMARY KEY, email_normalized TEXT NOT NULL UNIQUE,"
        " password_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE',"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_login_at TEXT)"
    )
    conn.execute(
        "INSERT INTO user_accounts (user_id, email_normalized, password_hash, status,"
        " created_at, updated_at) VALUES ('usr_legacy', 'legacy@example.com', 'h1', 'ACTIVE',"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    # Opening it through Database must run the migration and preserve the row.
    d = Database(path)
    user = store.get_user(d, "usr_legacy")
    assert user["email_normalized"] == "legacy@example.com"
    assert user["password_hash"] == "h1"
    cols = {r["name"]: r["notnull"] for r in d.query("PRAGMA table_info(user_accounts)")}
    assert cols["password_hash"] == 0  # now nullable

    # And a Google-only (NULL password_hash) account can now be created.
    uid = store.create_user(d, email_normalized="google-only@example.com", password_hash=None, now=NOW)
    assert store.get_user(d, uid)["password_hash"] is None
    d.close()
