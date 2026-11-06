"""Users, their encrypted Google tokens and their Gmail sync position."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdAndTimestamps


class User(IdAndTimestamps, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str | None] = mapped_column(Text)
    picture_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | needs_reconnect
    onboarded: Mapped[bool] = mapped_column(default=False)


class OAuthToken(IdAndTimestamps, Base):
    """Google OAuth tokens, Fernet-encrypted at rest."""

    __tablename__ = "oauth_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime]
    scopes: Mapped[list[str]] = mapped_column(default=list)


class SyncState(IdAndTimestamps, Base):
    """Where Gmail sync left off for a user, and when their Gmail watch expires."""

    __tablename__ = "sync_state"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    history_id: Mapped[str | None] = mapped_column(String(32))
    watch_expires_at: Mapped[datetime | None]
    last_synced_at: Mapped[datetime | None]
