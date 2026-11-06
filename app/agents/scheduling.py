"""Scheduling agent: decides what to do with an invite or meeting request.

The model makes the judgment call; deterministic tools do every time calculation, and the
graph re-checks its answer in code (validate_decision). It never writes to the calendar.
"""

import json
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain.agents.middleware.types import InputAgentState
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from app.agents.email_format import format_email
from app.errors import LLMUnavailable
from app.llm.prompts import render_prompt
from app.llm.provider import get_chat_model
from app.llm.retry import call_with_retry
from app.schemas.scheduling import SchedulingDecision
from app.tools import SCHEDULING_TOOLS
from app.tools.context import AgentContext

RECURSION_LIMIT = 16  # caps the tool-calling loop (each model or tool step counts)


def _request_text(email: dict[str, Any], triage: dict[str, Any]) -> str:
    """The email plus what triage already found (so the agent does not re-read everything)."""
    found = {
        "summary": triage.get("summary"),
        "meeting_intent": triage.get("meeting_intent"),
        "invite_uid": (email.get("invite") or {}).get("uid"),
    }
    return f"{format_email(email)}\n\nTriage result:\n{json.dumps(found, default=str)}"


async def decide_scheduling(
    email: dict[str, Any], triage: dict[str, Any], context: dict[str, Any], user_id: uuid.UUID
) -> SchedulingDecision:
    """Run the tool-calling agent and return its structured decision."""
    timezone = context.get("timezone", "UTC")
    now = datetime.now(ZoneInfo(timezone)).isoformat(timespec="minutes")
    agent = create_agent(
        get_chat_model("fast"),
        tools=SCHEDULING_TOOLS,
        system_prompt=render_prompt("scheduling", timezone=timezone, now=now),
        response_format=ToolStrategy(SchedulingDecision),
        context_schema=AgentContext,
    )
    agent_context = AgentContext(user_id=user_id, timezone=timezone, context=context)
    agent_input = InputAgentState(messages=[HumanMessage(_request_text(email, triage))])
    config: RunnableConfig = {"recursion_limit": RECURSION_LIMIT}

    result = await call_with_retry(
        lambda: agent.ainvoke(agent_input, config, context=agent_context),
        purpose="scheduling",
    )
    decision = result.get("structured_response")
    if not isinstance(decision, SchedulingDecision):
        raise LLMUnavailable("The scheduling assistant did not return a decision.")
    return decision
