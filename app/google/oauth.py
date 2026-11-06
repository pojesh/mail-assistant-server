"""Google OAuth: build the consent URL, exchange the code, and keep access tokens fresh."""

import asyncio
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from pydantic import BaseModel
from sqlalchemy import select, update

from app.config import settings
from app.db import open_session
from app.errors import BadRequest, GoogleApiError, NeedsReconnect
from app.logging import get_logger
from app.models import OAuthToken, User
from app.security.crypto import decrypt, encrypt

# NOTE: Google may return scopes in a different order or fewer scopes than requested (users can
# untick boxes). We check the granted scopes ourselves, so oauthlib must not raise on a change.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

log = get_logger(__name__)

TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    GMAIL_SCOPE,
    CALENDAR_EVENTS_SCOPE,
    CALENDAR_READ_SCOPE,
]
REQUIRED_SCOPES = {GMAIL_SCOPE, CALENDAR_EVENTS_SCOPE, CALENDAR_READ_SCOPE}
REFRESH_MARGIN = timedelta(minutes=5)


class ConsentRequest(BaseModel):
    url: str
    state: str
    code_verifier: str


class GoogleTokens(BaseModel):
    """Result of a successful code exchange, including who signed in."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scopes: list[str]
    email: str
    name: str | None
    picture_url: str | None


def _build_flow() -> Flow:
    """OAuth flow configured with our web client and redirect URI."""
    client_config = {
        "web": {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": [settings.oauth_redirect_uri],
        }
    }
    flow: Flow = Flow.from_client_config(
        client_config, scopes=SCOPES, redirect_uri=settings.oauth_redirect_uri
    )
    return flow


def create_consent_url() -> ConsentRequest:
    """Google consent URL with offline access, forced consent, state and PKCE (S256)."""
    flow = _build_flow()
    flow.code_verifier = secrets.token_urlsafe(64)
    url, state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    return ConsentRequest(url=url, state=state, code_verifier=flow.code_verifier)


def _exchange_code_blocking(code: str, code_verifier: str) -> GoogleTokens:
    """Swap the authorization code for tokens and verify the ID token (blocking I/O)."""
    flow = _build_flow()
    flow.code_verifier = code_verifier
    token = flow.fetch_token(code=code)
    credentials = flow.credentials
    claims = id_token.verify_oauth2_token(
        credentials.id_token, GoogleRequest(), settings.google_oauth_client_id
    )
    if not claims.get("email_verified"):
        raise BadRequest("Your Google email address is not verified.")

    granted = token.get("scope", [])
    scopes = granted.split() if isinstance(granted, str) else list(granted)
    expiry = credentials.expiry or datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    return GoogleTokens(
        access_token=credentials.token,
        refresh_token=credentials.refresh_token,
        expires_at=expiry.replace(tzinfo=UTC),  # google-auth returns naive UTC datetimes
        scopes=scopes,
        email=str(claims["email"]).lower(),
        name=claims.get("name"),
        picture_url=claims.get("picture"),
    )


async def exchange_code(code: str, code_verifier: str) -> GoogleTokens:
    """Finish sign-in. Raises BadRequest with a safe message if Google rejects the code."""
    try:
        return await asyncio.to_thread(_exchange_code_blocking, code, code_verifier)
    except BadRequest:
        raise
    except Exception as error:  # oauthlib, google-auth and network errors all end up here
        log.warning("oauth_exchange_failed", error_type=type(error).__name__)
        raise BadRequest("Google sign-in failed. Please try again.") from error


def missing_scopes(scopes: list[str]) -> set[str]:
    """Required scopes the user did not grant."""
    return REQUIRED_SCOPES - set(scopes)


def _refresh_blocking(refresh_token: str) -> tuple[str, datetime]:
    """Use the refresh token to get a new access token (blocking I/O)."""
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
    )
    credentials.refresh(GoogleRequest())
    expiry = credentials.expiry or datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    return str(credentials.token), expiry.replace(tzinfo=UTC)


async def mark_needs_reconnect(user_id: uuid.UUID) -> None:
    """Flag the user so the UI shows the "Reconnect Google" banner."""
    async with open_session() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(status="needs_reconnect")
        )
        await session.commit()
    log.info("user_needs_reconnect", user_id=str(user_id))


async def get_valid_access_token(user_id: uuid.UUID) -> str:
    """Return an access token valid for at least 5 more minutes, refreshing it if needed."""
    async with open_session() as session:
        row = await session.scalar(select(OAuthToken).where(OAuthToken.user_id == user_id))
        if row is None:
            raise NeedsReconnect()
        if row.expires_at - REFRESH_MARGIN > datetime.now(UTC):
            return decrypt(row.access_token_enc)
        if not row.refresh_token_enc:
            await mark_needs_reconnect(user_id)
            raise NeedsReconnect()

        refresh_token = decrypt(row.refresh_token_enc)
        try:
            access_token, expires_at = await asyncio.to_thread(_refresh_blocking, refresh_token)
        except RefreshError as error:  # invalid_grant: revoked, or expired (7 days in Testing)
            await mark_needs_reconnect(user_id)
            raise NeedsReconnect() from error
        except GoogleAuthError as error:  # network problems: try again later
            raise GoogleApiError() from error

        row.access_token_enc = encrypt(access_token)
        row.expires_at = expires_at
        await session.commit()
        return access_token


async def revoke_token(token: str) -> None:
    """Revoke a Google token. Failures are logged, not raised: the user is leaving anyway."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(REVOKE_URI, data={"token": token})
        if response.status_code not in (200, 400):  # 400 = already invalid
            log.warning("token_revoke_failed", status=response.status_code)
    except httpx.HTTPError as error:
        log.warning("token_revoke_failed", error_type=type(error).__name__)
