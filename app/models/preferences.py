"""Structured preferences and free-text preference notes."""

import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdAndTimestamps, OwnedByUser
from app.models.email import EMBEDDING_DIM


class Preferences(IdAndTimestamps, Base):
    """One row per user. `meeting_preferences` is validated by the MeetingPreferences schema."""

    __tablename__ = "preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    signature: Mapped[str] = mapped_column(Text, default="")
    sign_off: Mapped[str] = mapped_column(Text, default="Best regards")
    tone: Mapped[str] = mapped_column(Text, default="friendly and professional")
    language: Mapped[str] = mapped_column(String(64), default="English")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    vip_senders: Mapped[list[str]] = mapped_column(default=list)
    meeting_preferences: Mapped[dict[str, Any]] = mapped_column(default=dict)


class PreferenceNote(IdAndTimestamps, OwnedByUser, Base):
    """A free-text preference ("When clients ask for discounts, ..."), embedded for retrieval."""

    __tablename__ = "preference_notes"

    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="user")  # user | learned
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|suggested|rejected
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
