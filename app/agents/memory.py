"""Memory agent: suggest new preference notes from how the user edited a draft."""

from typing import cast

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.email_format import neutralise
from app.llm.prompts import load_prompt
from app.llm.provider import get_chat_model
from app.llm.retry import call_with_retry
from app.schemas.llm import PreferenceSuggestion, PreferenceSuggestions

MIN_CHANGED_CHARACTERS = 15  # smaller edits are typo fixes, not preferences


def was_meaningfully_edited(original: str, final: str) -> bool:
    """Skip the model call when the user barely changed the draft."""
    if original.strip() == final.strip():
        return False
    return (
        abs(len(original) - len(final)) >= MIN_CHANGED_CHARACTERS
        or original.split() != final.split()
    )


async def suggest_preferences(original_draft: str, final_text: str) -> list[PreferenceSuggestion]:
    """Up to 3 suggested preferences; the user accepts or rejects them on the Preferences page."""
    if not was_meaningfully_edited(original_draft, final_text):
        return []
    content = (
        f"<draft>\n{neutralise(original_draft)}\n</draft>\n\n"
        f"<final>\n{neutralise(final_text)}\n</final>"
    )
    model = get_chat_model("fast").with_structured_output(PreferenceSuggestions)
    messages = [SystemMessage(load_prompt("memory")), HumanMessage(content)]
    result = cast(
        PreferenceSuggestions,
        await call_with_retry(lambda: model.ainvoke(messages), purpose="memory"),
    )
    return result.suggestions
