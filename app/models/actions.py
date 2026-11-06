"""Approvals queue, scheduling actions (with undo), notifications and the activity log."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdAndTimestamps, OwnedByUser


class Approval(IdAndTimestamps, OwnedByUser, Base):
    """A write the user must approve: a draft, a new event, or an invite response."""

    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_user_status", "user_id", "status"),)

    kind: Mapped[str] = mapped_column(String(32))  # draft | event | invite_response
    email_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), index=True
    )
    thread_id: Mapped[str] = mapped_column(Text)  # LangGraph thread id
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    decided_at: Mapped[datetime | None]
    result: Mapped[dict[str, Any] | None]


class SchedulingAction(IdAndTimestamps, OwnedByUser, Base):
    """A calendar write that was performed (automatically or after approval)."""

    __tablename__ = "scheduling_actions"

    email_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(32))
    calendar_event_id: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(16))  # auto | approved
    decision: Mapped[dict[str, Any]] = mapped_column(default=dict)
    undo_until: Mapped[datetime | None]
    undone: Mapped[bool] = mapped_column(default=False)


class Notification(IdAndTimestamps, OwnedByUser, Base):
    __tablename__ = "notifications"

    type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(Text)
    read: Mapped[bool] = mapped_column(default=False)
    undo_action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scheduling_actions.id", ondelete="SET NULL")
    )


class ActivityLog(IdAndTimestamps, OwnedByUser, Base):
    """One row per agent/graph step: who did what, with which tool, and the outcome."""

    __tablename__ = "activity_log"
    __table_args__ = (Index("ix_activity_user_created", "user_id", "created_at"),)

    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    tool: Mapped[str | None] = mapped_column(String(64))
    email_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("emails.id", ondelete="SET NULL"))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|error|blocked|auto
