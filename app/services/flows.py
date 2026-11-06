"""Start and resume email-graph runs (new mail, "Draft reply", "Find a time", approvals)."""

import uuid
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Conflict
from app.graphs.email_graph import get_email_graph
from app.graphs.state import EmailState, Flow, email_to_state, thread_id_for
from app.logging import get_logger
from app.models import Approval, Email, EmailInsight
from app.schemas.approvals import ApprovalDecision
from app.schemas.emails import FlowStartResponse
from app.services.email_views import get_user_email

log = get_logger(__name__)


def _config(graph_thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": graph_thread_id}, "recursion_limit": 60}


async def _invoke(graph_input: EmailState | Command[Any], graph_thread_id: str) -> EmailState:
    """Run the graph until it ends or pauses for approval; returns the final state."""
    result = await get_email_graph().ainvoke(graph_input, _config(graph_thread_id))
    return cast(EmailState, result)


def fresh_state(
    email: Email, flow: Flow, triage: dict[str, Any] | None, input_check: dict[str, Any] | None
) -> EmailState:
    """A complete starting state. Every key is set, because LangGraph keeps values from
    earlier runs on the same thread unless they are overwritten."""
    return {
        "user_id": str(email.user_id),
        "email_id": str(email.id),
        "flow": flow,
        "email": email_to_state(email),
        "input_check": input_check or {"suspicious": False, "reasons": []},
        "triage": triage or {},
        "context": {},
        "scheduling": None,
        "scheduling_warnings": [],
        "auto_approved": False,
        "autonomy_reason": "",
        "scheduling_action_id": None,
        "drafts": None,
        "output_check": {},
        "redraft_count": 0,
        "approval_id": None,
        "decision": {},
        "outcome": None,
        "stale_count": 0,
        "result": {},
        "errors": [],
    }


async def run_new_email(email: Email) -> EmailState:
    """Triage a newly synced email (and draft/schedule if needed)."""
    state = fresh_state(email, "triage", None, None)
    return await _invoke(state, thread_id_for(state["user_id"], state["email_id"]))


def _insight_to_triage(insight: EmailInsight) -> dict[str, Any]:
    return {
        "category": insight.category,
        "priority": insight.priority,
        "sentiment": insight.sentiment,
        "sentiment_score": insight.sentiment_score,
        "summary": insight.summary,
        "action_items": insight.action_items,
        "meeting_intent": insight.meeting_intent,
        "needs_reply": True,  # the user asked for a reply
    }


async def start_flow(
    db: AsyncSession, user_id: uuid.UUID, email_id: uuid.UUID, flow: Flow
) -> FlowStartResponse:
    """Run "Draft reply" / "Find a time" with the same graph, guardrails and approvals."""
    email = await get_user_email(db, user_id, email_id)
    insight = await db.scalar(select(EmailInsight).where(EmailInsight.email_id == email.id))
    if insight is not None and insight.suspicious:
        raise Conflict("This email looks suspicious, so automation is turned off for it.")
    pending = await db.scalar(
        select(Approval.id).where(Approval.email_id == email.id, Approval.status == "pending")
    )
    if pending is not None:
        raise Conflict("Something for this email is already waiting in Approvals.")

    triage = _insight_to_triage(insight) if insight else None
    input_check = {"suspicious": False, "reasons": []}
    state = fresh_state(email, flow, triage, input_check)
    return summarise_run(await _invoke(state, thread_id_for(str(user_id), str(email.id))))


def summarise_run(result: EmailState) -> FlowStartResponse:
    """What the run ended with, for the UI."""
    if result.get("approval_id") and result.get("outcome") is None:
        return FlowStartResponse(
            status="approval_created",
            approval_id=uuid.UUID(result["approval_id"]),
            message="Ready for your review in Approvals.",
        )
    if result.get("scheduling_action_id"):
        return FlowStartResponse(
            status="auto_scheduled",
            approval_id=None,
            message="Done automatically. You can undo it from the notification.",
        )
    return FlowStartResponse(
        status="nothing_to_do", approval_id=None, message="Nothing needs to be done for this email."
    )


async def resume_approval(approval: Approval, decision: ApprovalDecision) -> EmailState:
    """Continue the paused graph with the user's decision (it then executes it)."""
    return await _invoke(Command(resume=decision.model_dump(mode="json")), approval.thread_id)
