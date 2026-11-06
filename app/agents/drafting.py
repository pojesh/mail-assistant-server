"""Drafting agent: 2–3 reply variants in the user's own voice (never sent automatically)."""

from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.email_format import format_email, neutralise
from app.llm.prompts import render_prompt
from app.llm.provider import get_chat_model
from app.llm.retry import call_with_retry
from app.schemas.llm import DraftSet


def tone_adjustment(triage: dict[str, Any], sender_is_vip: bool) -> str:
    """How the reply's tone should adapt to this sender (plain rules, not the model)."""
    sentiment = triage.get("sentiment")
    if sentiment in ("frustrated", "negative"):
        advice = "The sender is unhappy: acknowledge their concern first and be empathetic."
    elif sentiment == "urgent":
        advice = "The sender is in a hurry: answer the key point first, briefly."
    else:
        advice = "Match the sender's level of formality."
    if sender_is_vip:
        advice += " The sender is a VIP: be more formal and careful."
    return advice


def _context_block(context: dict[str, Any]) -> str:
    notes = "\n".join(f"- {note}" for note in context.get("notes", [])) or "- (none)"
    examples = "\n---\n".join(context.get("examples", [])) or "(none)"
    return neutralise(f"User notes:\n{notes}\n\nPast replies by the user:\n{examples}")


def finalize_body(body: str, sign_off: str, signature: str) -> str:
    """Append the user's sign-off and signature (the model is told not to write them)."""
    closing = f"{sign_off.strip().rstrip(',')}," if sign_off.strip() else ""
    parts = [body.strip(), "\n".join(part for part in (closing, signature.strip()) if part)]
    return "\n\n".join(part for part in parts if part)


async def draft_replies(
    email: dict[str, Any],
    triage: dict[str, Any],
    context: dict[str, Any],
    scheduling_text: str | None = None,
    problems: list[str] | None = None,
) -> DraftSet:
    """Write reply variants. `problems` are guardrail findings from a previous attempt."""
    timezone = context.get("timezone", "UTC")
    system = render_prompt(
        "draft",
        tone=context.get("tone", "friendly and professional"),
        language=context.get("language", "English"),
        timezone=timezone,
        today=datetime.now(ZoneInfo(timezone)).strftime("%A %Y-%m-%d"),
        tone_adjustment=tone_adjustment(triage, bool(context.get("sender_is_vip"))),
    )
    parts = [format_email(email), f"<context>\n{_context_block(context)}\n</context>"]
    if scheduling_text:
        parts.append(f"<scheduling>\n{scheduling_text}\n</scheduling>")
    if problems:
        fixes = "\n".join(f"- {problem}" for problem in problems)
        parts.append(f"Your previous draft had these problems. Fix all of them:\n{fixes}")

    model = get_chat_model("smart").with_structured_output(DraftSet)
    messages = [SystemMessage(system), HumanMessage("\n\n".join(parts))]
    result = await call_with_retry(lambda: model.ainvoke(messages), purpose="drafting")
    return cast(DraftSet, result)
