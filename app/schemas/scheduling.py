"""The scheduling agent's decision, and the slot/violation shapes its tools return."""

from typing import Literal

from pydantic import BaseModel, Field

SchedulingAction = Literal[
    "accept_invite",
    "tentative_invite",
    "decline_invite",
    "propose_new_time",
    "create_event",
    "propose_times",
    "ignore",
]
INVITE_ACTIONS = {"accept_invite", "tentative_invite", "decline_invite", "propose_new_time"}
# Actions that put the user in a meeting: their time must be re-checked in code.
BOOKING_ACTIONS = {"accept_invite", "tentative_invite", "create_event"}


class Slot(BaseModel):
    start: str  # ISO 8601 with offset
    end: str
    reason: str  # shown to the user, e.g. "Morning, matches your preference"


class SchedulingDecision(BaseModel):
    action: SchedulingAction
    event_id: str | None = None  # existing event for invite actions
    slot: Slot | None = None  # for create_event (and the invite's own time)
    proposed_slots: list[Slot] = Field(default_factory=list, max_length=3)
    title: str | None = None
    attendees: list[str] = Field(default_factory=list)
    agenda: str | None = None
    add_video_link: bool = True
    needs_reply: bool  # e.g. propose_times / decline need a draft
    reason: str  # one-line explanation for the user
    confidence: float = Field(ge=0, le=1)


class Violation(BaseModel):
    """A broken hard rule, e.g. rule='focus_block', detail='Tue 9:00-11:00 is a focus block'."""

    rule: str
    detail: str


class AvailabilityResult(BaseModel):
    ok: bool
    violations: list[Violation] = Field(default_factory=list)


class SlotOption(BaseModel):
    """A valid free slot found by code, ranked by soft preferences."""

    id: str
    start: str
    end: str
    soft_score: int
    reason: str
