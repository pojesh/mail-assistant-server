"""Create or update users when they sign in with Google."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.google.oauth import GoogleTokens
from app.models import OAuthToken, Preferences, SyncState, User
from app.security.crypto import encrypt


async def _upsert_user(db: AsyncSession, tokens: GoogleTokens) -> User:
    """Find the user by email or create them; refresh name and picture from Google."""
    user = await db.scalar(select(User).where(User.email == tokens.email))
    if user is None:
        user = User(email=tokens.email)
        db.add(user)
    user.name = tokens.name
    user.picture_url = tokens.picture_url
    user.status = "active"
    await db.flush()  # assigns user.id
    return user


async def _save_tokens(db: AsyncSession, user: User, tokens: GoogleTokens) -> None:
    """Store the tokens encrypted. Keep the old refresh token if Google did not send a new one."""
    row = await db.scalar(select(OAuthToken).where(OAuthToken.user_id == user.id))
    if row is None:
        row = OAuthToken(user_id=user.id)
        db.add(row)
    row.access_token_enc = encrypt(tokens.access_token)
    if tokens.refresh_token:
        row.refresh_token_enc = encrypt(tokens.refresh_token)
    row.expires_at = tokens.expires_at
    row.scopes = tokens.scopes


async def _ensure_user_rows(db: AsyncSession, user: User) -> None:
    """Every user has exactly one preferences row and one sync_state row."""
    if await db.scalar(select(Preferences.id).where(Preferences.user_id == user.id)) is None:
        db.add(Preferences(user_id=user.id))
    if await db.scalar(select(SyncState.id).where(SyncState.user_id == user.id)) is None:
        db.add(SyncState(user_id=user.id))


async def sign_in_user(db: AsyncSession, tokens: GoogleTokens) -> User:
    """Upsert the user and their tokens after a successful Google sign-in."""
    user = await _upsert_user(db, tokens)
    await _save_tokens(db, user, tokens)
    await _ensure_user_rows(db, user)
    await db.commit()
    return user
