"""Every hard rule in services/slots.py, ranking, and timezone/DST edges."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.schemas.preferences import FocusBlock, MeetingPreferences
from app.services.slots import (
    CalendarSnapshot,
    Interval,
    RuleContext,
    check_slot,
    describe_slot,
    find_slots,
    parse_iso,
)

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 9, 8, 0, tzinfo=IST)  # Monday 08:00
MONDAY, TUESDAY, WEDNESDAY, SATURDAY = (date(2026, 11, day) for day in (9, 10, 11, 14))


def at(day: date, hour: int, minute: int = 0, minutes: int = 30, zone: ZoneInfo = IST) -> Interval:
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
    return Interval(start, start + timedelta(minutes=minutes))


def ctx(prefs: MeetingPreferences | None = None, is_vip: bool = False) -> RuleContext:
    return RuleContext(prefs=prefs or MeetingPreferences(), timezone=IST, now=NOW, is_vip=is_vip)


def rules(
    slot: Interval, context: RuleContext | None = None, calendar: CalendarSnapshot | None = None
) -> set[str]:
    return {
        violation.rule
        for violation in check_slot(slot, context or ctx(), calendar or CalendarSnapshot())
    }


def test_valid_slot_has_no_violations() -> None:
    assert rules(at(TUESDAY, 10)) == set()


def test_min_notice() -> None:
    assert "min_notice" in rules(at(MONDAY, 10))  # only 2 hours from now
    assert "min_notice" not in rules(at(MONDAY, 12))  # exactly 4 hours


def test_working_days_and_no_meeting_days() -> None:
    assert "working_days" in rules(at(SATURDAY, 10))
    prefs = MeetingPreferences(no_meeting_days=["wed"])
    assert "no_meeting_day" in rules(at(WEDNESDAY, 10), ctx(prefs))
    assert rules(at(TUESDAY, 10), ctx(prefs)) == set()


@pytest.mark.parametrize(
    ("hour", "minute", "allowed"),
    [(9, 0, False), (9, 30, True), (17, 30, True), (17, 45, False)],
)
def test_working_hours(hour: int, minute: int, allowed: bool) -> None:
    assert ("working_hours" not in rules(at(TUESDAY, hour, minute))) is allowed


def test_meeting_crossing_midnight_is_outside_working_hours() -> None:
    prefs = MeetingPreferences(working_hours={"start": time(0, 0), "end": time(23, 59)})
    assert "working_hours" in rules(at(TUESDAY, 23, 45), ctx(prefs))


def test_lunch_break() -> None:
    assert "lunch_break" in rules(at(TUESDAY, 12, 45))
    assert "lunch_break" not in rules(at(TUESDAY, 12, 30))  # ends exactly at 13:00
    assert "lunch_break" not in rules(at(TUESDAY, 13, 0), ctx(MeetingPreferences(lunch_break=None)))


def test_focus_blocks_and_vip_override() -> None:
    block = FocusBlock(day="tue", start=time(9, 30), end=time(11, 0))
    strict = MeetingPreferences(focus_blocks=[block])
    flexible = MeetingPreferences(focus_blocks=[block], vip_can_override_focus=True)
    assert "focus_block" in rules(at(TUESDAY, 10), ctx(strict))
    assert "focus_block" in rules(at(TUESDAY, 10), ctx(strict, is_vip=True))  # override is off
    assert "focus_block" not in rules(at(TUESDAY, 10), ctx(flexible, is_vip=True))
    assert "focus_block" in rules(at(TUESDAY, 10), ctx(flexible, is_vip=False))
    assert "focus_block" not in rules(at(WEDNESDAY, 10), ctx(strict))  # other day


def test_busy_and_buffer() -> None:
    calendar = CalendarSnapshot(busy=[at(TUESDAY, 11, minutes=60)])
    assert "busy" in rules(at(TUESDAY, 11, 30), calendar=calendar)
    assert "buffer" in rules(at(TUESDAY, 12, 5), calendar=calendar)  # 5 min after, buffer is 10
    assert rules(at(TUESDAY, 12, 10), calendar=calendar) == set()
    assert "buffer" in rules(at(TUESDAY, 10, 25), calendar=calendar)  # ends 5 min before


def test_daily_meeting_cap() -> None:
    full = CalendarSnapshot(meetings_per_day={TUESDAY: 6})
    assert "daily_cap" in rules(at(TUESDAY, 10), calendar=full)
    assert "daily_cap" not in rules(at(WEDNESDAY, 10), calendar=full)


def test_invalid_interval() -> None:
    slot = at(TUESDAY, 10)
    assert rules(Interval(slot.end, slot.start)) == {"invalid"}


def test_rules_use_the_users_timezone() -> None:
    # 04:30 UTC is 10:00 in India: allowed. 02:00 UTC is 07:30 in India: too early.
    utc = ZoneInfo("UTC")
    assert rules(at(TUESDAY, 4, 30, zone=utc)) == set()
    assert "working_hours" in rules(at(TUESDAY, 2, 0, zone=utc))


def test_daylight_saving_change() -> None:
    """New York leaves DST on 1 Nov 2026: 13:30 UTC is 9:30 before and 8:30 after."""
    new_york, utc = ZoneInfo("America/New_York"), ZoneInfo("UTC")
    context = RuleContext(MeetingPreferences(), new_york, datetime(2026, 10, 28, 0, 0, tzinfo=utc))
    before = at(date(2026, 10, 30), 13, 30, zone=utc)  # Fri 9:30 EDT
    after = at(date(2026, 11, 2), 13, 30, zone=utc)  # Mon 8:30 EST
    assert check_slot(before, context, CalendarSnapshot()) == []
    assert "working_hours" in {v.rule for v in check_slot(after, context, CalendarSnapshot())}
    assert check_slot(at(date(2026, 11, 2), 14, 30, zone=utc), context, CalendarSnapshot()) == []


def test_parse_iso_requires_offset() -> None:
    assert parse_iso("2026-11-10T10:00:00+05:30").utcoffset() == timedelta(hours=5, minutes=30)
    with pytest.raises(ValueError, match="no UTC offset"):
        parse_iso("2026-11-10T10:00:00")


def window(days: int = 10) -> Interval:
    return Interval(NOW, NOW + timedelta(days=days))


def test_find_slots_only_returns_valid_spread_slots() -> None:
    calendar = CalendarSnapshot(busy=[at(TUESDAY, 9, 30, minutes=120)])
    slots = find_slots(30, window(), ctx(), calendar)
    assert slots
    per_day: dict[str, int] = {}
    intervals = []
    for slot in slots:
        interval = Interval(parse_iso(slot.start), parse_iso(slot.end))
        assert check_slot(interval, ctx(), calendar) == []
        assert interval.start.minute % 15 == 0
        per_day[interval.start.date().isoformat()] = (
            per_day.get(interval.start.date().isoformat(), 0) + 1
        )
        intervals.append(interval)
    assert max(per_day.values()) <= 2
    assert not any(a.overlaps(b) for i, a in enumerate(intervals) for b in intervals[i + 1 :])


def test_find_slots_prefers_mornings() -> None:
    slots = find_slots(30, window(), ctx(), CalendarSnapshot(), limit=3)
    assert all(parse_iso(slot.start).hour < 12 for slot in slots)
    assert "Morning" in slots[0].reason


def test_sender_proposed_time_ranks_first() -> None:
    proposed = [at(WEDNESDAY, 10)]  # morning (+2), early (+1), their suggestion (+1)
    slots = find_slots(30, window(), ctx(), CalendarSnapshot(), proposed=proposed, limit=3)
    assert parse_iso(slots[0].start) == proposed[0].start
    assert "suggested" in slots[0].reason


def test_partly_overlapping_slot_is_not_the_senders_time() -> None:
    proposed = [at(WEDNESDAY, 10)]
    slots = find_slots(30, window(), ctx(), CalendarSnapshot(), proposed=proposed, limit=10)
    matches = [slot for slot in slots if "suggested" in slot.reason]
    assert [parse_iso(slot.start) for slot in matches] == [proposed[0].start]


def test_find_slots_respects_notice_and_horizon() -> None:
    prefs = MeetingPreferences(search_horizon_days=2)
    slots = find_slots(30, window(30), ctx(prefs), CalendarSnapshot(), limit=10)
    assert slots
    for slot in slots:
        start = parse_iso(slot.start)
        assert NOW + timedelta(hours=4) <= start <= NOW + timedelta(days=2)


def test_find_slots_empty_when_calendar_is_full() -> None:
    full = CalendarSnapshot(busy=[Interval(NOW, NOW + timedelta(days=11))])
    assert find_slots(30, window(), ctx(), full) == []


def test_describe_slot() -> None:
    text = describe_slot("2026-11-10T04:30:00+00:00", "2026-11-10T05:00:00+00:00", "Asia/Kolkata")
    assert text == "Tue 10 Nov, 10:00–10:30 (Asia/Kolkata)"
