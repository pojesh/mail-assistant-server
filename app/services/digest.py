"""Build and store the daily digest; read the latest one."""

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.digest import write_digest
from app.db import open_session
from app.errors import AppError
from app.google import calendar_api
from app.logging import get_logger
from app.models import Digest as DigestRow
from app.models import Email, EmailInsight, SchedulingAction
from app.schemas.digest import Digest, DigestEmail, DigestMeeting
from app.services.preferences import load_preferences

log = get_logger(__name__)

LOOKBACK = timedelta(hours=36)
MAX_ITEMS = 10
DIGEST_HOUR = 8  # local time after which the sweep builds today's digest


async def needs_attention(db: AsyncSession, user_id: uuid.UUID) -> list[DigestEmail]:
    """Recent, non-suspicious mail that needs a reply or has priority >= 3, most important first."""
    since = datetime.now(UTC) - LOOKBACK
    rows = await db.execute(
        select(Email, EmailInsight)
        .join(EmailInsight, EmailInsight.email_id == Email.id)
        .where(
            Email.user_id == user_id,
            Email.received_at >= since,
            EmailInsight.suspicious.is_(False),
            (EmailInsight.needs_reply.is_(True)) | (EmailInsight.priority >= 3),
        )
        .order_by(EmailInsight.priority.desc(), Email.received_at.desc())
        .limit(MAX_ITEMS)
    )
    return [
        DigestEmail(
            email_id=email.id,
            subject=email.subject,
            from_name=email.from_name,
            from_email=email.from_email,
            summary=insight.summary,
            priority=insight.priority,
            needs_reply=insight.needs_reply,
        )
        for email, insight in rows.all()
    ]


async def todays_meetings(
    db: AsyncSession, user_id: uuid.UUID, timezone: str
) -> list[DigestMeeting]:
    """Today's timed, not-declined events (auto-added ones marked). Calendar errors -> empty."""
    zone = ZoneInfo(timezone)
    start = datetime.combine(datetime.now(zone).date(), time(0, 0), tzinfo=zone)
    try:
        events = await calendar_api.list_events(user_id, start, start + timedelta(days=1))
    except AppError as error:
        log.info("digest_calendar_unavailable", code=error.code)
        return []
    auto_ids = set(
        await db.scalars(
            select(SchedulingAction.calendar_event_id).where(
                SchedulingAction.user_id == user_id,
                SchedulingAction.mode == "auto",
                SchedulingAction.undone.is_(False),
            )
        )
    )
    return [
        DigestMeeting(
            title=event.summary, start=event.start, end=event.end, auto_added=event.id in auto_ids
        )
        for event in events
        if not event.all_day and event.my_response != "declined"
    ]


async def build_digest(user_id: uuid.UUID) -> Digest:
    """Gather today's facts, let the digest agent summarise them, and store the result."""
    prefs = await load_preferences(user_id)
    today = datetime.now(ZoneInfo(prefs.timezone)).date()
    async with open_session() as db:
        emails = await needs_attention(db, user_id)
        meetings = await todays_meetings(db, user_id, prefs.timezone)
    facts = {
        "needs_attention": [
            item.model_dump(include={"subject", "from_name", "summary", "priority", "needs_reply"})
            for item in emails
        ],
        "meetings": [item.model_dump() for item in meetings],
    }
    text = await write_digest(facts, prefs.timezone, today.strftime("%A %d %B %Y"))
    digest = Digest(
        date=today,
        headline=text.headline,
        summary=text.summary,
        needs_attention=emails,
        meetings=meetings,
        generated_at=datetime.now(UTC),
    )
    async with open_session() as db:
        row = await db.scalar(
            select(DigestRow).where(DigestRow.user_id == user_id, DigestRow.date == today)
        )
        if row is None:
            row = DigestRow(user_id=user_id, date=today)
            db.add(row)
        row.content = digest.model_dump(mode="json")
        await db.commit()
    return digest


async def latest_digest(db: AsyncSession, user_id: uuid.UUID) -> Digest | None:
    row = await db.scalar(
        select(DigestRow)
        .where(DigestRow.user_id == user_id)
        .order_by(DigestRow.date.desc())
        .limit(1)
    )
    return Digest.model_validate(row.content) if row else None


def is_digest_due(timezone: str, has_today: bool, now: datetime | None = None) -> bool:
    """Build once per local day, after 08:00 local time."""
    local_now = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone))
    return not has_today and local_now.hour >= DIGEST_HOUR
