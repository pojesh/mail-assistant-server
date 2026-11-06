"""State of one email's run through the email graph.

NOTE: values are plain JSON dicts (`model.model_dump(mode="json")`), not Pydantic objects.
LangGraph checkpoints the state to Postgres, and restoring arbitrary classes from a
checkpoint is being phased out; dicts also survive model changes while an approval waits.
Nodes validate them back into models (TriageResult, SchedulingDecision, ...) when needed.
"""

from typing import Any, Literal, TypedDict

from app.models import Email

Flow = Literal["triage", "draft", "schedule"]


class EmailState(TypedDict, total=False):
    user_id: str
    email_id: str
    flow: Flow  # "triage" for new mail; "draft"/"schedule" when started by the user or chat
    email: dict[str, Any]  # parsed email incl. optional invite info
    input_check: dict[str, Any]  # InputCheck
    triage: dict[str, Any]  # TriageResult (after the priority boost)
    context: dict[str, Any]  # preferences + meeting prefs + RAG examples + notes
    scheduling: dict[str, Any] | None  # SchedulingDecision
    scheduling_warnings: list[str]
    auto_approved: bool
    autonomy_reason: str
    scheduling_action_id: str | None  # set when a calendar action was executed automatically
    drafts: dict[str, Any] | None  # DraftSet (bodies already include sign-off + signature)
    output_check: dict[str, Any]  # OutputCheck
    redraft_count: int
    approval_id: str | None
    decision: dict[str, Any]  # user's approval payload from the UI
    outcome: Literal["done", "rejected", "stale", "failed"] | None
    stale_count: int
    result: dict[str, Any]  # ids of created draft / event / response
    errors: list[str]


def email_to_state(email: Email) -> dict[str, Any]:
    """The email fields agents may see (JSON-safe)."""
    return {
        "id": str(email.id),
        "gmail_id": email.gmail_id,
        "thread_id": email.thread_id,
        "from_email": email.from_email,
        "from_name": email.from_name,
        "to": email.to,
        "cc": email.cc,
        "subject": email.subject,
        "snippet": email.snippet,
        "body_text": email.body_text or "",
        "received_at": email.received_at.isoformat(),
        "invite": email.invite,
        "message_id_header": email.message_id_header,
        "references_header": email.references_header,
    }


def thread_id_for(user_id: str, email_id: str) -> str:
    """LangGraph thread id: one checkpointed conversation per email."""
    return f"email:{user_id}:{email_id}"
