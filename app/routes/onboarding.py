"""Onboarding steps that need the server: VIP suggestions, learning the writing style, finishing."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.routes.auth import me_response
from app.schemas.common import MeResponse
from app.security.session import current_user
from app.services import onboarding
from app.services.embeddings import learn_writing_style
from app.services.onboarding import VipSuggestion

router = APIRouter(tags=["onboarding"])


class LearnStyleResponse(BaseModel):
    embedded: int  # sent emails added as writing-style examples


@router.get("/onboarding/vip-suggestions", response_model=list[VipSuggestion])
async def get_vip_suggestions(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> list[VipSuggestion]:
    """Frequent correspondents (call after learn-style, which imports sent mail)."""
    return await onboarding.suggest_vips(db, user)


@router.post("/onboarding/learn-style", response_model=LearnStyleResponse)
async def learn_style(user: User = Depends(current_user)) -> LearnStyleResponse:
    """Import and embed the last 200 sent emails. Takes up to a couple of minutes."""
    return LearnStyleResponse(embedded=await learn_writing_style(user.id))


@router.post("/onboarding/complete", response_model=MeResponse)
async def complete_onboarding(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> MeResponse:
    """Create the AI/* Gmail labels and mark onboarding as done."""
    return me_response(await onboarding.complete_onboarding(db, user))
