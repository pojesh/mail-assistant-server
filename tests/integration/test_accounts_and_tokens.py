"""Sign-in upsert and access-token refresh against the database."""

from datetime import UTC, datetime, timedelta

import pytest
from google.auth.exceptions import RefreshError
from sqlalchemy import func, select

from app.db import open_session
from app.errors import NeedsReconnect
from app.google import oauth
from app.google.oauth import GoogleTokens, get_valid_access_token
from app.models import OAuthToken, Preferences, User
from app.security.crypto import decrypt
from app.services.accounts import sign_in_user
from tests.integration.conftest import create_user


def tokens(refresh_token: str | None = "refresh-1", name: str = "Me") -> GoogleTokens:
    return GoogleTokens(
        access_token="access-1",
        refresh_token=refresh_token,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=sorted(oauth.REQUIRED_SCOPES),
        email="me@example.com",
        name=name,
        picture_url=None,
    )


async def test_sign_in_creates_then_updates_user() -> None:
    async with open_session() as db:
        first = await sign_in_user(db, tokens())
    async with open_session() as db:
        # Google sends no refresh token on some re-consents: the old one must be kept.
        second = await sign_in_user(db, tokens(refresh_token=None, name="New Name"))
    assert first.id == second.id

    async with open_session() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == 1
        assert await db.scalar(select(func.count()).select_from(Preferences)) == 1
        row = await db.scalar(select(OAuthToken))
        assert row is not None and row.refresh_token_enc is not None
        assert decrypt(row.refresh_token_enc) == "refresh-1"
        assert (await db.get(User, first.id)).name == "New Name"


async def test_valid_token_is_returned_without_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    user = await create_user()

    def refresh_must_not_run(_: str) -> tuple[str, datetime]:
        raise AssertionError("should not refresh")

    monkeypatch.setattr(oauth, "_refresh_blocking", refresh_must_not_run)
    assert await get_valid_access_token(user.id) == "access-token"


async def test_expiring_token_is_refreshed_and_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    user = await create_user(token_expires_in=timedelta(minutes=2))
    new_expiry = datetime.now(UTC) + timedelta(hours=1)
    monkeypatch.setattr(oauth, "_refresh_blocking", lambda _: ("fresh-token", new_expiry))

    assert await get_valid_access_token(user.id) == "fresh-token"
    async with open_session() as db:
        row = await db.scalar(select(OAuthToken).where(OAuthToken.user_id == user.id))
        assert row is not None and decrypt(row.access_token_enc) == "fresh-token"


async def test_revoked_refresh_token_marks_user_needs_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user(token_expires_in=timedelta(seconds=-1))

    def revoked(_: str) -> tuple[str, datetime]:
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(oauth, "_refresh_blocking", revoked)
    with pytest.raises(NeedsReconnect):
        await get_valid_access_token(user.id)
    async with open_session() as db:
        assert (await db.get(User, user.id)).status == "needs_reconnect"
