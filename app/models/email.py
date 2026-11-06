"""Emails, their AI insights, attachment metadata and sent-mail embeddings."""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Computed, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdAndTimestamps, OwnedByUser

EMBEDDING_DIM = 768  # must match settings.embedding_dim and the migration


class Email(IdAndTimestamps, OwnedByUser, Base):
    __tablename__ = "emails"
    __table_args__ = (
        UniqueConstraint("user_id", "gmail_id", name="uq_emails_user_gmail_id"),
        Index("ix_emails_user_received", "user_id", "received_at"),
        Index("ix_emails_user_thread", "user_id", "thread_id"),
    )

    gmail_id: Mapped[str] = mapped_column(String(64))
    thread_id: Mapped[str] = mapped_column(String(64))  # Gmail thread id
    from_email: Mapped[str] = mapped_column(String(320))
    from_name: Mapped[str | None] = mapped_column(Text)
    to: Mapped[list[str]] = mapped_column(default=list)
    cc: Mapped[list[str]] = mapped_column(default=list)
    subject: Mapped[str] = mapped_column(Text, default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str | None] = mapped_column(Text)  # truncated; cleared after 30 days
    received_at: Mapped[datetime]
    is_sent: Mapped[bool] = mapped_column(default=False)
    is_unread: Mapped[bool] = mapped_column(default=False)
    invite: Mapped[dict[str, Any] | None]
    # NOTE: Message-ID and References are stored so drafts can be threaded replies.
    message_id_header: Mapped[str | None] = mapped_column(Text)
    references_header: Mapped[str | None] = mapped_column(Text)


class EmailInsight(IdAndTimestamps, OwnedByUser, Base):
    """Triage output for one email (category, priority, sentiment, summary, action items)."""

    __tablename__ = "email_insights"

    email_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), unique=True
    )
    category: Mapped[str] = mapped_column(String(32), index=True)
    priority: Mapped[int]
    sentiment: Mapped[str] = mapped_column(String(32))
    sentiment_score: Mapped[float]
    summary: Mapped[str] = mapped_column(Text)
    thread_summary: Mapped[str | None] = mapped_column(Text)
    action_items: Mapped[list[Any]] = mapped_column(default=list)
    meeting_intent: Mapped[dict[str, Any] | None]
    needs_reply: Mapped[bool] = mapped_column(default=False)
    suspicious: Mapped[bool] = mapped_column(default=False)
    suspicious_reasons: Mapped[list[Any]] = mapped_column(default=list)
    model: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(32))


class Attachment(IdAndTimestamps, OwnedByUser, Base):
    """Attachment metadata only — file contents are never downloaded or indexed."""

    __tablename__ = "attachments"
    __table_args__ = (
        Index("ix_attachments_search", "search", postgresql_using="gin"),
        Index(
            "ix_attachments_filename_trgm",
            "filename",
            postgresql_using="gin",
            postgresql_ops={"filename": "gin_trgm_ops"},
        ),
    )

    email_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    gmail_attachment_id: Mapped[str | None] = mapped_column(Text)
    # Copied from the email so the generated search column can use them.
    sender_email: Mapped[str] = mapped_column(String(320))
    sender_name: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime]
    gmail_thread_id: Mapped[str] = mapped_column(String(64))
    gmail_message_id: Mapped[str] = mapped_column(String(64))
    search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(filename, '') || ' ' || coalesce(sender_name, '')"
            " || ' ' || coalesce(sender_email, '') || ' ' || coalesce(subject, ''))",
            persisted=True,
        ),
    )


class SentEmbedding(IdAndTimestamps, OwnedByUser, Base):
    """One embedded chunk of a sent email, used as a writing-style example."""

    __tablename__ = "sent_embeddings"
    __table_args__ = (
        Index(
            "ix_sent_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    email_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), index=True
    )
    chunk: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
