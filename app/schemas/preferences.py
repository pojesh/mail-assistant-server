"""User preferences: writing style, meeting rules and the auto-scheduling policy."""

import uuid
from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: list[Weekday] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class TimeRange(BaseModel):
    start: time
    end: time

    @model_validator(mode="after")
    def check_order(self) -> "TimeRange":
        """A range must end after it starts."""
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class FocusBlock(BaseModel):
    day: Weekday
    start: time
    end: time

    @model_validator(mode="after")
    def check_order(self) -> "FocusBlock":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class MeetingPreferences(BaseModel):
    # --- hard rules (checked in code, never violated by auto actions) ---
    working_days: list[Weekday] = ["mon", "tue", "wed", "thu", "fri"]
    working_hours: TimeRange = TimeRange(start=time(9, 30), end=time(18, 0))
    no_meeting_days: list[Weekday] = []
    focus_blocks: list[FocusBlock] = []
    lunch_break: TimeRange | None = TimeRange(start=time(13, 0), end=time(14, 0))
    buffer_minutes: int = Field(10, ge=0, le=60)
    min_notice_hours: int = Field(4, ge=0, le=72)
    max_meetings_per_day: int = Field(6, ge=1, le=20)
    search_horizon_days: int = Field(10, ge=1, le=30)

    # --- soft preferences (used by the agent and for ranking) ---
    preferred_times: list[Literal["morning", "afternoon", "evening"]] = ["morning"]
    default_duration_minutes: int = Field(30, ge=15, le=240)
    add_video_link: bool = True

    # --- autonomy ---
    auto_schedule: Literal["off", "trusted", "all"] = "trusted"
    trusted_domains: list[str] = []  # e.g. the user's company domain
    auto_actions_per_day: int = Field(5, ge=0, le=20)
    undo_window_hours: int = Field(24, ge=1, le=72)
    vip_can_override_focus: bool = False

    @field_validator("trusted_domains")
    @classmethod
    def clean_domains(cls, domains: list[str]) -> list[str]:
        """Store domains lower-case without a leading '@'."""
        return sorted({domain.strip().lower().lstrip("@") for domain in domains if domain.strip()})


def validate_timezone(name: str) -> str:
    """Reject unknown IANA timezone names such as 'India/Mumbai'."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"Unknown timezone: {name}") from error
    return name


class PreferencesBody(BaseModel):
    """Everything on the Preferences page (GET response and PUT body)."""

    signature: str = Field("", max_length=2000)
    sign_off: str = Field("Best regards", max_length=100)
    tone: str = Field("friendly and professional", max_length=200)
    language: str = Field("English", max_length=64)
    timezone: str = "UTC"
    vip_senders: list[str] = Field(default_factory=list, max_length=200)
    meeting_preferences: MeetingPreferences = Field(default_factory=MeetingPreferences)

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str) -> str:
        return validate_timezone(value)

    @field_validator("vip_senders")
    @classmethod
    def clean_vips(cls, senders: list[str]) -> list[str]:
        """VIPs are email addresses or whole domains ('@client.com'), stored lower-case."""
        return sorted({sender.strip().lower() for sender in senders if sender.strip()})


NoteStatus = Literal["active", "suggested", "rejected"]


class PreferenceNoteOut(BaseModel):
    id: uuid.UUID
    text: str
    source: Literal["user", "learned"]
    status: NoteStatus
    created_at: datetime


class NoteCreate(BaseModel):
    text: str = Field(min_length=3, max_length=1000)


class NoteUpdate(BaseModel):
    """Edit the text, or accept/reject a learned suggestion."""

    text: str | None = Field(None, min_length=3, max_length=1000)
    status: NoteStatus | None = None
