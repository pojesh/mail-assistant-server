"""Attachment search by file name, sender and subject (metadata only; contents are never read)."""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment
from app.schemas.emails import AttachmentItem
from app.services.email_views import to_attachment_item

SIMILARITY_THRESHOLD = 0.2  # pg_trgm: catches partial and misspelled file names ("invoce")


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_attachments(
    db: AsyncSession, user_id: uuid.UUID, query: str | None, limit: int = 50
) -> list[AttachmentItem]:
    """Full-text match (file name, sender, subject) or a fuzzy file-name match; newest first
    when there is no query."""
    statement = select(Attachment).where(Attachment.user_id == user_id)
    text = (query or "").strip()
    if not text:
        rows = await db.scalars(statement.order_by(Attachment.received_at.desc()).limit(limit))
        return [to_attachment_item(row) for row in rows]

    ts_query = func.plainto_tsquery("simple", text)
    similarity = func.similarity(Attachment.filename, text)
    rank = func.ts_rank(Attachment.search, ts_query) + similarity
    statement = (
        statement.where(
            or_(
                Attachment.search.op("@@")(ts_query),
                similarity > SIMILARITY_THRESHOLD,
                Attachment.filename.ilike(f"%{_escape_like(text)}%"),
            )
        )
        .order_by(rank.desc(), Attachment.received_at.desc())
        .limit(limit)
    )
    return [to_attachment_item(row) for row in await db.scalars(statement)]
