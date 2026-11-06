"""Availability = the user's real calendar + their hard rules (pure logic in slots.py)."""

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.google import calendar_api
from app.schemas.calendar import CalendarEvent
from app.schemas.preferences import MeetingPreferences
from app.schemas.scheduling import AvailabilityResult, SlotOption, Violation
from app.services.slots import (
    CalendarSnapshot,
    Interval,
    RuleContext,
    check_slot,
    find_slots,
    parse_iso,
)

# NOTE: busy times come from events.list rather than freebusy.query: freebusy cannot tell
# which block is the invite being evaluated (a pending invite would always "conflict with
# itself"), and events.list also gives the per-day meeting counts in the same call.


def _blocks_time(event: CalendarEvent, exclude_event_id: str | None) -> bool:
    """Timed, opaque events the user has not declined occupy their time."""
    if event.all_day or event.transparent or event.id == exclude_event_id:
        return False
    return event.my_response != "declined"


def snapshot_from_events(
    events: list[CalendarEvent], timezone: ZoneInfo, exclude_event_id: str | None = None
) -> CalendarSnapshot:
    """Busy intervals and meetings-per-day from a list of events."""
    busy: list[Interval] = []
    per_day: dict[date, int] = {}
    for event in events:
        if not _blocks_time(event, exclude_event_id):
            continue
        interval = Interval(parse_iso(event.start), parse_iso(event.end))
        busy.append(interval)
        local_day = interval.start.astimezone(timezone).date()
        per_day[local_day] = per_day.get(local_day, 0) + 1
    return CalendarSnapshot(busy=busy, meetings_per_day=per_day)


async def load_snapshot(
    user_id: uuid.UUID,
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    exclude_event_id: str | None = None,
) -> CalendarSnapshot:
    """Fetch events around [start, end] (whole local days, for the daily cap)."""
    events = await calendar_api.list_events(
        user_id, start - timedelta(days=1), end + timedelta(days=1)
    )
    return snapshot_from_events(events, timezone, exclude_event_id)


def rule_context(prefs: MeetingPreferences, timezone: str, is_vip: bool) -> RuleContext:
    return RuleContext(
        prefs=prefs, timezone=ZoneInfo(timezone), now=datetime.now(UTC), is_vip=is_vip
    )


async def check_availability(
    user_id: uuid.UUID,
    prefs: MeetingPreferences,
    timezone: str,
    start: str,
    end: str,
    is_vip: bool = False,
    exclude_event_id: str | None = None,
) -> AvailabilityResult:
    """Is [start, end] free and allowed by every hard rule? Lists each broken rule."""
    try:
        slot = Interval(parse_iso(start), parse_iso(end))
    except ValueError as error:
        return AvailabilityResult(
            ok=False, violations=[Violation(rule="invalid", detail=str(error))]
        )
    ctx = rule_context(prefs, timezone, is_vip)
    snapshot = await load_snapshot(user_id, slot.start, slot.end, ctx.timezone, exclude_event_id)
    violations = check_slot(slot, ctx, snapshot)
    return AvailabilityResult(ok=not violations, violations=violations)


async def find_free_slots(
    user_id: uuid.UUID,
    prefs: MeetingPreferences,
    timezone: str,
    duration_minutes: int,
    window_start: str | None = None,
    window_end: str | None = None,
    is_vip: bool = False,
    proposed_times: list[str] | None = None,
    limit: int = 10,
) -> list[SlotOption]:
    """Valid free slots in the window (default: from now to the search horizon), best first."""
    ctx = rule_context(prefs, timezone, is_vip)
    start = parse_iso(window_start) if window_start else ctx.now
    end = (
        parse_iso(window_end) if window_end else ctx.now + timedelta(days=prefs.search_horizon_days)
    )
    window = Interval(start, end)
    duration = timedelta(minutes=duration_minutes)
    proposed = [
        Interval(parse_iso(item), parse_iso(item) + duration) for item in proposed_times or []
    ]
    snapshot = await load_snapshot(user_id, window.start, window.end, ctx.timezone)
    return find_slots(duration_minutes, window, ctx, snapshot, proposed, limit)
