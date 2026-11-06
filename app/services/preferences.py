"""Read and update a user's structured preferences."""

import uuid

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import open_session
from app.logging import get_logger
from app.models import Preferences
from app.schemas.preferences import MeetingPreferences, PreferencesBody

log = get_logger(__name__)


def to_body(row: Preferences) -> PreferencesBody:
    """Database row -> API model. Broken stored meeting rules fall back to the defaults."""
    try:
        meeting = MeetingPreferences.model_validate(row.meeting_preferences or {})
    except ValidationError:
        log.warning("meeting_preferences_invalid", user_id=str(row.user_id))
        meeting = MeetingPreferences()
    return PreferencesBody(
        signature=row.signature,
        sign_off=row.sign_off,
        tone=row.tone,
        language=row.language,
        timezone=row.timezone,
        vip_senders=row.vip_senders,
        meeting_preferences=meeting,
    )


async def get_or_create_row(db: AsyncSession, user_id: uuid.UUID) -> Preferences:
    """The user's preferences row (created with defaults if missing)."""
    row = await db.scalar(select(Preferences).where(Preferences.user_id == user_id))
    if row is None:
        row = Preferences(user_id=user_id)
        db.add(row)
        await db.flush()
    return row


async def load_preferences(user_id: uuid.UUID) -> PreferencesBody:
    """Preferences for agents and services that run outside a request session."""
    async with open_session() as db:
        row = await get_or_create_row(db, user_id)
        await db.commit()
        return to_body(row)


async def save_preferences(
    db: AsyncSession, user_id: uuid.UUID, body: PreferencesBody
) -> PreferencesBody:
    """Replace all structured preferences."""
    row = await get_or_create_row(db, user_id)
    row.signature = body.signature
    row.sign_off = body.sign_off
    row.tone = body.tone
    row.language = body.language
    row.timezone = body.timezone
    row.vip_senders = body.vip_senders
    row.meeting_preferences = body.meeting_preferences.model_dump(mode="json")
    await db.commit()
    return to_body(row)
