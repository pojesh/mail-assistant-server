"""Digest agent: turns the day's facts into a headline and a short summary.

NOTE: the facts (needs-attention emails, today's meetings) are gathered by code in
services/digest.py from stored insights and the calendar, so this is one model call instead
of a tool-calling loop — cheaper on free tiers and it cannot miss an email.
"""

import json
from typing import Any, cast

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.email_format import neutralise
from app.llm.prompts import render_prompt
from app.llm.provider import get_chat_model
from app.llm.retry import call_with_retry
from app.schemas.digest import DigestText


async def write_digest(facts: dict[str, Any], timezone: str, today: str) -> DigestText:
    """Headline + summary for the facts. Uses only what it is given."""
    model = get_chat_model("fast").with_structured_output(DigestText)
    content = f"<facts>\n{neutralise(json.dumps(facts, default=str))}\n</facts>"
    messages = [
        SystemMessage(render_prompt("digest", timezone=timezone, today=today)),
        HumanMessage(content),
    ]
    result = await call_with_retry(lambda: model.ainvoke(messages), purpose="digest")
    return cast(DigestText, result)
