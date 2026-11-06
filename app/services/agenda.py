"""Agenda view: real calendar events, auto-added ones marked, pending proposals as ghosts."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.google import calendar_api
from app.models import Approval, SchedulingAction
from app.schemas.approvals import ApprovalPayload
from app.schemas.calendar import AgendaEvent, AgendaResponse, ProposedEvent
from app.services.slots import parse_iso


async def _auto_actions(db: AsyncSession, user_id: uuid.UUID) -> dict[str, SchedulingAction]:
    """Calendar event id -> the automatic action that created or accepted it (not undone)."""
    rows = await db.scalars(
        select(SchedulingAction).where(
            SchedulingAction.user_id == user_id,
            SchedulingAction.mode == "auto",
            SchedulingAction.undone.is_(False),
            SchedulingAction.calendar_event_id.is_not(None),
        )
    )
    return {row.calendar_event_id: row for row in rows if row.calendar_event_id}


async def _proposals(
    db: AsyncSession, user_id: uuid.UUID, start: datetime, end: datetime
) -> list[ProposedEvent]:
    """Times waiting for approval inside the window."""
    rows = await db.scalars(
        select(Approval).where(Approval.user_id == user_id, Approval.status == "pending")
    )
    proposals = []
    for row in rows:
        payload = ApprovalPayload.model_validate(row.payload)
        title = (payload.scheduling.title if payload.scheduling else None) or payload.email.subject
        for slot in payload.slots:
            slot_start = parse_iso(slot.start)
            if start <= slot_start < end:
                proposals.append(
                    ProposedEvent(
                        approval_id=row.id,
                        title=title or "Meeting",
                        start=slot.start,
                        end=slot.end,
                        reason=slot.reason,
                    )
                )
    return proposals


async def get_agenda(
    db: AsyncSession, user_id: uuid.UUID, start: datetime, end: datetime
) -> AgendaResponse:
    events = await calendar_api.list_events(user_id, start, end)
    auto = await _auto_actions(db, user_id)
    agenda = []
    for event in events:
        action = auto.get(event.id)
        agenda.append(
            AgendaEvent(
                **event.model_dump(),
                auto_added=action is not None,
                action_id=action.id if action else None,
                undo_until=action.undo_until.isoformat() if action and action.undo_until else None,
            )
        )
    return AgendaResponse(events=agenda, proposals=await _proposals(db, user_id, start, end))
