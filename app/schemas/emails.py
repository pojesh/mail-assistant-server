"""API models for the inbox list and email detail."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.google import InviteInfo
from app.schemas.llm import ActionItem, Category, MeetingIntent, Sentiment
from app.schemas.scheduling import SchedulingDecision


class InsightBrief(BaseModel):
    """The AI chips shown in the inbox list."""

    category: Category
    priority: int
    sentiment: Sentiment
    sentiment_score: float
    summary: str
    needs_reply: bool
    suspicious: bool
    has_meeting_intent: bool


class InsightDetail(InsightBrief):
    """Everything triage found, shown in the AI panel."""

    thread_summary: str | None
    action_items: list[ActionItem]
    meeting_intent: MeetingIntent | None
    suspicious_reasons: list[str]
    model: str
    provider: str


class EmailListItem(BaseModel):
    id: uuid.UUID
    thread_id: str
    from_email: str
    from_name: str | None
    subject: str
    snippet: str
    received_at: datetime
    is_unread: bool
    has_attachments: bool
    has_invite: bool
    insight: InsightBrief | None


class EmailListResponse(BaseModel):
    items: list[EmailListItem]
    next_cursor: str | None


class AttachmentItem(BaseModel):
    id: uuid.UUID
    email_id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    sender_email: str
    sender_name: str | None
    subject: str
    received_at: datetime


class ThreadMessage(BaseModel):
    """Another message in the same conversation."""

    id: uuid.UUID
    from_email: str
    from_name: str | None
    snippet: str
    received_at: datetime
    is_sent: bool


class SchedulingInfo(BaseModel):
    """The scheduling card: what was decided and where it stands."""

    decision: SchedulingDecision
    state: Literal["auto", "approved", "pending", "undone"]
    action_id: uuid.UUID | None = None  # for Undo
    undo_until: datetime | None = None
    approval_id: uuid.UUID | None = None


class EmailDetail(BaseModel):
    id: uuid.UUID
    gmail_id: str
    thread_id: str
    from_email: str
    from_name: str | None
    to: list[str]
    cc: list[str]
    subject: str
    snippet: str
    body_text: str | None
    body_html: str | None  # fetched live from Gmail; the web app sanitises it before display
    received_at: datetime
    is_unread: bool
    is_sent: bool
    invite: InviteInfo | None
    insight: InsightDetail | None
    attachments: list[AttachmentItem]
    thread: list[ThreadMessage]
    scheduling: SchedulingInfo | None = None


class FlowStartResponse(BaseModel):
    """Result of "Draft reply" / "Find a time"."""

    status: Literal["approval_created", "auto_scheduled", "nothing_to_do"]
    approval_id: uuid.UUID | None
    message: str


class SyncResponse(BaseModel):
    status: Literal["synced", "already_syncing"]
    processed: int
    remaining: int
