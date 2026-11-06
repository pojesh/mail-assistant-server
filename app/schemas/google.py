"""Small models returned by the Gmail / Calendar wrappers (never raw API dicts)."""

from datetime import datetime

from pydantic import BaseModel, Field


class InviteInfo(BaseModel):
    """Calendar invite details parsed from a `text/calendar` part."""

    uid: str | None = None
    method: str | None = None  # REQUEST, CANCEL, REPLY, ...
    start: str | None = None  # ISO 8601
    end: str | None = None
    organizer: str | None = None
    summary: str | None = None


class AttachmentInfo(BaseModel):
    """Attachment metadata only. The file itself is never downloaded."""

    filename: str
    mime_type: str
    size_bytes: int = 0
    attachment_id: str | None = None


class ParsedEmail(BaseModel):
    """A Gmail message turned into clean, typed fields."""

    gmail_id: str
    thread_id: str
    label_ids: list[str] = Field(default_factory=list)
    from_email: str
    from_name: str | None = None
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = ""
    snippet: str = ""
    body_text: str = ""
    body_html: str | None = None
    received_at: datetime
    message_id_header: str | None = None
    in_reply_to: str | None = None
    references_header: str | None = None
    attachments: list[AttachmentInfo] = Field(default_factory=list)
    invite: InviteInfo | None = None


class HistoryChanges(BaseModel):
    """What changed in the mailbox since a history id."""

    added_message_ids: list[str] = Field(default_factory=list)  # oldest first, no duplicates
    read_message_ids: list[str] = Field(default_factory=list)  # UNREAD label removed
    unread_message_ids: list[str] = Field(default_factory=list)  # UNREAD label added
    latest_history_id: str


class WatchResult(BaseModel):
    history_id: str
    expires_at: datetime


class GmailThreadSummary(BaseModel):
    """A search hit: one Gmail conversation (metadata only)."""

    thread_id: str
    subject: str
    last_from: str
    snippet: str
    last_date: datetime | None
    message_count: int
    email_id: str | None = None  # our stored email for this thread, if any (for flows/citations)


class GmailThreadMessage(BaseModel):
    gmail_id: str
    from_email: str
    from_name: str | None
    date: datetime
    body: str  # quotes removed, capped


class GmailThread(BaseModel):
    thread_id: str
    subject: str
    messages: list[GmailThreadMessage]
