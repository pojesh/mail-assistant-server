"""validate_decision logic: busy slots warn; invalid proposals are dropped and topped up."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.schemas.calendar import CalendarEvent
from app.schemas.preferences import MeetingPreferences
from app.schemas.scheduling import SchedulingDecision, Slot
from app.services.decision_checks import check_decision, proposal_duration
from app.services.slots import CalendarSnapshot, Interval, RuleContext, parse_iso

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 9, 8, 0, tzinfo=IST)
CTX = RuleContext(prefs=MeetingPreferences(), timezone=IST, now=NOW)
SENDER = "ravi@client.com"


def slot(day: int, hour: int, minute: int = 0, minutes: int = 30) -> Slot:
    start = datetime(2026, 11, day, hour, minute, tzinfo=IST)
    end = start + timedelta(minutes=minutes)
    return Slot(start=start.isoformat(), end=end.isoformat(), reason="test")


def decision(**fields: object) -> SchedulingDecision:
    base: dict[str, object] = {
        "action": "create_event",
        "needs_reply": False,
        "reason": "r",
        "confidence": 0.9,
    }
    return SchedulingDecision.model_validate({**base, **fields})


def test_free_slot_passes_and_sender_becomes_attendee() -> None:
    checked, warnings = check_decision(
        decision(slot=slot(10, 10)), CTX, CalendarSnapshot(), None, SENDER
    )
    assert warnings == []
    assert checked.attendees == [SENDER]


def test_busy_slot_becomes_a_warning() -> None:
    busy = CalendarSnapshot(
        busy=[Interval(parse_iso(slot(10, 10).start), parse_iso(slot(10, 11).start))]
    )
    _, warnings = check_decision(decision(slot=slot(10, 10)), CTX, busy, None, SENDER)
    assert warnings == ["Overlaps another event"]


def test_rule_breaking_slot_explains_the_rule() -> None:
    _, warnings = check_decision(decision(slot=slot(10, 13)), CTX, CalendarSnapshot(), None, SENDER)
    assert any("lunch" in warning for warning in warnings)


def test_missing_slot_is_a_warning() -> None:
    _, warnings = check_decision(decision(), CTX, CalendarSnapshot(), None, SENDER)
    assert warnings == ["No time was chosen for the meeting."]


def test_invalid_proposals_are_dropped_and_topped_up() -> None:
    proposals = [slot(10, 10), slot(10, 13), slot(14, 10)]  # ok, lunch, Saturday
    checked, warnings = check_decision(
        decision(action="propose_times", proposed_slots=proposals),
        CTX,
        CalendarSnapshot(),
        None,
        SENDER,
    )
    assert warnings == []
    assert len(checked.proposed_slots) == 3
    assert checked.proposed_slots[0].start == proposals[0].start
    assert checked.needs_reply  # proposals are always sent as a reply
    starts = [parse_iso(item.start) for item in checked.proposed_slots]
    assert all(start.hour != 13 and start.weekday() < 5 for start in starts)


def test_no_free_time_is_a_warning() -> None:
    full = CalendarSnapshot(busy=[Interval(NOW, NOW + timedelta(days=30))])
    checked, warnings = check_decision(decision(action="propose_times"), CTX, full, None, SENDER)
    assert checked.proposed_slots == []
    assert warnings == ["No free times were found in your search window."]


def test_invite_time_and_id_come_from_the_calendar() -> None:
    real = slot(11, 10)
    event = CalendarEvent(id="evt-1", start=real.start, end=real.end)
    model_guess = decision(action="accept_invite", slot=slot(12, 15), event_id="wrong")
    checked, warnings = check_decision(model_guess, CTX, CalendarSnapshot(), event, SENDER)
    assert checked.event_id == "evt-1"
    assert checked.slot is not None and checked.slot.start == real.start
    assert warnings == []


def test_invite_missing_from_calendar_is_a_warning() -> None:
    _, warnings = check_decision(
        decision(action="accept_invite"), CTX, CalendarSnapshot(), None, SENDER
    )
    assert warnings == ["This invite was not found in your Google Calendar."]


def test_accepting_a_conflicting_invite_is_a_warning() -> None:
    real = slot(11, 10)
    event = CalendarEvent(id="evt-1", start=real.start, end=real.end)
    busy = CalendarSnapshot(busy=[Interval(parse_iso(real.start), parse_iso(real.end))])
    _, warnings = check_decision(decision(action="accept_invite"), CTX, busy, event, SENDER)
    assert warnings == ["Overlaps another event"]


def test_proposal_duration() -> None:
    assert proposal_duration(decision(proposed_slots=[slot(10, 10, minutes=45)]), 30) == 45
    assert proposal_duration(decision(), 30) == 30
