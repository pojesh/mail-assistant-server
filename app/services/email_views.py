"""Build the API views of emails (list items and the full detail) from database rows."""

import uuid
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, NotFound
from app.google import gmail_api
from app.logging import get_logger
from app.models import Approval, Attachment, Email, EmailInsight, SchedulingAction
from app.schemas.approvals import ApprovalPayload
from app.schemas.emails import (
    AttachmentItem,
    EmailDetail,
    EmailListItem,
    InsightBrief,
    InsightDetail,
    SchedulingInfo,
    ThreadMessage,
)
from app.schemas.google import InviteInfo
from app.schemas.llm import ActionItem, Category, MeetingIntent, Sentiment
from app.schemas.scheduling import SchedulingDecision

log = get_logger(__name__)


def to_insight_brief(insight: EmailInsight) -> InsightBrief:
    meeting = insight.meeting_intent or {}
    return InsightBrief(
        category=cast(Category, insight.category),  # validated by TriageResult when stored
        priority=insight.priority,
        sentiment=cast(Sentiment, insight.sentiment),
        sentiment_score=insight.sentiment_score,
        summary=insight.summary,
        needs_reply=insight.needs_reply,
        suspicious=insight.suspicious,
        has_meeting_intent=bool(meeting.get("wants_meeting")),
    )


def to_insight_detail(insight: EmailInsight) -> InsightDetail:
    brief = to_insight_brief(insight)
    return InsightDetail(
        **brief.model_dump(),
        thread_summary=insight.thread_summary,
        action_items=[ActionItem.model_validate(item) for item in insight.action_items],
        meeting_intent=(
            MeetingIntent.model_validate(insight.meeting_intent) if insight.meeting_intent else None
        ),
        suspicious_reasons=[str(reason) for reason in insight.suspicious_reasons],
        model=insight.model,
        provider=insight.provider,
    )


def to_list_item(
    email: Email, insight: EmailInsight | None, has_attachments: bool
) -> EmailListItem:
    return EmailListItem(
        id=email.id,
        thread_id=email.thread_id,
        from_email=email.from_email,
        from_name=email.from_name,
        subject=email.subject,
        snippet=email.snippet,
        received_at=email.received_at,
        is_unread=email.is_unread,
        has_attachments=has_attachments,
        has_invite=email.invite is not None,
        insight=to_insight_brief(insight) if insight else None,
    )


def to_attachment_item(attachment: Attachment) -> AttachmentItem:
    return AttachmentItem(
        id=attachment.id,
        email_id=attachment.email_id,
        filename=attachment.filename,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        sender_email=attachment.sender_email,
        sender_name=attachment.sender_name,
        subject=attachment.subject,
        received_at=attachment.received_at,
    )


async def get_user_email(db: AsyncSession, user_id: uuid.UUID, email_id: uuid.UUID) -> Email:
    """Load one of the user's emails, or 404 (also for other users' emails)."""
    email = await db.scalar(select(Email).where(Email.id == email_id, Email.user_id == user_id))
    if email is None:
        raise NotFound("Email not found.")
    return email


async def _fetch_html(user_id: uuid.UUID, gmail_id: str) -> str | None:
    """HTML body, fetched live from Gmail (not stored). Missing HTML is not an error."""
    # NOTE: only plain text is stored (spec §9); the HTML view is fetched on demand.
    try:
        return (await gmail_api.get_message(user_id, gmail_id)).body_html
    except AppError as error:
        log.info("email_html_unavailable", code=error.code)
        return None


async def get_scheduling_info(db: AsyncSession, email_id: uuid.UUID) -> SchedulingInfo | None:
    """Latest calendar action for the email, else a pending approval that carries a decision."""
    action = await db.scalar(
        select(SchedulingAction)
        .where(SchedulingAction.email_id == email_id)
        .order_by(SchedulingAction.created_at.desc())
        .limit(1)
    )
    if action is not None:
        state = "undone" if action.undone else action.mode
        return SchedulingInfo(
            decision=SchedulingDecision.model_validate(action.decision),
            state=cast(Literal["auto", "approved", "undone"], state),
            action_id=action.id,
            undo_until=action.undo_until,
        )
    approval = await db.scalar(
        select(Approval).where(Approval.email_id == email_id, Approval.status == "pending").limit(1)
    )
    if approval is None:
        return None
    payload = ApprovalPayload.model_validate(approval.payload)
    if payload.scheduling is None:
        return None
    return SchedulingInfo(decision=payload.scheduling, state="pending", approval_id=approval.id)


async def get_email_detail(
    db: AsyncSession, user_id: uuid.UUID, email_id: uuid.UUID
) -> EmailDetail:
    """Email + insight + attachments + other messages in the thread."""
    email = await get_user_email(db, user_id, email_id)
    insight = await db.scalar(select(EmailInsight).where(EmailInsight.email_id == email.id))
    attachments = await db.scalars(select(Attachment).where(Attachment.email_id == email.id))
    thread = await db.scalars(
        select(Email)
        .where(Email.user_id == user_id, Email.thread_id == email.thread_id, Email.id != email.id)
        .order_by(Email.received_at)
    )
    return EmailDetail(
        id=email.id,
        gmail_id=email.gmail_id,
        thread_id=email.thread_id,
        from_email=email.from_email,
        from_name=email.from_name,
        to=email.to,
        cc=email.cc,
        subject=email.subject,
        snippet=email.snippet,
        body_text=email.body_text,
        body_html=await _fetch_html(user_id, email.gmail_id),
        received_at=email.received_at,
        is_unread=email.is_unread,
        is_sent=email.is_sent,
        invite=InviteInfo.model_validate(email.invite) if email.invite else None,
        insight=to_insight_detail(insight) if insight else None,
        attachments=[to_attachment_item(item) for item in attachments],
        thread=[
            ThreadMessage(
                id=item.id,
                from_email=item.from_email,
                from_name=item.from_name,
                snippet=item.snippet,
                received_at=item.received_at,
                is_sent=item.is_sent,
            )
            for item in thread
        ],
        scheduling=await get_scheduling_info(db, email.id),
    )
