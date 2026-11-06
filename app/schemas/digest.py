"""The daily digest: needs-attention emails and today's meetings, plus a short written summary."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class DigestText(BaseModel):
    """What the digest agent writes."""

    headline: str = Field(max_length=200)
    summary: str = Field(max_length=1000)


class DigestEmail(BaseModel):
    email_id: uuid.UUID
    subject: str
    from_name: str | None
    from_email: str
    summary: str
    priority: int
    needs_reply: bool


class DigestMeeting(BaseModel):
    title: str
    start: str
    end: str
    auto_added: bool = False


class Digest(BaseModel):
    date: date
    headline: str
    summary: str
    needs_attention: list[DigestEmail]
    meetings: list[DigestMeeting]
    generated_at: datetime
