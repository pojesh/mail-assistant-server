"""Pure-Python meeting rules: check a time against the user's hard rules, and find free slots.

No I/O and no LLM here: callers pass in the busy times and meeting counts. All datetimes are
timezone-aware; rules about days and hours are evaluated in the user's own timezone.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.schemas.preferences import WEEKDAYS, MeetingPreferences, TimeRange
from app.schemas.scheduling import SlotOption, Violation

STEP = timedelta(minutes=15)
MAX_SLOTS_PER_DAY = 2  # spread suggestions over several days


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def overlaps(self, other: "Interval", margin: timedelta = timedelta(0)) -> bool:
        """True if the intervals overlap once `other` is widened by `margin` on both sides."""
        return self.start < other.end + margin and self.end > other.start - margin


@dataclass(frozen=True)
class RuleContext:
    """Everything the rules need besides the calendar."""

    prefs: MeetingPreferences
    timezone: ZoneInfo
    now: datetime
    is_vip: bool = False


@dataclass(frozen=True)
class CalendarSnapshot:
    """The user's calendar, already fetched: busy times and meeting counts per local date."""

    busy: list[Interval] = field(default_factory=list)
    meetings_per_day: dict[date, int] = field(default_factory=dict)


def parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 datetime. A missing UTC offset is an error (times must be exact)."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"Time has no UTC offset: {value}")
    return parsed


def _overlaps_times(start: time, end: time, block: TimeRange) -> bool:
    return start < block.end and end > block.start


def _calendar_violations(
    slot: Interval, ctx: RuleContext, calendar: CalendarSnapshot
) -> list[Violation]:
    """Conflicts with existing events (including buffers) and the daily meeting cap."""
    violations = []
    buffer = timedelta(minutes=ctx.prefs.buffer_minutes)
    for busy in calendar.busy:
        if slot.overlaps(busy):
            violations.append(Violation(rule="busy", detail="Overlaps another event"))
            break
        if slot.overlaps(busy, margin=buffer):
            detail = f"Leaves less than your {ctx.prefs.buffer_minutes}-minute buffer"
            violations.append(Violation(rule="buffer", detail=detail))
            break
    local_date = slot.start.astimezone(ctx.timezone).date()
    if calendar.meetings_per_day.get(local_date, 0) >= ctx.prefs.max_meetings_per_day:
        detail = f"You already have {ctx.prefs.max_meetings_per_day} meetings that day"
        violations.append(Violation(rule="daily_cap", detail=detail))
    return violations


def _day_violations(start: datetime, end: datetime, ctx: RuleContext) -> list[Violation]:
    """Working days, no-meeting days, working hours, lunch and focus blocks (local time)."""
    prefs, violations = ctx.prefs, []
    day = WEEKDAYS[start.weekday()]
    day_name = start.strftime("%a")
    if day not in prefs.working_days:
        violations.append(Violation(rule="working_days", detail=f"{day_name} is not a working day"))
    if day in prefs.no_meeting_days:
        violations.append(
            Violation(rule="no_meeting_day", detail=f"{day_name} is a no-meeting day")
        )

    hours = prefs.working_hours
    if start.date() != end.date() or start.time() < hours.start or end.time() > hours.end:
        detail = f"Outside working hours ({hours.start:%H:%M}-{hours.end:%H:%M})"
        violations.append(Violation(rule="working_hours", detail=detail))
    lunch = prefs.lunch_break
    if lunch and _overlaps_times(start.time(), end.time(), lunch):
        detail = f"Overlaps lunch ({lunch.start:%H:%M}-{lunch.end:%H:%M})"
        violations.append(Violation(rule="lunch_break", detail=detail))

    vip_override = ctx.is_vip and prefs.vip_can_override_focus
    for block in prefs.focus_blocks:
        block_range = TimeRange(start=block.start, end=block.end)
        if (
            block.day == day
            and _overlaps_times(start.time(), end.time(), block_range)
            and not vip_override
        ):
            detail = f"{day_name} {block.start:%H:%M}-{block.end:%H:%M} is a focus block"
            violations.append(Violation(rule="focus_block", detail=detail))
    return violations


def check_slot(slot: Interval, ctx: RuleContext, calendar: CalendarSnapshot) -> list[Violation]:
    """Every hard rule this slot breaks (empty list = the slot is allowed)."""
    if slot.end <= slot.start:
        return [Violation(rule="invalid", detail="The meeting ends before it starts")]
    violations = []
    if slot.start < ctx.now + timedelta(hours=ctx.prefs.min_notice_hours):
        detail = f"Less than {ctx.prefs.min_notice_hours} hours' notice"
        violations.append(Violation(rule="min_notice", detail=detail))
    local_start = slot.start.astimezone(ctx.timezone)
    local_end = slot.end.astimezone(ctx.timezone)
    violations += _day_violations(local_start, local_end, ctx)
    violations += _calendar_violations(slot, ctx, calendar)
    return violations


def _time_of_day(moment: datetime) -> str:
    if moment.hour < 12:
        return "morning"
    return "afternoon" if moment.hour < 17 else "evening"


def _score(
    slot: Interval, ctx: RuleContext, window: Interval, proposed: list[Interval]
) -> tuple[int, str]:
    """Soft score and a short human reason: +2 preferred time, +1 early, +1 sender's suggestion."""
    score, reasons = 0, []
    period = _time_of_day(slot.start.astimezone(ctx.timezone))
    if period in ctx.prefs.preferred_times:
        score += 2
        reasons.append(f"{period.capitalize()}, matches your preference")
    if any(item.start <= slot.start and slot.end <= item.end for item in proposed):
        score += 1
        reasons.append("matches a time they suggested")
    if slot.start < window.start + (window.end - window.start) / 3:
        score += 1
        reasons.append("early in your search window")
    reason = "; ".join(reasons) or "Free and within your working hours"
    return score, reason[0].upper() + reason[1:]


def _round_up(moment: datetime) -> datetime:
    """Next quarter-hour boundary (or the moment itself if already on one)."""
    moment = moment.replace(second=0, microsecond=0)
    extra = (-moment.minute) % 15
    return moment + timedelta(minutes=extra)


def find_slots(
    duration_minutes: int,
    window: Interval,
    ctx: RuleContext,
    calendar: CalendarSnapshot,
    proposed: list[Interval] | None = None,
    limit: int = 10,
) -> list[SlotOption]:
    """Valid slots inside `window`, best first, at most two per day and never overlapping."""
    earliest = ctx.now + timedelta(hours=ctx.prefs.min_notice_hours)
    latest = ctx.now + timedelta(days=ctx.prefs.search_horizon_days)
    search = Interval(max(window.start, earliest), min(window.end, latest))
    duration = timedelta(minutes=duration_minutes)

    candidates: list[tuple[int, Interval, str]] = []
    cursor = _round_up(search.start)
    while cursor + duration <= search.end:
        slot = Interval(cursor, cursor + duration)
        if not check_slot(slot, ctx, calendar):
            score, reason = _score(slot, ctx, search, proposed or [])
            candidates.append((score, slot, reason))
        cursor += STEP

    candidates.sort(key=lambda item: (-item[0], item[1].start))
    chosen: list[tuple[int, Interval, str]] = []
    per_day: dict[date, int] = {}
    for score, slot, reason in candidates:
        local_day = slot.start.astimezone(ctx.timezone).date()
        if per_day.get(local_day, 0) >= MAX_SLOTS_PER_DAY or any(
            slot.overlaps(c[1]) for c in chosen
        ):
            continue
        chosen.append((score, slot, reason))
        per_day[local_day] = per_day.get(local_day, 0) + 1
        if len(chosen) == limit:
            break

    return [
        SlotOption(
            id=f"slot-{number}",
            start=slot.start.astimezone(ctx.timezone).isoformat(),
            end=slot.end.astimezone(ctx.timezone).isoformat(),
            soft_score=score,
            reason=reason,
        )
        for number, (score, slot, reason) in enumerate(chosen, start=1)
    ]


def describe_slot(start: str, end: str, timezone: str) -> str:
    """'Tue 11 Nov, 10:00–10:30 (Asia/Kolkata)' — for drafts, notifications and the UI."""
    zone = ZoneInfo(timezone)
    local_start, local_end = parse_iso(start).astimezone(zone), parse_iso(end).astimezone(zone)
    return f"{local_start:%a %d %b, %H:%M}–{local_end:%H:%M} ({timezone})"
