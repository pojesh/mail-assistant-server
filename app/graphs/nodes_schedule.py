"""Graph nodes for invites and meeting requests: agent -> code checks -> policy -> execution."""

import uuid
from datetime import timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.agents.scheduling import decide_scheduling
from app.errors import AppError
from app.google import calendar_api
from app.graphs.nodes_triage import ids
from app.graphs.state import EmailState
from app.schemas.calendar import CalendarEvent
from app.schemas.preferences import MeetingPreferences
from app.schemas.scheduling import INVITE_ACTIONS, SchedulingDecision
from app.services import scheduling_actions
from app.services.activity import log_activity
from app.services.autonomy import AutonomyInputs, decide_autonomy, is_trusted_domain
from app.services.availability import load_snapshot, rule_context
from app.services.decision_checks import check_decision
from app.services.notifications import create_notification
from app.services.slots import describe_slot, parse_iso


async def _find_invite_event(state: EmailState) -> CalendarEvent | None:
    user_id, _ = ids(state)
    uid = (state["email"].get("invite") or {}).get("uid")
    return await calendar_api.find_event_by_ical_uid(user_id, uid) if uid else None


async def scheduling_agent_node(state: EmailState) -> dict[str, Any]:
    """Let the agent decide (using read-only tools for all time math)."""
    user_id, email_id = ids(state)
    context = dict(state["context"])
    invite_event = await _find_invite_event(state)
    context["invite_event_id"] = invite_event.id if invite_event else None
    decision = await decide_scheduling(state["email"], state.get("triage") or {}, context, user_id)
    await log_activity(
        user_id,
        "scheduling",
        "decide",
        email_id=email_id,
        detail={
            "action": decision.action,
            "confidence": decision.confidence,
            "reason": decision.reason,
        },
    )
    return {"scheduling": decision.model_dump(mode="json")}


async def validate_decision_node(state: EmailState) -> dict[str, Any]:
    """Re-check every time in code; violations become warnings that force approval."""
    user_id, email_id = ids(state)
    context = state["context"]
    decision = SchedulingDecision.model_validate(state["scheduling"])
    prefs = MeetingPreferences.model_validate(context["meeting_preferences"])
    ctx = rule_context(prefs, context["timezone"], bool(context.get("sender_is_vip")))

    is_invite = decision.action in INVITE_ACTIONS
    invite_event = await _find_invite_event(state) if is_invite else None
    start, end = ctx.now, ctx.now + timedelta(days=prefs.search_horizon_days)
    if decision.slot:
        try:
            start = min(start, parse_iso(decision.slot.start))
            end = max(end, parse_iso(decision.slot.end))
        except ValueError:
            pass  # reported by check_decision
    exclude = invite_event.id if invite_event else decision.event_id
    snapshot = await load_snapshot(user_id, start, end, ZoneInfo(context["timezone"]), exclude)

    checked, warnings = check_decision(
        decision, ctx, snapshot, invite_event, state["email"]["from_email"]
    )
    await log_activity(
        user_id,
        "validator",
        "validate_decision",
        status="ok" if not warnings else "blocked",
        email_id=email_id,
        detail={"action": checked.action, "warnings": warnings[:5]},
    )
    return {"scheduling": checked.model_dump(mode="json"), "scheduling_warnings": warnings}


async def autonomy_gate_node(state: EmailState) -> dict[str, Any]:
    """Apply the user's auto-scheduling policy (plain code, see services/autonomy.py)."""
    user_id, email_id = ids(state)
    context = state["context"]
    decision = SchedulingDecision.model_validate(state["scheduling"])
    prefs = MeetingPreferences.model_validate(context["meeting_preferences"])
    sender = state["email"]["from_email"]
    verdict = decide_autonomy(
        AutonomyInputs(
            policy=prefs.auto_schedule,
            action=decision.action,
            suspicious=bool((state.get("input_check") or {}).get("suspicious")),
            has_warnings=bool(state.get("scheduling_warnings")),
            confidence=decision.confidence,
            auto_actions_today=await scheduling_actions.count_auto_actions_today(
                user_id, context["timezone"]
            ),
            auto_actions_per_day=prefs.auto_actions_per_day,
            sender_is_vip=bool(context.get("sender_is_vip")),
            sender_domain_trusted=is_trusted_domain(sender, prefs.trusted_domains),
            user_has_emailed_sender=await scheduling_actions.has_emailed(user_id, sender),
        )
    )
    await log_activity(
        user_id,
        "autonomy",
        "gate",
        status="auto" if verdict.auto else "ok",
        email_id=email_id,
        detail={"auto": verdict.auto, "reason": verdict.reason},
    )
    return {"auto_approved": verdict.auto, "autonomy_reason": verdict.reason}


def route_after_gate(
    state: EmailState,
) -> Literal["execute_calendar_action", "draft_replies", "create_approval", "__end__"]:
    if state.get("auto_approved"):
        return "execute_calendar_action"
    return route_to_reply_or_approval(state)


def route_to_reply_or_approval(
    state: EmailState,
) -> Literal["draft_replies", "create_approval", "__end__"]:
    """Without an automatic action: draft a reply if one is needed, else ask for approval."""
    decision = state["scheduling"] or {}
    if decision.get("action") == "ignore":
        return "draft_replies" if (state.get("triage") or {}).get("needs_reply") else "__end__"
    return "draft_replies" if decision.get("needs_reply") else "create_approval"


async def execute_calendar_action_node(state: EmailState) -> dict[str, Any]:
    """Auto path: do the calendar write. On failure, fall back to asking the user."""
    user_id, email_id = ids(state)
    decision = SchedulingDecision.model_validate(state["scheduling"])
    prefs = MeetingPreferences.model_validate(state["context"]["meeting_preferences"])
    try:
        row = await scheduling_actions.perform_calendar_action(
            user_id, email_id, decision, mode="auto", undo_window_hours=prefs.undo_window_hours
        )
    except AppError as error:
        await log_activity(
            user_id,
            "executor",
            decision.action,
            status="error",
            tool="calendar",
            email_id=email_id,
            detail={"code": error.code},
        )
        warnings = [
            *state.get("scheduling_warnings", []),
            f"Automatic scheduling failed: {error.message}",
        ]
        return {"auto_approved": False, "scheduling_warnings": warnings}
    await log_activity(
        user_id,
        "executor",
        decision.action,
        status="auto",
        tool="calendar",
        email_id=email_id,
        detail={"scheduling_action_id": str(row.id), "event_id": row.calendar_event_id},
    )
    return {
        "scheduling_action_id": str(row.id),
        "result": {"calendar_event_id": row.calendar_event_id},
    }


def route_after_execute(
    state: EmailState,
) -> Literal["notify", "draft_replies", "create_approval", "__end__"]:
    return "notify" if state.get("auto_approved") else route_to_reply_or_approval(state)


NOTIFY_VERBS = {
    "accept_invite": "Accepted",
    "tentative_invite": "Tentatively accepted",
    "create_event": "Added",
}


async def notify_node(state: EmailState) -> dict[str, Any]:
    """Tell the user what was done automatically, with an Undo button."""
    user_id, email_id = ids(state)
    decision = SchedulingDecision.model_validate(state["scheduling"])
    when = (
        describe_slot(decision.slot.start, decision.slot.end, state["context"]["timezone"])
        if decision.slot
        else ""
    )
    title = decision.title or state["email"].get("subject") or "Meeting"
    body = decision.reason
    if decision.action != "create_event":
        body += " The organizer has been notified of your response."
    action_id = state.get("scheduling_action_id")
    await create_notification(
        user_id,
        "auto_action",
        f"{NOTIFY_VERBS.get(decision.action, 'Scheduled')} “{title}” {when}".strip(),
        body,
        link=f"/inbox/{email_id}",
        undo_action_id=uuid.UUID(action_id) if action_id else None,
    )
    return {}


def route_after_notify(state: EmailState) -> Literal["draft_replies", "__end__"]:
    return "draft_replies" if (state["scheduling"] or {}).get("needs_reply") else "__end__"
