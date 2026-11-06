"""Triage agent: category, priority, sentiment, summary, action items and meeting intent."""

from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.email_format import format_email
from app.llm.prompts import render_prompt
from app.llm.provider import get_chat_model
from app.llm.retry import call_with_retry
from app.schemas.llm import TriageResult

LOW_VALUE_CATEGORIES = {"newsletter", "promotion"}


async def triage(email: dict[str, Any], timezone: str) -> TriageResult:
    """Classify one email. The result is later adjusted by `boost_priority` (plain code)."""
    today = datetime.now(ZoneInfo(timezone)).strftime("%A %Y-%m-%d")
    system = render_prompt("triage", timezone=timezone, today=today)
    model = get_chat_model("fast").with_structured_output(TriageResult)
    messages = [SystemMessage(system), HumanMessage(format_email(email))]
    result = await call_with_retry(lambda: model.ainvoke(messages), purpose="triage")
    return cast(TriageResult, result)


def boost_priority(result: TriageResult, sender_is_vip: bool) -> TriageResult:
    """Deterministic adjustments: +1 for a VIP sender, +1 if frustrated/urgent, capped at 5.

    Newsletters and promotions never need a reply, whatever the model said.
    """
    priority = result.priority
    if sender_is_vip:
        priority += 1
    if result.sentiment in ("frustrated", "urgent"):
        priority += 1
    needs_reply = result.needs_reply and result.category not in LOW_VALUE_CATEGORIES
    return result.model_copy(update={"priority": min(priority, 5), "needs_reply": needs_reply})
