"""Activity log, notifications (the bell) and the dashboard strip."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ActivityItem(BaseModel):
    id: uuid.UUID
    actor: str  # guardrail, triage, scheduling, executor, user, ...
    action: str
    tool: str | None
    email_id: uuid.UUID | None
    detail: dict[str, Any]
    status: Literal["ok", "error", "blocked", "auto"]
    created_at: datetime
    undo_action_id: uuid.UUID | None = None  # set while an automatic action can still be undone
    undo_until: datetime | None = None


class ActivityPage(BaseModel):
    items: list[ActivityItem]
    next_cursor: str | None


class NotificationItem(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    body: str
    link: str | None
    read: bool
    created_at: datetime
    undo_action_id: uuid.UUID | None
    can_undo: bool  # false once the undo window has passed or it was already undone


class NotificationList(BaseModel):
    items: list[NotificationItem]
    unread_count: int


class MarkReadBody(BaseModel):
    """Mark the given notifications as read, or all of them when `ids` is empty."""

    ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)


class PriorityItem(BaseModel):
    email_id: uuid.UUID
    subject: str
    from_name: str | None
    from_email: str
    summary: str
    priority: int
    received_at: datetime


class DashboardResponse(BaseModel):
    """Counts for the last 7 days of received mail, plus what is waiting for the user."""

    window_days: int
    total: int
    unread: int
    by_category: dict[str, int]
    by_sentiment: dict[str, int]
    priority_queue: list[PriorityItem]
    pending_approvals: int
    auto_actions_today: int
    suspicious: int
