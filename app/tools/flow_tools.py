"""Chat-only tools that hand work to the email graph. They never write anything themselves:
the graph applies the same guardrails, autonomy policy and approvals as for new mail."""

import uuid
from typing import Any

from langchain.tools import ToolRuntime, tool

from app.db import open_session
from app.errors import AppError
from app.graphs.state import Flow
from app.tools.context import AgentContext


async def _start(email_id: str, flow: Flow, runtime: ToolRuntime[AgentContext]) -> dict[str, Any]:
    # NOTE: imported here because the email graph itself imports app.tools (circular import).
    from app.services.flows import start_flow

    try:
        parsed_id = uuid.UUID(email_id)
    except ValueError:
        return {"error": "email_id must be an id returned by search_threads or get_thread"}
    try:
        async with open_session() as db:
            result = await start_flow(db, runtime.context.user_id, parsed_id, flow)
    except AppError as error:
        return {"error": error.message}
    return result.model_dump(mode="json")


@tool
async def start_draft_flow(email_id: str, runtime: ToolRuntime[AgentContext]) -> dict[str, Any]:
    """Draft reply variants for a stored email. They go to the Approvals page; nothing is sent."""
    return await _start(email_id, "draft", runtime)


@tool
async def start_schedule_flow(email_id: str, runtime: ToolRuntime[AgentContext]) -> dict[str, Any]:
    """Run the scheduling assistant for a stored email (same rules and auto-scheduling policy)."""
    return await _start(email_id, "schedule", runtime)
