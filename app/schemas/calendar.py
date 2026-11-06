"""Calendar shapes returned by the Calendar API wrappers and the agenda route."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

ResponseStatus = Literal["accepted", "tentative", "declined", "needsAction"]


class CalendarEvent(BaseModel):
    id: str
    ical_uid: str | None = None
    summary: str = ""
    start: str  # ISO 8601 datetime, or a YYYY-MM-DD date for all-day events
    end: str
    all_day: bool = False
    my_response: ResponseStatus | None = None  # None when the user is the only attendee/organizer
    transparent: bool = False  # "show as available": does not block time
    organizer: str | None = None
    attendees: list[str] = Field(default_factory=list)
    html_link: str | None = None
    meet_link: str | None = None


class NewEvent(BaseModel):
    """An event the app creates after approval (or auto-scheduling)."""

    title: str
    start: str
    end: str
    attendees: list[str] = Field(default_factory=list)
    description: str | None = None
    add_video_link: bool = True


class AgendaEvent(CalendarEvent):
    auto_added: bool = False  # created or accepted automatically by the assistant
    action_id: uuid.UUID | None = None  # scheduling action (for Undo)
    undo_until: str | None = None


class ProposedEvent(BaseModel):
    """A pending approval shown as a dashed "ghost" event."""

    approval_id: uuid.UUID
    title: str
    start: str
    end: str
    reason: str


class AgendaResponse(BaseModel):
    events: list[AgendaEvent]
    proposals: list[ProposedEvent]
