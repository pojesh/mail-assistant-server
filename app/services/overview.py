"""Read models for the activity log, the notification bell and the dashboard."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.models import ActivityLog, Approval, Email, EmailInsight, Notification, SchedulingAction
from app.schemas.overview import (
    ActivityItem,
    ActivityPage,
    DashboardResponse,
    NotificationItem,
    NotificationList,
    PriorityItem,
)
from app.services.activity import ActivityStatus
from app.services.emails import decode_cursor, encode_cursor
from app.services.scheduling_actions import count_auto_actions_today

DASHBOARD_DAYS = 7


async def _undoable(db: AsyncSession, user_id: uuid.UUID) -> dict[uuid.UUID, SchedulingAction]:
    """Automatic actions that can still be undone, by id."""
    rows = await db.scalars(
        select(SchedulingAction).where(
            SchedulingAction.user_id == user_id,
            SchedulingAction.mode == "auto",
            SchedulingAction.undone.is_(False),
            SchedulingAction.undo_until > datetime.now(UTC),
        )
    )
    return {row.id: row for row in rows}


def _action_id(detail: dict[str, object]) -> uuid.UUID | None:
    try:
        return (
            uuid.UUID(str(detail["scheduling_action_id"]))
            if "scheduling_action_id" in detail
            else None
        )
    except ValueError:
        return None


async def list_activity(
    db: AsyncSession, user_id: uuid.UUID, cursor: str | None, limit: int
) -> ActivityPage:
    """Newest first, with Undo info for automatic actions that are still undoable."""
    statement = select(ActivityLog).where(ActivityLog.user_id == user_id)
    if cursor:
        moment, row_id = decode_cursor(cursor)
        statement = statement.where(
            tuple_(ActivityLog.created_at, ActivityLog.id) < (moment, row_id)
        )
    rows = list(
        await db.scalars(
            statement.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc()).limit(
                limit + 1
            )
        )
    )
    next_cursor = (
        encode_cursor(rows[limit - 1].created_at, rows[limit - 1].id) if len(rows) > limit else None
    )
    undoable = await _undoable(db, user_id)
    items = []
    for row in rows[:limit]:
        action_id = _action_id(row.detail) if row.status == "auto" else None
        action = undoable.get(action_id) if action_id else None
        items.append(
            ActivityItem(
                id=row.id,
                actor=row.actor,
                action=row.action,
                tool=row.tool,
                email_id=row.email_id,
                detail=row.detail,
                status=cast(ActivityStatus, row.status),
                created_at=row.created_at,
                undo_action_id=action.id if action else None,
                undo_until=action.undo_until if action else None,
            )
        )
    return ActivityPage(items=items, next_cursor=next_cursor)


async def list_notifications(db: AsyncSession, user_id: uuid.UUID) -> NotificationList:
    rows = list(
        await db.scalars(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
            .limit(50)
        )
    )
    undoable = await _undoable(db, user_id)
    unread = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read.is_(False))
    )
    items = [
        NotificationItem(
            id=row.id,
            type=row.type,
            title=row.title,
            body=row.body,
            link=row.link,
            read=row.read,
            created_at=row.created_at,
            undo_action_id=row.undo_action_id,
            can_undo=row.undo_action_id in undoable,
        )
        for row in rows
    ]
    return NotificationList(items=items, unread_count=int(unread or 0))


async def mark_read(db: AsyncSession, user_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    statement = update(Notification).where(Notification.user_id == user_id).values(read=True)
    if ids:
        statement = statement.where(Notification.id.in_(ids))
    await db.execute(statement)
    await db.commit()


async def _count_by(
    db: AsyncSession, user_id: uuid.UUID, column: InstrumentedAttribute[str], since: datetime
) -> dict[str, int]:
    rows = await db.execute(
        select(column, func.count())
        .select_from(Email)
        .join(EmailInsight, EmailInsight.email_id == Email.id)
        .where(Email.user_id == user_id, Email.is_sent.is_(False), Email.received_at >= since)
        .group_by(column)
    )
    return {str(key): int(count) for key, count in rows.all()}


async def dashboard(db: AsyncSession, user_id: uuid.UUID, timezone: str) -> DashboardResponse:
    """Category and sentiment counts, the priority queue, and what is waiting for the user."""
    since = datetime.now(UTC) - timedelta(days=DASHBOARD_DAYS)
    received = (Email.user_id == user_id, Email.is_sent.is_(False), Email.received_at >= since)
    total = await db.scalar(select(func.count()).select_from(Email).where(*received))
    unread = await db.scalar(
        select(func.count()).select_from(Email).where(*received, Email.is_unread.is_(True))
    )
    queue_rows = await db.execute(
        select(Email, EmailInsight)
        .join(EmailInsight, EmailInsight.email_id == Email.id)
        .where(*received, EmailInsight.priority >= 3, EmailInsight.suspicious.is_(False))
        .order_by(EmailInsight.priority.desc(), Email.received_at.desc())
        .limit(5)
    )
    pending = await db.scalar(
        select(func.count())
        .select_from(Approval)
        .where(Approval.user_id == user_id, Approval.status == "pending")
    )
    suspicious = await db.scalar(
        select(func.count())
        .select_from(Email)
        .join(EmailInsight, EmailInsight.email_id == Email.id)
        .where(*received, EmailInsight.suspicious.is_(True))
    )
    return DashboardResponse(
        window_days=DASHBOARD_DAYS,
        total=int(total or 0),
        unread=int(unread or 0),
        by_category=await _count_by(db, user_id, EmailInsight.category, since),
        by_sentiment=await _count_by(db, user_id, EmailInsight.sentiment, since),
        priority_queue=[
            PriorityItem(
                email_id=email.id,
                subject=email.subject,
                from_name=email.from_name,
                from_email=email.from_email,
                summary=insight.summary,
                priority=insight.priority,
                received_at=email.received_at,
            )
            for email, insight in queue_rows.all()
        ],
        pending_approvals=int(pending or 0),
        auto_actions_today=await count_auto_actions_today(user_id, timezone),
        suspicious=int(suspicious or 0),
    )
