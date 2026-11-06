"""Graph nodes for new mail: input guardrail, triage, Gmail labels, and routing."""

import uuid
from typing import Any, Literal

from app.agents import guardrail
from app.agents import triage as triage_agent
from app.errors import AppError
from app.google import gmail_writes
from app.graphs.state import EmailState
from app.schemas.llm import InputCheck, TriageResult
from app.services.activity import ActivityStatus, log_activity
from app.services.autonomy import is_vip
from app.services.insights import save_insight
from app.services.preferences import load_preferences

SKIP_CATEGORIES = {"newsletter", "promotion"}
INVITE_METHODS = {None, "REQUEST"}  # CANCEL / REPLY invites are only informational


def ids(state: EmailState) -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.UUID(state["user_id"]), uuid.UUID(state["email_id"])


async def guardrail_input_node(state: EmailState) -> dict[str, Any]:
    """Flag prompt-injection attempts before any agent acts on the email."""
    user_id, email_id = ids(state)
    check = await guardrail.check_input(state["email"])
    status: ActivityStatus = "blocked" if check.suspicious else "ok"
    await log_activity(
        user_id,
        "guardrail",
        "check_input",
        status=status,
        email_id=email_id,
        detail={"suspicious": check.suspicious, "reasons": check.reasons[:3]},
    )
    return {"input_check": check.model_dump(mode="json")}


async def triage_node(state: EmailState) -> dict[str, Any]:
    """Classify the email, apply the deterministic priority boost, and store the insight."""
    user_id, email_id = ids(state)
    prefs = await load_preferences(user_id)
    result = await triage_agent.triage(state["email"], prefs.timezone)
    boosted = triage_agent.boost_priority(
        result, is_vip(state["email"]["from_email"], prefs.vip_senders)
    )
    check = InputCheck.model_validate(state["input_check"])
    await save_insight(user_id, email_id, boosted, check)
    await log_activity(
        user_id,
        "triage",
        "triage",
        email_id=email_id,
        detail={
            "category": boosted.category,
            "priority": boosted.priority,
            "needs_reply": boosted.needs_reply,
        },
    )
    return {"triage": boosted.model_dump(mode="json")}


async def apply_labels_node(state: EmailState) -> dict[str, Any]:
    """Apply AI/* Gmail labels. A Gmail failure is logged but never stops the flow."""
    user_id, email_id = ids(state)
    labels = [gmail_writes.CATEGORY_LABELS[state["triage"]["category"]]]
    if state["input_check"]["suspicious"]:
        labels.append(gmail_writes.SUSPICIOUS_LABEL)
    try:
        await gmail_writes.apply_labels(user_id, state["email"]["gmail_id"], add=labels, remove=[])
    except AppError as error:
        await log_activity(
            user_id,
            "labeler",
            "apply_labels",
            status="error",
            tool="gmail.modify",
            email_id=email_id,
            detail={"code": error.code},
        )
        return {"errors": [*state.get("errors", []), "Could not apply Gmail labels"]}
    await log_activity(
        user_id,
        "labeler",
        "apply_labels",
        tool="gmail.modify",
        email_id=email_id,
        detail={"labels": labels},
    )
    return {}


def wants_scheduling(state: EmailState) -> bool:
    """A real invite, or a meeting request found by triage (or the user asked for it)."""
    if state.get("flow") == "schedule":
        return True
    if state.get("flow") == "draft":
        return False
    invite = state["email"].get("invite")
    if invite and invite.get("method") in INVITE_METHODS:
        return True
    triage = TriageResult.model_validate(state["triage"])
    if triage.category == "meeting_request":  # even if the model left meeting_intent empty
        return True
    return bool(triage.meeting_intent and triage.meeting_intent.wants_meeting)


def route_after_labels(state: EmailState) -> Literal["retrieve_context", "__end__"]:
    """Suspicious, newsletter, promotion or no reply needed -> stop; else continue."""
    if state["input_check"]["suspicious"]:
        return "__end__"
    if state["triage"]["category"] in SKIP_CATEGORIES:
        return "__end__"
    if wants_scheduling(state) or state["triage"]["needs_reply"]:
        return "retrieve_context"
    return "__end__"
