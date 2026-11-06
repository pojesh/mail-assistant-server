"""Apply the user's decision on an approval: validate it, claim it, resume the paused graph."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import BadRequest
from app.logging import get_logger
from app.models import Approval
from app.schemas.approvals import ApprovalDecision, ApprovalPayload, DecideResponse
from app.services import approvals, flows

log = get_logger(__name__)

OUTCOME_MESSAGES = {
    "done": "Done. Drafts are in Gmail → Drafts; nothing was sent.",
    "rejected": "Dismissed.",
    "stale": "That time is no longer free. A new suggestion is on its way to Approvals.",
    "failed": "Google did not accept the change. Please try again.",
}


def check_decision_fits(row: Approval, decision: ApprovalDecision) -> None:
    """Reject choices that do not exist on this approval (e.g. a third of two variants)."""
    if decision.action == "reject":
        return
    payload = ApprovalPayload.model_validate(row.payload)
    if decision.variant_index is not None and decision.variant_index >= len(payload.variants):
        raise BadRequest("That draft variant does not exist.")
    if decision.edited_body is not None and not payload.variants:
        raise BadRequest("This item has no draft to edit.")
    if decision.slot_index is not None and decision.slot_index >= len(payload.slots):
        raise BadRequest("That time slot does not exist.")
    if decision.response is not None and row.kind != "invite_response":
        raise BadRequest("Only invites can be answered with a response.")
    if (decision.title is not None or decision.attendees is not None) and row.kind != "event":
        raise BadRequest("Only new events can be renamed or get other attendees.")


async def decide(
    db: AsyncSession, user_id: uuid.UUID, approval_id: uuid.UUID, decision: ApprovalDecision
) -> DecideResponse:
    """Approve or reject, then run the rest of the graph (which performs the writes)."""
    row = await approvals.get_approval(db, user_id, approval_id)
    check_decision_fits(row, decision)
    row = await approvals.claim_for_decision(db, user_id, approval_id, decision)
    try:
        await flows.resume_approval(row, decision)
    except Exception:
        # Put it back so the user can try again; the error itself is handled by the caller.
        await approvals.record_outcome(
            row.id, "pending", {"error": "The action could not be completed."}
        )
        raise
    await db.refresh(row)
    outcome = {"approved": "done", "rejected": "rejected", "stale": "stale"}.get(
        row.status, "failed"
    )
    log.info("approval_decided", approval_id=str(row.id), outcome=outcome)
    return DecideResponse(
        approval=approvals.to_out(row),
        outcome=outcome,
        message=OUTCOME_MESSAGES[outcome],
    )
