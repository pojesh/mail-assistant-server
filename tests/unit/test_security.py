"""Token encryption, session tokens, CSRF, OAuth consent URL."""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.errors import NeedsReconnect
from app.google import oauth
from app.main import app
from app.security.crypto import decrypt, encrypt
from app.security.session import create_signed_token, read_signed_token


def test_encrypt_round_trip_and_hides_plain_text() -> None:
    secret = "ya29.secret-token"
    stored = encrypt(secret)
    assert secret not in stored
    assert decrypt(stored) == secret


def test_decrypt_garbage_needs_reconnect() -> None:
    with pytest.raises(NeedsReconnect):
        decrypt("not-a-fernet-token")


def test_signed_token_round_trip() -> None:
    token = create_signed_token({"sub": "user-1"}, timedelta(minutes=5))
    claims = read_signed_token(token)
    assert claims is not None and claims["sub"] == "user-1"


def test_signed_token_rejects_tampering_and_expiry() -> None:
    forged = jwt.encode(
        {"sub": "user-1"}, "another-secret-that-is-long-enough-123", algorithm="HS256"
    )
    assert read_signed_token(forged) is None
    expired = create_signed_token({"sub": "user-1"}, timedelta(seconds=-1))
    assert read_signed_token(expired) is None
    assert read_signed_token("garbage") is None


def test_consent_url_has_offline_access_and_pkce() -> None:
    consent = oauth.create_consent_url()
    query = parse_qs(urlparse(consent.url).query)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [consent.state]
    assert query["redirect_uri"] == [settings.oauth_redirect_uri]
    assert oauth.GMAIL_SCOPE in query["scope"][0]
    assert len(consent.code_verifier) >= 43


def test_missing_scopes() -> None:
    assert oauth.missing_scopes(oauth.SCOPES) == set()
    assert oauth.missing_scopes(["openid"]) == oauth.REQUIRED_SCOPES


@pytest.fixture
async def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_me_requires_session(client: AsyncClient) -> None:
    response = await client.get("/api/me")
    assert response.status_code == 401
    assert response.json() == {"error": {"code": "unauthorized", "message": "Please sign in."}}


async def test_post_without_csrf_header_is_forbidden(client: AsyncClient) -> None:
    client.cookies.set("csrf_token", "abc")
    response = await client.post("/api/sync")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_login_redirects_to_google_and_sets_state_cookie(client: AsyncClient) -> None:
    response = await client.get("/api/auth/google/login")
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://accounts.google.com/")
    assert "oauth_state=" in response.headers["set-cookie"]


async def test_callback_with_bad_state_redirects_with_error(client: AsyncClient) -> None:
    response = await client.get("/api/auth/google/callback?code=x&state=wrong")
    assert response.status_code == 303
    assert response.headers["location"].endswith("/?auth_error=invalid_state")


async def test_callback_when_user_cancels(client: AsyncClient) -> None:
    response = await client.get("/api/auth/google/callback?error=access_denied")
    assert response.headers["location"].endswith("/?auth_error=access_denied")
