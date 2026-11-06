"""Settings page: disconnect Google, delete all data."""

from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.schemas.common import OkResponse
from app.security.session import clear_session_cookies, current_user
from app.services import account

router = APIRouter(tags=["account"])


class DeleteAccountBody(BaseModel):
    """The user must type "delete" to confirm."""

    confirm: Literal["delete"]


@router.post("/account/disconnect", response_model=OkResponse)
async def disconnect_google(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> OkResponse:
    """Revoke Google access and stop Gmail push. Your synced data stays until you delete it."""
    await account.disconnect_google(db, user)
    return OkResponse()


@router.delete("/account", response_model=OkResponse)
async def delete_account(
    body: DeleteAccountBody,
    response: Response,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> OkResponse:
    """Delete everything stored about you and sign out. This cannot be undone."""
    await account.delete_account(db, user)
    clear_session_cookies(response)
    return OkResponse()
