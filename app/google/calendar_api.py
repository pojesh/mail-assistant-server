"""Typed async wrappers around the Google Calendar API (primary calendar).

Write functions (create_event, respond_to_invite, delete_event) are only called by graph
execution nodes and Undo — never exposed to an LLM as tools.
"""

import uuid
from datetime import datetime
from typing import Any

from app.errors import NotFound
from app.google.clients import get_calendar, run_google
from app.schemas.calendar import CalendarEvent, NewEvent, ResponseStatus

CALENDAR_ID = "primary"
MAX_PAGES = 4  # 250 events per page is plenty for a 30-day window


def _to_event(item: dict[str, Any]) -> CalendarEvent:
    """Calendar API event dict -> CalendarEvent."""
    start, end = item.get("start", {}), item.get("end", {})
    attendees = item.get("attendees", [])
    me = next((person for person in attendees if person.get("self")), None)
    meet_link = item.get("hangoutLink")
    return CalendarEvent(
        id=item["id"],
        ical_uid=item.get("iCalUID"),
        summary=item.get("summary", "(no title)"),
        start=start.get("dateTime") or start.get("date", ""),
        end=end.get("dateTime") or end.get("date", ""),
        all_day="date" in start,
        my_response=me.get("responseStatus") if me else None,
        transparent=item.get("transparency") == "transparent",
        organizer=item.get("organizer", {}).get("email"),
        attendees=[person["email"] for person in attendees if person.get("email")],
        html_link=item.get("htmlLink"),
        meet_link=meet_link,
    )


async def list_events(user_id: uuid.UUID, start: datetime, end: datetime) -> list[CalendarEvent]:
    """Events between start and end, recurring events expanded, in start order."""
    calendar = await get_calendar(user_id)
    events: list[CalendarEvent] = []
    page_token: str | None = None
    for _ in range(MAX_PAGES):
        request = calendar.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
            pageToken=page_token,
        )
        response = await run_google(request)
        events += [
            _to_event(item)
            for item in response.get("items", [])
            if item.get("status") != "cancelled"
        ]
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return events


async def find_event_by_ical_uid(user_id: uuid.UUID, ical_uid: str) -> CalendarEvent | None:
    """The calendar event an email invite refers to (usually waiting for a response)."""
    calendar = await get_calendar(user_id)
    request = calendar.events().list(calendarId=CALENDAR_ID, iCalUID=ical_uid, maxResults=5)
    items = (await run_google(request)).get("items", [])
    return _to_event(items[0]) if items else None


async def create_event(user_id: uuid.UUID, event: NewEvent) -> CalendarEvent:
    """Create an event (with a Meet link if asked) and email the attendees the invite."""
    body: dict[str, Any] = {
        "summary": event.title,
        "description": event.description or "",
        "start": {"dateTime": event.start},
        "end": {"dateTime": event.end},
        "attendees": [{"email": address} for address in event.attendees],
    }
    if event.add_video_link:
        body["conferenceData"] = {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    calendar = await get_calendar(user_id)
    request = calendar.events().insert(
        calendarId=CALENDAR_ID, body=body, conferenceDataVersion=1, sendUpdates="all"
    )
    return _to_event(await run_google(request))


async def respond_to_invite(
    user_id: uuid.UUID, event_id: str, response: ResponseStatus
) -> CalendarEvent:
    """Set only the user's own attendee response; other attendees are left untouched."""
    calendar = await get_calendar(user_id)
    event = await run_google(calendar.events().get(calendarId=CALENDAR_ID, eventId=event_id))
    attendees = event.get("attendees", [])
    if not any(person.get("self") for person in attendees):
        raise NotFound("You are not an attendee of this event.")
    updated = [
        {**person, "responseStatus": response} if person.get("self") else person
        for person in attendees
    ]
    request = calendar.events().patch(
        calendarId=CALENDAR_ID, eventId=event_id, body={"attendees": updated}, sendUpdates="all"
    )
    return _to_event(await run_google(request))


async def delete_event(user_id: uuid.UUID, event_id: str) -> None:
    """Delete an event the assistant created (Undo). Attendees are notified."""
    calendar = await get_calendar(user_id)
    await run_google(
        calendar.events().delete(calendarId=CALENDAR_ID, eventId=event_id, sendUpdates="all")
    )
