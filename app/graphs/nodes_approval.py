"""Graph nodes for human approval: queue the item, pause, then execute the user's decision.

`create_approval` and `await_approval` are separate nodes on purpose: on resume LangGraph
re-runs the interrupted node from its start, so the approval row must be written before it.
"""

import uuid
from typing import Any, Literal

from langgraph.types import interrupt

from app.graphs.nodes_triage import ids
from app.graphs.state import EmailState, thread_id_for
from app.schemas.approvals import ApprovalEmail, ApprovalPayload
from app.schemas.scheduling import SchedulingDecision
from app.services import approvals
from app.services.activity import log_activity
from app.services.notifications import create_notification

CALENDAR_KINDS = {
    "create_event": "event",
    "accept_invite": "invite_response",
    "tentative_invite": "invite_response",
    "decline_invite": "invite_response",
}


def approval_kind(state: EmailState) -> str:
    """A pending calendar write decides the kind; otherwise it is a reply draft."""
    decision = state.get("scheduling")
    if decision and not state.get("auto_approved"):
        return CALENDAR_KINDS.get(decision["action"], "draft")
    return "draft"


def build_payload(state: EmailState, kind: str) -> ApprovalPayload:
    """What the Approvals page shows. Calendar details are included unless already done
    automatically; `slots` are the times to pick from (new event) or being offered (reply)."""
    email = state["email"]
    decision = None
    if state.get("scheduling") and not state.get("auto_approved"):
        decision = SchedulingDecision.model_validate(state["scheduling"])
    slots = []
    if decision and kind == "event":
        slots = [slot for slot in [decision.slot, *decision.proposed_slots] if slot is not None]
    elif decision:
        slots = list(decision.proposed_slots)
    return ApprovalPayload(
        email=ApprovalEmail(
            subject=email.get("subject", ""),
            from_email=email.get("from_email", ""),
            from_name=email.get("from_name"),
            received_at=email.get("received_at", ""),
        ),
        summary=(state.get("triage") or {}).get("summary"),
        variants=(state.get("drafts") or {}).get("variants", []),
        scheduling=decision,
        slots=slots,
        reason=decision.reason if decision else None,
        autonomy_reason=state.get("autonomy_reason") if decision else None,
    )


def collect_warnings(state: EmailState) -> list[str]:
    warnings = list(state.get("scheduling_warnings", []))
    check = state.get("output_check")
    if check and not check.get("ok"):
        warnings += [f"Please check: {problem}" for problem in check.get("problems", [])]
    return warnings


async def create_approval_node(state: EmailState) -> dict[str, Any]:
    """Write the approvals row and notify the user."""
    user_id, email_id = ids(state)
    kind = approval_kind(state)
    payload = build_payload(state, kind)
    approval_id = await approvals.create_approval(
        user_id,
        email_id,
        kind,
        thread_id_for(state["user_id"], state["email_id"]),
        payload,
        collect_warnings(state),
    )
    label = {
        "draft": "Reply ready",
        "event": "Meeting to confirm",
        "invite_response": "Invite to answer",
    }[kind]
    await create_notification(
        user_id,
        "approval",
        f"{label}: {payload.email.subject or '(no subject)'}",
        link="/approvals",
    )
    await log_activity(
        user_id,
        "approvals",
        "create_approval",
        email_id=email_id,
        detail={"kind": kind, "approval_id": str(approval_id)},
    )
    return {"approval_id": str(approval_id), "outcome": None}


async def await_approval_node(state: EmailState) -> dict[str, Any]:
    """Pause until the user decides. The state is checkpointed; no request is held open."""
    decision = interrupt({"approval_id": state["approval_id"]})
    return {"decision": decision}


def route_after_execute_decision(
    state: EmailState,
) -> Literal["learn_from_edit", "await_approval", "scheduling_agent", "__end__"]:
    outcome = state.get("outcome")
    if outcome == "failed":
        return "await_approval"  # the item is pending again; the user can retry
    if outcome == "stale" and state.get("stale_count", 0) <= 1:
        return "scheduling_agent"  # the time is no longer free: decide again
    if outcome == "done" and (state.get("decision") or {}).get("edited_body"):
        return "learn_from_edit"
    return "__end__"


def approval_uuid(state: EmailState) -> uuid.UUID:
    return uuid.UUID(state["approval_id"])
