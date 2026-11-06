"""Agenda and Undo for automatic calendar actions."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.errors import BadRequest
from app.models import User
from app.schemas.calendar import AgendaResponse
from app.security.session import current_user
from app.services import agenda, scheduling_actions

router = APIRouter(tags=["calendar"])

MAX_AGENDA_DAYS = 31


class UndoResponse(BaseModel):
    id: uuid.UUID
    undone: bool
    message: str


@router.get("/calendar/agenda", response_model=AgendaResponse)
async def get_agenda(
    start: datetime | None = Query(None, alias="from", description="ISO 8601; default now"),
    end: datetime | None = Query(None, alias="to", description="ISO 8601; default 7 days later"),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AgendaResponse:
    """Events from Google Calendar; auto-added ones are flagged, pending proposals included."""
    window_start = start or datetime.now(UTC)
    window_end = end or window_start + timedelta(days=7)
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise BadRequest("'from' and 'to' need a UTC offset, e.g. 2026-11-10T00:00:00+05:30.")
    if not window_start < window_end <= window_start + timedelta(days=MAX_AGENDA_DAYS):
        raise BadRequest(f"'to' must be after 'from' and at most {MAX_AGENDA_DAYS} days later.")
    return await agenda.get_agenda(db, user.id, window_start, window_end)


@router.post("/scheduling-actions/{action_id}/undo", response_model=UndoResponse)
async def undo_scheduling_action(
    action_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> UndoResponse:
    """Undo an automatic action within its window (delete the event / reset the response)."""
    row = await scheduling_actions.undo_action(db, user.id, action_id)
    message = "Event removed." if row.action == "create_event" else "Invite response reset."
    return UndoResponse(id=row.id, undone=row.undone, message=message)
