"""Read-only search tools: attachment metadata and the user's own past replies."""

from typing import Any

from langchain.tools import ToolRuntime, tool
from sqlalchemy import select

from app.db import open_session
from app.models import Email, SentEmbedding
from app.services import attachments
from app.services.embeddings import embed_query
from app.tools.context import AgentContext

SENT_RESULTS = 5
EXCERPT_LIMIT = 500


@tool
async def search_attachments(
    query: str, runtime: ToolRuntime[AgentContext]
) -> list[dict[str, Any]]:
    """Find attachments by file name, sender or subject (e.g. 'invoice pdf ravi')."""
    async with open_session() as db:
        items = await attachments.search_attachments(db, runtime.context.user_id, query, limit=10)
    return [item.model_dump(mode="json") for item in items]


@tool
async def search_my_sent_mail(
    query: str, runtime: ToolRuntime[AgentContext]
) -> list[dict[str, Any]]:
    """Find the user's own past emails that are most similar in meaning to the query."""
    vector = await embed_query(query)
    distance = SentEmbedding.embedding.cosine_distance(vector)
    statement = (
        select(SentEmbedding.chunk, Email.to, Email.subject, Email.received_at, Email.thread_id)
        .join(Email, Email.id == SentEmbedding.email_id)
        .where(SentEmbedding.user_id == runtime.context.user_id)
        .order_by(distance)
        .limit(SENT_RESULTS)
    )
    async with open_session() as db:
        rows = (await db.execute(statement)).all()
    return [
        {
            "to": row.to,
            "subject": row.subject,
            "date": row.received_at.isoformat(),
            "thread_id": row.thread_id,
            "excerpt": row.chunk[:EXCERPT_LIMIT],
        }
        for row in rows
    ]
