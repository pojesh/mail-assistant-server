"""Read-only calendar tools for the scheduling (and chat) agents.

All time math and rule checks happen in code (services/slots.py); the model only decides.
Bad arguments come back as {"error": ...} so the model can correct itself.
"""

from datetime import timedelta
from typing import Any

from langchain.tools import ToolRuntime, tool

from app.google import calendar_api
from app.schemas.preferences import MeetingPreferences
from app.services import availability
from app.services.slots import parse_iso
from app.tools.context import AgentContext

MAX_LISTED_EVENTS = 40


def _prefs(runtime: ToolRuntime[AgentContext]) -> MeetingPreferences:
    return MeetingPreferences.model_validate(runtime.context.context.get("meeting_preferences", {}))


def _is_vip(runtime: ToolRuntime[AgentContext]) -> bool:
    return bool(runtime.context.context.get("sender_is_vip", False))


@tool
async def get_meeting_preferences(runtime: ToolRuntime[AgentContext]) -> dict[str, Any]:
    """The user's meeting rules, timezone, whether the sender is a VIP, and relevant notes."""
    context = runtime.context.context
    return {
        "timezone": runtime.context.timezone,
        "meeting_preferences": _prefs(runtime).model_dump(mode="json"),
        "sender_is_vip": _is_vip(runtime),
        "preference_notes": context.get("notes", []),
    }


@tool
async def check_availability(
    start: str, end: str, runtime: ToolRuntime[AgentContext]
) -> dict[str, Any]:
    """Check if a time (ISO 8601 with UTC offset) is free and allowed by every hard rule.

    Returns {"ok": bool, "violations": [{"rule", "detail"}]}.
    """
    exclude = runtime.context.context.get("invite_event_id")
    result = await availability.check_availability(
        runtime.context.user_id,
        _prefs(runtime),
        runtime.context.timezone,
        start,
        end,
        _is_vip(runtime),
        exclude,
    )
    return result.model_dump()


@tool
async def find_free_slots(
    duration_minutes: int,
    runtime: ToolRuntime[AgentContext],
    window_start: str | None = None,
    window_end: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]] | dict[str, str]:
    """Find valid free slots (best first, each with a reason). Times are ISO 8601 with offset.

    Leave the window empty to search from now until the user's search horizon.
    """
    if not 15 <= duration_minutes <= 240:
        return {"error": "duration_minutes must be between 15 and 240"}
    try:
        slots = await availability.find_free_slots(
            runtime.context.user_id,
            _prefs(runtime),
            runtime.context.timezone,
            duration_minutes,
            window_start,
            window_end,
            _is_vip(runtime),
            runtime.context.context.get("proposed_times", []),
            max(1, min(limit, 10)),
        )
    except ValueError as error:
        return {"error": str(error)}
    return [slot.model_dump() for slot in slots]


@tool
async def find_invite_event(
    ical_uid: str, runtime: ToolRuntime[AgentContext]
) -> dict[str, Any] | None:
    """Look up the calendar event an invite email refers to (by its iCalendar UID)."""
    event = await calendar_api.find_event_by_ical_uid(runtime.context.user_id, ical_uid)
    return event.model_dump() if event else None


@tool
async def list_events(
    start: str, end: str, runtime: ToolRuntime[AgentContext]
) -> list[dict[str, Any]] | dict[str, str]:
    """List calendar events between two ISO 8601 times (at most 31 days)."""
    try:
        window_start, window_end = parse_iso(start), parse_iso(end)
    except ValueError as error:
        return {"error": str(error)}
    if window_end <= window_start or window_end - window_start > timedelta(days=31):
        return {"error": "end must be after start and at most 31 days later"}
    events = await calendar_api.list_events(runtime.context.user_id, window_start, window_end)
    return [
        {
            "summary": event.summary,
            "start": event.start,
            "end": event.end,
            "my_response": event.my_response,
        }
        for event in events[:MAX_LISTED_EVENTS]
    ]
