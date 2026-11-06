"""Activity log: every agent step, tool, reason and outcome."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.schemas.overview import ActivityPage
from app.security.session import current_user
from app.services import overview

router = APIRouter(tags=["activity"])


@router.get("/activity", response_model=ActivityPage)
async def list_activity(
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ActivityPage:
    """Newest first; automatic actions that can still be undone carry their Undo id."""
    return await overview.list_activity(db, user.id, cursor, limit)
