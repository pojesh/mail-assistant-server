"""Verify the scheduling agent's decision in code (never trust a model's "this time is free").

Pure functions: the caller passes a calendar snapshot and the rule context.
"""

from datetime import timedelta

from app.schemas.calendar import CalendarEvent
from app.schemas.scheduling import BOOKING_ACTIONS, INVITE_ACTIONS, SchedulingDecision, Slot
from app.services.slots import (
    CalendarSnapshot,
    Interval,
    RuleContext,
    check_slot,
    find_slots,
    parse_iso,
)

PROPOSAL_ACTIONS = {"propose_times", "propose_new_time"}
PROPOSAL_COUNT = 3


def _slot_problems(slot: Slot, ctx: RuleContext, calendar: CalendarSnapshot) -> list[str]:
    """Rule violations of one slot, as user-facing sentences."""
    try:
        interval = Interval(parse_iso(slot.start), parse_iso(slot.end))
    except ValueError:
        return ["The suggested time is not a valid date and time."]
    return [violation.detail for violation in check_slot(interval, ctx, calendar)]


def _use_invite_event(decision: SchedulingDecision, event: CalendarEvent | None) -> list[str]:
    """For invites, the event id and time come from the calendar, not from the model."""
    if event is None:
        return [] if decision.event_id else ["This invite was not found in your Google Calendar."]
    decision.event_id = event.id
    if not event.all_day:
        reason = decision.slot.reason if decision.slot else "The time in the invite"
        decision.slot = Slot(start=event.start, end=event.end, reason=reason)
    return []


def _top_up_proposals(
    decision: SchedulingDecision,
    ctx: RuleContext,
    calendar: CalendarSnapshot,
    duration_minutes: int,
) -> list[str]:
    """Keep only valid proposed slots, then fill up to three from the slot finder."""
    valid = [slot for slot in decision.proposed_slots if not _slot_problems(slot, ctx, calendar)]
    if len(valid) < PROPOSAL_COUNT:
        window = Interval(ctx.now, ctx.now + timedelta(days=ctx.prefs.search_horizon_days))
        for option in find_slots(duration_minutes, window, ctx, calendar, limit=PROPOSAL_COUNT * 2):
            candidate = Interval(parse_iso(option.start), parse_iso(option.end))
            taken = [Interval(parse_iso(slot.start), parse_iso(slot.end)) for slot in valid]
            if not any(candidate.overlaps(other) for other in taken):
                valid.append(Slot(start=option.start, end=option.end, reason=option.reason))
            if len(valid) == PROPOSAL_COUNT:
                break
    decision.proposed_slots = valid[:PROPOSAL_COUNT]
    return [] if valid else ["No free times were found in your search window."]


def proposal_duration(decision: SchedulingDecision, default_minutes: int) -> int:
    """Length of the meeting being proposed: from the model's first slot, else the default."""
    for slot in [decision.slot, *decision.proposed_slots]:
        if slot is None:
            continue
        try:
            minutes = int((parse_iso(slot.end) - parse_iso(slot.start)).total_seconds() // 60)
        except ValueError:
            continue
        if 15 <= minutes <= 240:
            return minutes
    return default_minutes


def check_decision(
    decision: SchedulingDecision,
    ctx: RuleContext,
    calendar: CalendarSnapshot,
    invite_event: CalendarEvent | None,
    sender: str,
) -> tuple[SchedulingDecision, list[str]]:
    """Return a corrected copy of the decision and warnings. Any warning forces approval."""
    checked = decision.model_copy(deep=True)
    warnings: list[str] = []
    if checked.action in INVITE_ACTIONS:
        warnings += _use_invite_event(checked, invite_event)
    if checked.action == "create_event":
        if checked.slot is None:
            warnings.append("No time was chosen for the meeting.")
        if not checked.attendees:
            checked.attendees = [sender]
    if checked.action in BOOKING_ACTIONS and checked.slot is not None:
        warnings += _slot_problems(checked.slot, ctx, calendar)
    if checked.action in PROPOSAL_ACTIONS:
        checked.needs_reply = True  # proposals are sent as a reply
    if checked.action in PROPOSAL_ACTIONS or checked.proposed_slots:
        duration = proposal_duration(checked, ctx.prefs.default_duration_minutes)
        problems = _top_up_proposals(checked, ctx, calendar, duration)
        if checked.action in PROPOSAL_ACTIONS:
            warnings += problems
    return checked, warnings
