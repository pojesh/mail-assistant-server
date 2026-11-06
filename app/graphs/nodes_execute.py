"""Execute the user's approval (Gmail draft and/or calendar write), then learn from edits."""

from typing import Any

from app.agents.memory import suggest_preferences
from app.db import open_session
from app.errors import AppError
from app.google import gmail_writes
from app.graphs.nodes_approval import approval_kind, approval_uuid, build_payload
from app.graphs.nodes_triage import ids
from app.graphs.state import EmailState
from app.models import PreferenceNote
from app.schemas.approvals import ApprovalDecision, ApprovalPayload
from app.schemas.preferences import MeetingPreferences
from app.schemas.scheduling import SchedulingDecision, Slot
from app.services import approvals, availability, scheduling_actions
from app.services.activity import log_activity

BOOKING_RESPONSES = {"accepted", "tentative"}
RESPONSE_FOR_ACTION = {
    "accept_invite": "accepted",
    "tentative_invite": "tentative",
    "decline_invite": "declined",
}


def chosen_slot(payload: ApprovalPayload, decision: ApprovalDecision) -> Slot | None:
    if payload.slots:
        return payload.slots[decision.slot_index or 0]
    return payload.scheduling.slot if payload.scheduling else None


async def stale_reason(
    state: EmailState, payload: ApprovalPayload, decision: ApprovalDecision
) -> str | None:
    """Re-check the time right before booking it. Returns why it no longer works, if so."""
    scheduling = payload.scheduling
    if scheduling is None:
        return None
    response = decision.response or RESPONSE_FOR_ACTION.get(scheduling.action)
    if scheduling.action != "create_event" and response not in BOOKING_RESPONSES:
        return None  # declining never conflicts
    slot = chosen_slot(payload, decision)
    if slot is None:
        return None
    user_id, _ = ids(state)
    context = state["context"]
    result = await availability.check_availability(
        user_id,
        MeetingPreferences.model_validate(context["meeting_preferences"]),
        context["timezone"],
        slot.start,
        slot.end,
        bool(context.get("sender_is_vip")),
        exclude_event_id=scheduling.event_id,
    )
    return None if result.ok else result.violations[0].detail


def with_user_edits(
    scheduling: SchedulingDecision, decision: ApprovalDecision
) -> SchedulingDecision:
    """Apply the title/attendee changes the user made on the event card."""
    changes: dict[str, Any] = {}
    if decision.title:
        changes["title"] = decision.title
    if decision.attendees:
        changes["attendees"] = [str(address).lower() for address in decision.attendees]
    return scheduling.model_copy(update=changes) if changes else scheduling


def _reset_for_rescheduling(state: EmailState) -> dict[str, Any]:
    return {
        "outcome": "stale",
        "stale_count": state.get("stale_count", 0) + 1,
        "drafts": None,
        "output_check": {},
        "redraft_count": 0,
        "scheduling_warnings": [],
        "result": {},
    }


async def _save_draft(
    state: EmailState, payload: ApprovalPayload, decision: ApprovalDecision
) -> str:
    email = state["email"]
    body = decision.edited_body or payload.variants[decision.variant_index or 0].body
    user_id, _ = ids(state)
    return await gmail_writes.create_draft(
        user_id,
        email["thread_id"],
        email["from_email"],
        email["subject"],
        body,
        email.get("message_id_header"),
        email.get("references_header"),
    )


async def execute_decision_node(state: EmailState) -> dict[str, Any]:
    """Approve -> write to Gmail/Calendar. Failures put the item back to pending for a retry."""
    user_id, email_id = ids(state)
    approval_id = approval_uuid(state)
    decision = ApprovalDecision.model_validate(state["decision"])
    if decision.action == "reject":
        await log_activity(
            user_id, "user", "reject", email_id=email_id, detail={"approval_id": str(approval_id)}
        )
        return {"outcome": "rejected"}

    kind = approval_kind(state)
    payload = build_payload(state, kind)
    result = dict(state.get("result") or {})  # keeps a calendar write that already succeeded
    try:
        if kind != "draft" and "calendar_event_id" not in result:
            reason = await stale_reason(state, payload, decision)
            if reason:
                await approvals.record_outcome(approval_id, "stale", {"reason": reason})
                await log_activity(
                    user_id,
                    "executor",
                    "stale",
                    status="blocked",
                    email_id=email_id,
                    detail={"reason": reason},
                )
                return _reset_for_rescheduling(state)
            assert payload.scheduling is not None
            prefs = MeetingPreferences.model_validate(state["context"]["meeting_preferences"])
            row = await scheduling_actions.perform_calendar_action(
                user_id,
                email_id,
                with_user_edits(payload.scheduling, decision),
                mode="approved",
                slot=chosen_slot(payload, decision),
                response=decision.response,
                undo_window_hours=prefs.undo_window_hours,
            )
            result["calendar_event_id"] = row.calendar_event_id
        if payload.variants:
            result["draft_id"] = await _save_draft(state, payload, decision)
    except AppError as error:
        await approvals.record_outcome(approval_id, "pending", {**result, "error": error.message})
        await log_activity(
            user_id,
            "executor",
            "execute",
            status="error",
            email_id=email_id,
            detail={"code": error.code},
        )
        return {"outcome": "failed", "result": result}

    await approvals.record_outcome(approval_id, "approved", result)
    await log_activity(
        user_id,
        "executor",
        "execute",
        tool="gmail/calendar",
        email_id=email_id,
        detail={"kind": kind, **result},
    )
    return {"outcome": "done", "result": result}


async def learn_from_edit_node(state: EmailState) -> dict[str, Any]:
    """Memory agent: suggest preferences from the user's edits (they accept/reject them later)."""
    user_id, email_id = ids(state)
    decision = ApprovalDecision.model_validate(state["decision"])
    variants = (state.get("drafts") or {}).get("variants", [])
    if not decision.edited_body or not variants:
        return {}
    original = variants[min(decision.variant_index or 0, len(variants) - 1)]["body"]
    try:
        suggestions = await suggest_preferences(original, decision.edited_body)
    except AppError as error:
        await log_activity(
            user_id,
            "memory",
            "suggest",
            status="error",
            email_id=email_id,
            detail={"code": error.code},
        )
        return {}
    async with open_session() as db:
        for suggestion in suggestions:
            db.add(
                PreferenceNote(
                    user_id=user_id, text=suggestion.text, source="learned", status="suggested"
                )
            )
        await db.commit()
    await log_activity(
        user_id, "memory", "suggest", email_id=email_id, detail={"suggestions": len(suggestions)}
    )
    return {}
