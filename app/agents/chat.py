"""Chat agent: answers questions about the user's mail and calendar, streaming its steps.

Read-only: its only "write" tools hand work to the email graph (approvals, same guardrails).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain.agents.middleware.types import InputAgentState
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from app.errors import AppError, LLMUnavailable
from app.llm.prompts import render_prompt
from app.llm.provider import get_chat_model
from app.llm.retry import OTHER_DELAYS, RATE_LIMIT_DELAYS, is_rate_limit, is_retryable
from app.logging import get_logger
from app.schemas.chat import ChatStepEvent, ChatTokenEvent, Citation
from app.tools import CHAT_TOOLS
from app.tools.context import AgentContext

log = get_logger(__name__)

RECURSION_LIMIT = 20
MAX_CITATIONS = 5
MAX_SEARCH_CITATIONS = 3
STEP_LABELS = {
    "search_threads": "Searching mail…",
    "get_thread": "Reading the conversation…",
    "search_attachments": "Searching files…",
    "search_my_sent_mail": "Looking at your past emails…",
    "list_events": "Checking calendar…",
    "find_free_slots": "Finding free times…",
    "start_draft_flow": "Drafting a reply for Approvals…",
    "start_schedule_flow": "Preparing a meeting for Approvals…",
}


def citations_from(message: ToolMessage) -> list[Citation]:
    """Emails mentioned in a search_threads / get_thread result."""
    if message.name not in ("search_threads", "get_thread"):
        return []
    try:
        data = json.loads(str(message.content))
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else [data]
    return [
        Citation(
            thread_id=item["thread_id"],
            subject=item.get("subject") or "(no subject)",
            email_id=item.get("email_id"),
        )
        for item in items
        if isinstance(item, dict) and item.get("thread_id")
    ]


@dataclass
class CitationTracker:
    """Emails the answer is based on: conversations the agent opened beat plain search hits."""

    opened: dict[str, Citation] = field(default_factory=dict)  # read with get_thread
    found: dict[str, Citation] = field(default_factory=dict)  # only seen in search results

    def add(self, message: ToolMessage) -> None:
        target = self.opened if message.name == "get_thread" else self.found
        for citation in citations_from(message):
            target.setdefault(citation.thread_id, citation)

    def best(self) -> list[Citation]:
        if self.opened:
            return list(self.opened.values())[:MAX_CITATIONS]
        return list(self.found.values())[:MAX_SEARCH_CITATIONS]


def _events_from_update(update: dict[str, Any], citations: CitationTracker) -> list[ChatStepEvent]:
    """Tool calls become step events; tool results add citations."""
    steps = []
    for node, change in update.items():
        for message in (change or {}).get("messages", []):
            if node == "model" and isinstance(message, AIMessage):
                for call in message.tool_calls:
                    steps.append(
                        ChatStepEvent(
                            tool=call["name"], label=STEP_LABELS.get(call["name"], "Working…")
                        )
                    )
            elif node == "tools" and isinstance(message, ToolMessage):
                citations.add(message)
    return steps


def _token_from(chunk: tuple[Any, dict[str, Any]]) -> ChatTokenEvent | None:
    """Answer text from the model node (tool-call messages carry no text)."""
    message, metadata = chunk
    if metadata.get("langgraph_node") != "model" or not isinstance(
        message, AIMessageChunk | AIMessage
    ):
        return None
    if getattr(message, "tool_calls", None) or getattr(message, "tool_call_chunks", None):
        return None
    text = message.text
    return ChatTokenEvent(text=text) if text else None


async def run_chat(
    history: list[AnyMessage], context: AgentContext, citations: CitationTracker
) -> AsyncIterator[ChatStepEvent | ChatTokenEvent]:
    """Stream steps and answer tokens. `citations` is filled in as tools return results.

    Retries (like call_with_retry) only while nothing has been streamed yet, so the user
    never sees a half answer followed by a second one.
    """
    now = datetime.now(ZoneInfo(context.timezone)).isoformat(timespec="minutes")
    agent = create_agent(
        get_chat_model("smart"),
        tools=CHAT_TOOLS,
        system_prompt=render_prompt("chat", timezone=context.timezone, now=now),
        context_schema=AgentContext,
    )
    config: RunnableConfig = {"recursion_limit": RECURSION_LIMIT}
    for attempt in range(len(RATE_LIMIT_DELAYS) + 1):
        streamed = False
        try:
            stream = agent.astream(
                InputAgentState(messages=list(history)),
                config,
                context=context,
                stream_mode=["updates", "messages"],
            )
            async for mode, chunk in stream:
                # "updates" chunks are {node: changes}; "messages" chunks are (message, metadata).
                if mode == "updates":
                    events: list[Any] = _events_from_update(cast(dict[str, Any], chunk), citations)
                else:
                    events = [_token_from(cast(tuple[Any, dict[str, Any]], chunk))]
                for event in events:
                    if event is not None:
                        streamed = True
                        yield event
            return
        except AppError:
            raise
        except Exception as error:
            if streamed or attempt == len(RATE_LIMIT_DELAYS) or not is_retryable(error):
                log.warning("chat_failed", error_type=type(error).__name__, streamed=streamed)
                raise LLMUnavailable() from error
            delays = RATE_LIMIT_DELAYS if is_rate_limit(error) else OTHER_DELAYS
            await asyncio.sleep(delays[attempt])
