"""Perform calendar writes (after the autonomy gate or an approval), and undo them."""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import open_session
from app.errors import BadRequest, Conflict, NotFound
from app.google import calendar_api
from app.models import Email, SchedulingAction
from app.schemas.calendar import NewEvent, ResponseStatus
from app.schemas.scheduling import SchedulingDecision, Slot
from app.services.activity import log_activity

INVITE_RESPONSES: dict[str, ResponseStatus] = {
    "accept_invite": "accepted",
    "tentative_invite": "tentative",
    "decline_invite": "declined",
}
ACTION_FOR_RESPONSE = {response: action for action, response in INVITE_RESPONSES.items()}


async def perform_calendar_action(
    user_id: uuid.UUID,
    email_id: uuid.UUID | None,
    decision: SchedulingDecision,
    mode: str,
    slot: Slot | None = None,
    response: ResponseStatus | None = None,
    undo_window_hours: int = 24,
) -> SchedulingAction:
    """Create the event or send the invite response, and record it (with an undo deadline)."""
    calendar_event_id: str | None
    if decision.action == "create_event":
        chosen = slot or decision.slot
        if chosen is None:
            raise BadRequest("No time was chosen for the event.")
        event = await calendar_api.create_event(
            user_id,
            NewEvent(
                title=decision.title or "Meeting",
                start=chosen.start,
                end=chosen.end,
                attendees=decision.attendees,
                description=decision.agenda,
                add_video_link=decision.add_video_link,
            ),
        )
        calendar_event_id = event.id
    elif decision.action in INVITE_RESPONSES:
        if not decision.event_id:
            raise NotFound(
                "This invite is not in your Google Calendar, so it cannot be answered here."
            )
        response = response or INVITE_RESPONSES[decision.action]
        await calendar_api.respond_to_invite(user_id, decision.event_id, response)
        calendar_event_id = decision.event_id
    else:
        raise BadRequest(f"'{decision.action}' does not change the calendar.")

    async with open_session() as db:
        row = SchedulingAction(
            user_id=user_id,
            email_id=email_id,
            action=ACTION_FOR_RESPONSE.get(response, decision.action)
            if response
            else decision.action,
            calendar_event_id=calendar_event_id,
            mode=mode,
            decision=decision.model_dump(mode="json"),
            undo_until=datetime.now(UTC) + timedelta(hours=undo_window_hours)
            if mode == "auto"
            else None,
        )
        db.add(row)
        await db.commit()
        return row


async def count_auto_actions_today(user_id: uuid.UUID, timezone: str) -> int:
    """Automatic actions since local midnight (for the daily cap)."""
    local_midnight = datetime.now(ZoneInfo(timezone)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    async with open_session() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(SchedulingAction)
            .where(
                SchedulingAction.user_id == user_id,
                SchedulingAction.mode == "auto",
                SchedulingAction.created_at >= local_midnight,
            )
        )
    return int(count or 0)


async def has_emailed(user_id: uuid.UUID, address: str) -> bool:
    """Has the user ever sent mail to this address? (a "trusted sender" signal)"""
    async with open_session() as db:
        found = await db.scalar(
            select(Email.id)
            .where(
                Email.user_id == user_id,
                Email.is_sent.is_(True),
                Email.to.contains([address.lower()]),
            )
            .limit(1)
        )
    return found is not None


async def undo_action(
    db: AsyncSession, user_id: uuid.UUID, action_id: uuid.UUID
) -> SchedulingAction:
    """Undo an automatic action within its window: delete the event or reset the invite response."""
    row = await db.scalar(
        select(SchedulingAction).where(
            SchedulingAction.id == action_id, SchedulingAction.user_id == user_id
        )
    )
    if row is None:
        raise NotFound("Action not found.")
    if row.undone:
        raise Conflict("This action was already undone.")
    if row.mode != "auto" or row.undo_until is None or row.undo_until < datetime.now(UTC):
        raise Conflict("The undo window for this action has passed.")
    if not row.calendar_event_id:
        raise Conflict("Nothing to undo for this action.")

    if row.action == "create_event":
        await calendar_api.delete_event(user_id, row.calendar_event_id)
    else:
        await calendar_api.respond_to_invite(user_id, row.calendar_event_id, "needsAction")
    row.undone = True
    await db.commit()
    await log_activity(
        user_id,
        "user",
        "undo",
        tool="calendar",
        email_id=row.email_id,
        detail={"action": row.action, "scheduling_action_id": str(row.id)},
    )
    return row
