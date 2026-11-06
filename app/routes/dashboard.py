"""Dashboard strip and the daily digest."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.schemas.digest import Digest
from app.schemas.overview import DashboardResponse
from app.security.session import current_user
from app.services import digest, overview
from app.services.preferences import load_preferences

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> DashboardResponse:
    """Category and sentiment counts, priority queue, pending approvals, auto actions today."""
    prefs = await load_preferences(user.id)
    return await overview.dashboard(db, user.id, prefs.timezone)


@router.get("/digest/today", response_model=Digest | None)
async def get_latest_digest(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> Digest | None:
    """The most recent digest (built each morning after 08:00 local time), or null."""
    return await digest.latest_digest(db, user.id)
