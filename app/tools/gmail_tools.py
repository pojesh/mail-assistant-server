"""Read-only Gmail tools for the chat and digest agents."""

from typing import Any

from langchain.tools import ToolRuntime, tool
from sqlalchemy import select

from app.db import open_session
from app.google import gmail_api
from app.models import Email
from app.tools.context import AgentContext

MAX_RESULTS = 10


async def _stored_email_ids(context: AgentContext, thread_ids: list[str]) -> dict[str, str]:
    """Gmail thread id -> our latest stored email id (so chat can cite and start flows)."""
    if not thread_ids:
        return {}
    async with open_session() as db:
        rows = await db.execute(
            select(Email.thread_id, Email.id)
            .where(
                Email.user_id == context.user_id,
                Email.thread_id.in_(thread_ids),
                Email.is_sent.is_(False),
            )
            .order_by(Email.received_at)
        )
        return {thread_id: str(email_id) for thread_id, email_id in rows.all()}


@tool
async def search_threads(
    query: str, runtime: ToolRuntime[AgentContext], max_results: int = 10
) -> list[dict[str, Any]]:
    """Search the user's Gmail with Gmail search syntax, e.g. 'from:ravi invoice newer_than:30d'."""
    threads = await gmail_api.search_threads(
        runtime.context.user_id, query, max(1, min(max_results, MAX_RESULTS))
    )
    stored = await _stored_email_ids(runtime.context, [thread.thread_id for thread in threads])
    for thread in threads:
        thread.email_id = stored.get(thread.thread_id)
    return [thread.model_dump(mode="json") for thread in threads]


@tool
async def get_thread(thread_id: str, runtime: ToolRuntime[AgentContext]) -> dict[str, Any]:
    """Read one conversation (latest messages, quotes removed) by its Gmail thread id."""
    thread = await gmail_api.get_thread(runtime.context.user_id, thread_id)
    result = thread.model_dump(mode="json")
    result["email_id"] = (await _stored_email_ids(runtime.context, [thread_id])).get(thread_id)
    return result
