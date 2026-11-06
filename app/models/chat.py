"""Chat history and daily digests."""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdAndTimestamps, OwnedByUser


class ChatMessage(IdAndTimestamps, OwnedByUser, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_user_session", "user_id", "session_id"),)

    session_id: Mapped[uuid.UUID]
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    steps: Mapped[list[Any]] = mapped_column(default=list)
    citations: Mapped[list[Any]] = mapped_column(default=list)


class Digest(IdAndTimestamps, OwnedByUser, Base):
    __tablename__ = "digests"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_digests_user_date"),)

    date: Mapped[date]
    content: Mapped[dict[str, Any]] = mapped_column(default=dict)
