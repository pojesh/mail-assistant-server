"""Relative dates for eval cases, so the cases stay valid whenever they are run.

D1 = the first working day at least 2 days from today, D2/D3 = the next working days,
SAT = the Saturday after D1. Text placeholders like {D1} become "Tuesday 10 November 2026".
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def _next_working_day(day: date) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def build_days(timezone: str) -> dict[str, date]:
    today = datetime.now(ZoneInfo(timezone)).date()
    d1 = _next_working_day(today + timedelta(days=1))
    d2 = _next_working_day(d1)
    d3 = _next_working_day(d2)
    saturday = d1 + timedelta(days=(5 - d1.weekday()) % 7 or 7)
    return {"D1": d1, "D2": d2, "D3": d3, "SAT": saturday}


def render(text: str, days: dict[str, date]) -> str:
    """Replace {D1}, {D2}, {D3}, {SAT} with readable dates."""
    for name, day in days.items():
        text = text.replace("{" + name + "}", day.strftime("%A %d %B %Y"))
    return text


def iso_at(days: dict[str, date], day_name: str, clock: str, timezone: str) -> str:
    """'D1', '11:00' -> '2026-11-10T11:00:00+05:30'"""
    day = days[day_name]
    hour, minute = (int(part) for part in clock.split(":"))
    return datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(timezone)
    ).isoformat()
