"""Notification bell: automatic actions (with Undo), new approvals, reconnect warnings."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.schemas.common import OkResponse
from app.schemas.overview import MarkReadBody, NotificationList
from app.security.session import current_user
from app.services import overview

router = APIRouter(tags=["notifications"])


@router.get("/notifications", response_model=NotificationList)
async def list_notifications(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> NotificationList:
    """The latest 50 notifications and the unread count."""
    return await overview.list_notifications(db, user.id)


@router.post("/notifications/read", response_model=OkResponse)
async def mark_notifications_read(
    body: MarkReadBody, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> OkResponse:
    """Mark the given notifications as read (all of them if no ids are given)."""
    await overview.mark_read(db, user.id, body.ids)
    return OkResponse()
