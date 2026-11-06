"""Guardrail agent: flag prompt-injection emails, and check drafts before the user sees them."""

import re
from typing import Any, cast

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.email_format import format_email, neutralise
from app.llm.prompts import load_prompt
from app.llm.provider import get_chat_model
from app.llm.retry import call_with_retry
from app.schemas.llm import InputCheck, OutputCheck

# Obvious injection phrases are caught in code too (flagged even if the model misses them).
INJECTION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|prompts|rules)",
        r"disregard (all |any )?(the )?(previous|prior|above) (instructions|prompts)",
        r"\byou are now (a|an|the)\b",
        r"\b(system|developer) (prompt|message|instructions?)\s*:",
        r"\b(reveal|print|show|send) (me )?(your|the) (system )?(prompt|instructions|api key)",
        r"\b(ai|assistant|chatbot|language model)\s*[,:]\s*(please )?"
        r"(ignore|forward|send|delete|accept|approve|schedule)",
        r"\bnew instructions\s*:",
    )
]


def find_injection_phrases(text: str) -> list[str]:
    """Phrases in the text that match known prompt-injection patterns."""
    return [match.group(0) for pattern in INJECTION_PATTERNS if (match := pattern.search(text))]


async def check_input(email: dict[str, Any]) -> InputCheck:
    """Is this email trying to manipulate the assistant? Code rules first, then the model."""
    text = f"{email.get('subject', '')}\n{email.get('body_text') or ''}"
    phrases = find_injection_phrases(text)
    if phrases:
        return InputCheck(
            suspicious=True, reasons=[f'Contains "{phrase}"' for phrase in phrases[:3]]
        )

    model = get_chat_model("fast").with_structured_output(InputCheck)
    messages = [SystemMessage(load_prompt("guardrail_input")), HumanMessage(format_email(email))]
    result = await call_with_retry(lambda: model.ainvoke(messages), purpose="guardrail_input")
    return cast(InputCheck, result)


async def check_output(draft_body: str, email: dict[str, Any], facts: str) -> OutputCheck:
    """Does the draft invent facts or commitments, or leak anything? Returns the problems."""
    model = get_chat_model("fast").with_structured_output(OutputCheck)
    content = (
        f"{format_email(email)}\n\n<facts>\n{neutralise(facts) or 'None'}\n</facts>\n\n"
        f"<draft>\n{neutralise(draft_body)}\n</draft>"
    )
    messages = [SystemMessage(load_prompt("guardrail_output")), HumanMessage(content)]
    result = await call_with_retry(lambda: model.ainvoke(messages), purpose="guardrail_output")
    return cast(OutputCheck, result)
