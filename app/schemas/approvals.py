"""Approvals queue: what the user is asked to approve, and their decision."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field

from app.schemas.llm import DraftVariant
from app.schemas.scheduling import SchedulingDecision, Slot

ApprovalKind = Literal["draft", "event", "invite_response"]
ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "stale"]


class ApprovalEmail(BaseModel):
    subject: str
    from_email: str
    from_name: str | None
    received_at: str


class ApprovalPayload(BaseModel):
    """Everything the Approvals page shows for one item."""

    email: ApprovalEmail
    summary: str | None = None
    variants: list[DraftVariant] = Field(default_factory=list)  # bodies include the signature
    scheduling: SchedulingDecision | None = None  # the pending calendar action, if any
    slots: list[Slot] = Field(default_factory=list)  # choices for a new event
    reason: str | None = None  # why the assistant suggests this
    autonomy_reason: str | None = None  # why it was not done automatically


class ApprovalOut(BaseModel):
    id: uuid.UUID
    kind: ApprovalKind
    status: ApprovalStatus
    email_id: uuid.UUID | None
    payload: ApprovalPayload
    warnings: list[str]
    result: dict[str, Any] | None
    created_at: datetime
    decided_at: datetime | None


class ApprovalList(BaseModel):
    items: list[ApprovalOut]


class ApprovalDecision(BaseModel):
    """The user's answer. Only the fields relevant to the approval kind are used."""

    action: Literal["approve", "reject"]
    variant_index: int | None = Field(None, ge=0, le=2)
    edited_body: str | None = Field(None, min_length=1, max_length=20_000)
    slot_index: int | None = Field(None, ge=0, le=5)
    response: Literal["accepted", "tentative", "declined"] | None = None
    # New events only: the user may rename the meeting or change who is invited.
    title: str | None = Field(None, min_length=1, max_length=200)
    attendees: list[EmailStr] | None = Field(None, min_length=1, max_length=50)


class DecideResponse(BaseModel):
    approval: ApprovalOut
    outcome: Literal["done", "rejected", "stale", "failed"]
    message: str
