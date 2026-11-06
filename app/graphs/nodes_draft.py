"""Graph nodes for replies: load context, draft variants, check them with the output guardrail."""

from typing import Any, Literal

from app.agents import drafting, guardrail
from app.graphs.nodes_triage import ids, wants_scheduling
from app.graphs.state import EmailState
from app.schemas.llm import DraftSet
from app.services import retrieval
from app.services.activity import log_activity
from app.services.slots import describe_slot

MAX_REDRAFTS = 1


async def retrieve_context_node(state: EmailState) -> dict[str, Any]:
    """Preferences, meeting rules, relevant notes and past replies for this email."""
    user_id, email_id = ids(state)
    context = await retrieval.get_context(user_id, state["email"])
    meeting = (state.get("triage") or {}).get("meeting_intent") or {}
    context["proposed_times"] = meeting.get("proposed_times", [])
    await log_activity(
        user_id,
        "retrieval",
        "retrieve_context",
        email_id=email_id,
        detail={"notes": len(context["notes"]), "examples": len(context["examples"])},
    )
    return {"context": context}


def route_after_context(state: EmailState) -> Literal["scheduling_agent", "draft_replies"]:
    return "scheduling_agent" if wants_scheduling(state) else "draft_replies"


def scheduling_text(state: EmailState) -> str | None:
    """What the reply must say about times, taken from the checked scheduling decision."""
    decision = state.get("scheduling")
    if not decision:
        return None
    timezone = state["context"].get("timezone", "UTC")
    action = decision["action"]
    if action in ("propose_times", "propose_new_time") and decision.get("proposed_slots"):
        options = "\n".join(
            f"- {describe_slot(slot['start'], slot['end'], timezone)}"
            for slot in decision["proposed_slots"]
        )
        intro = (
            "Suggest these times (the original time does not work):"
            if action == "propose_new_time"
            else "Offer these times:"
        )
        return f"{intro}\n{options}"
    slot = decision.get("slot")
    if action == "create_event" and slot:
        when = describe_slot(slot["start"], slot["end"], timezone)
        return f"Confirm the meeting for {when}; an invite will follow."
    if action == "decline_invite":
        reason = decision.get("reason", "")
        return f"Politely decline the meeting. Reason to convey, if appropriate: {reason}"
    return None


async def draft_replies_node(state: EmailState) -> dict[str, Any]:
    """Write reply variants (again, with the guardrail's problems, on a redraft)."""
    user_id, email_id = ids(state)
    previous_check = state.get("output_check")
    problems = (
        previous_check.get("problems") if previous_check and not previous_check.get("ok") else None
    )
    context = state["context"]
    draft_set = await drafting.draft_replies(
        state["email"], state.get("triage") or {}, context, scheduling_text(state), problems
    )
    for variant in draft_set.variants:
        variant.body = drafting.finalize_body(
            variant.body, context["sign_off"], context["signature"]
        )
    await log_activity(
        user_id,
        "drafting",
        "draft_replies",
        email_id=email_id,
        detail={"variants": len(draft_set.variants), "redraft": bool(problems)},
    )
    redrafts = state.get("redraft_count", 0) + (1 if problems else 0)
    return {"drafts": draft_set.model_dump(mode="json"), "redraft_count": redrafts}


def _facts(state: EmailState) -> str:
    """What the draft may state beyond the email itself."""
    context = state["context"]
    lines = [f"User notes: {note}" for note in context.get("notes", [])]
    lines.append(f"User signature: {context.get('sign_off', '')} {context.get('signature', '')}")
    text = scheduling_text(state)
    if text:
        lines.append(f"Scheduling: {text}")
    return "\n".join(lines)


async def guardrail_output_node(state: EmailState) -> dict[str, Any]:
    """Check all variants in one call (free tiers limit requests per minute)."""
    user_id, email_id = ids(state)
    draft_set = DraftSet.model_validate(state["drafts"])
    combined = "\n\n".join(
        f"[Variant: {variant.label}]\n{variant.body}" for variant in draft_set.variants
    )
    check = await guardrail.check_output(combined, state["email"], _facts(state))
    await log_activity(
        user_id,
        "guardrail",
        "check_output",
        status="ok" if check.ok else "blocked",
        email_id=email_id,
        detail={"ok": check.ok, "problems": check.problems[:5]},
    )
    return {"output_check": check.model_dump(mode="json")}


def route_after_output_check(state: EmailState) -> Literal["draft_replies", "create_approval"]:
    """Redraft once with the problems listed; after that, show the user with warnings."""
    check = state["output_check"]
    if not check["ok"] and state.get("redraft_count", 0) < MAX_REDRAFTS:
        return "draft_replies"
    return "create_approval"
