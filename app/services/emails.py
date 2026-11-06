"""Store synced emails and read them back for the inbox list and detail pages."""

import base64
import uuid
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import exists, or_, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import BadRequest
from app.models import Attachment, Email, EmailInsight
from app.schemas.google import ParsedEmail
from app.schemas.llm import Category, Sentiment


class EmailFilters(BaseModel):
    category: Category | None = None
    min_priority: int | None = None
    sentiment: Sentiment | None = None
    query: str | None = None
    unread: bool | None = None


async def save_email(db: AsyncSession, user_id: uuid.UUID, parsed: ParsedEmail) -> Email | None:
    """Insert an email and its attachment metadata. Returns None if it was already stored."""
    email = Email(
        user_id=user_id,
        gmail_id=parsed.gmail_id,
        thread_id=parsed.thread_id,
        from_email=parsed.from_email,
        from_name=parsed.from_name,
        to=parsed.to,
        cc=parsed.cc,
        subject=parsed.subject,
        snippet=parsed.snippet,
        body_text=parsed.body_text,
        received_at=parsed.received_at,
        is_sent="SENT" in parsed.label_ids,
        is_unread="UNREAD" in parsed.label_ids,
        invite=parsed.invite.model_dump() if parsed.invite else None,
        message_id_header=parsed.message_id_header,
        references_header=parsed.references_header,
    )
    db.add(email)
    try:
        await db.flush()
    except IntegrityError:  # stored by a concurrent run: idempotent, not an error
        await db.rollback()
        return None
    for item in parsed.attachments:
        db.add(
            Attachment(
                user_id=user_id,
                email_id=email.id,
                filename=item.filename,
                mime_type=item.mime_type,
                size_bytes=item.size_bytes,
                gmail_attachment_id=item.attachment_id,
                sender_email=parsed.from_email,
                sender_name=parsed.from_name,
                subject=parsed.subject,
                received_at=parsed.received_at,
                gmail_thread_id=parsed.thread_id,
                gmail_message_id=parsed.gmail_id,
            )
        )
    await db.commit()
    return email


async def find_unstored_ids(
    db: AsyncSession, user_id: uuid.UUID, gmail_ids: list[str]
) -> list[str]:
    """The ids from `gmail_ids` that are not in the emails table yet (order kept)."""
    if not gmail_ids:
        return []
    stored = await db.scalars(
        select(Email.gmail_id).where(Email.user_id == user_id, Email.gmail_id.in_(gmail_ids))
    )
    stored_set = set(stored)
    return [gmail_id for gmail_id in gmail_ids if gmail_id not in stored_set]


async def set_unread(
    db: AsyncSession, user_id: uuid.UUID, gmail_ids: list[str], unread: bool
) -> None:
    """Mirror Gmail's read/unread state for already-stored emails."""
    if not gmail_ids:
        return
    await db.execute(
        update(Email)
        .where(Email.user_id == user_id, Email.gmail_id.in_(gmail_ids))
        .values(is_unread=unread)
    )
    await db.commit()


def encode_cursor(received_at: datetime, email_id: uuid.UUID) -> str:
    """Opaque pagination cursor for the inbox list."""
    raw = f"{received_at.isoformat()}|{email_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Read a cursor made by `encode_cursor`."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        timestamp, email_id = raw.split("|", 1)
        return datetime.fromisoformat(timestamp), uuid.UUID(email_id)
    except ValueError as error:
        raise BadRequest("Invalid cursor.") from error


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so user text is matched literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_emails(
    db: AsyncSession, user_id: uuid.UUID, filters: EmailFilters, cursor: str | None, limit: int
) -> tuple[list[tuple[Email, EmailInsight | None, bool]], str | None]:
    """Received emails, newest first, with insight and has-attachments flag."""
    has_attachments = exists().where(Attachment.email_id == Email.id).label("has_attachments")
    statement = (
        select(Email, EmailInsight, has_attachments)
        .outerjoin(EmailInsight, EmailInsight.email_id == Email.id)
        .where(Email.user_id == user_id, Email.is_sent.is_(False))
    )
    if filters.category:
        statement = statement.where(EmailInsight.category == filters.category)
    if filters.min_priority:
        statement = statement.where(EmailInsight.priority >= filters.min_priority)
    if filters.sentiment:
        statement = statement.where(EmailInsight.sentiment == filters.sentiment)
    if filters.unread is not None:
        statement = statement.where(Email.is_unread.is_(filters.unread))
    if filters.query:
        pattern = f"%{_escape_like(filters.query.strip())}%"
        statement = statement.where(
            or_(
                *(
                    column.ilike(pattern)
                    for column in (Email.subject, Email.from_email, Email.from_name, Email.snippet)
                )
            )
        )
    if cursor:
        cursor_time, cursor_id = decode_cursor(cursor)
        statement = statement.where(tuple_(Email.received_at, Email.id) < (cursor_time, cursor_id))

    statement = statement.order_by(Email.received_at.desc(), Email.id.desc()).limit(limit + 1)
    result = await db.execute(statement)
    rows: list[tuple[Email, EmailInsight | None, bool]] = [
        (row[0], row[1], bool(row[2])) for row in result.all()
    ]
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1][0]
        next_cursor = encode_cursor(last.received_at, last.id)
    return rows, next_cursor
