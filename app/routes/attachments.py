"""Files page: search attachment metadata."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.schemas.emails import AttachmentItem
from app.security.session import current_user
from app.services import attachments

router = APIRouter(tags=["attachments"])


@router.get("/attachments", response_model=list[AttachmentItem])
async def search_attachments(
    q: str | None = Query(None, max_length=200, description="e.g. 'invoice pdf ravi'"),
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AttachmentItem]:
    """Attachments matching the query (file name, sender, subject), or the newest ones."""
    return await attachments.search_attachments(db, user.id, q, limit)
