"""In-app notifications (the bell): auto actions with Undo, new approvals, reconnect warnings."""

import uuid
from typing import Literal

from app.db import open_session
from app.models import Notification

NotificationType = Literal["auto_action", "approval", "reconnect", "digest", "info"]


async def create_notification(
    user_id: uuid.UUID,
    type: NotificationType,
    title: str,
    body: str = "",
    link: str | None = None,
    undo_action_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Add a notification for the user and return its id."""
    async with open_session() as db:
        notification = Notification(
            user_id=user_id,
            type=type,
            title=title[:200],
            body=body[:1000],
            link=link,
            undo_action_id=undo_action_id,
        )
        db.add(notification)
        await db.commit()
        return notification.id
