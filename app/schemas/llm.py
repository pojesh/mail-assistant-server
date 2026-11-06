"""Structured outputs produced by the LLM agents (validated with Pydantic)."""

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["action_required", "fyi", "meeting_request", "newsletter", "promotion", "other"]
Sentiment = Literal["positive", "neutral", "negative", "frustrated", "urgent"]


class ActionItem(BaseModel):
    task: str
    due: str | None = Field(None, description="ISO date if a deadline is stated")


class MeetingIntent(BaseModel):
    wants_meeting: bool
    proposed_times: list[str] = Field(default_factory=list, description="ISO 8601 with offset")
    duration_minutes: int | None = None
    topic: str | None = None


class TriageResult(BaseModel):
    category: Category
    priority: int = Field(ge=1, le=5)
    sentiment: Sentiment
    sentiment_score: float = Field(ge=0, le=1)
    summary: str = Field(max_length=200)
    action_items: list[ActionItem] = Field(default_factory=list)
    meeting_intent: MeetingIntent | None = None
    needs_reply: bool


class InputCheck(BaseModel):
    """Guardrail verdict on an incoming email."""

    suspicious: bool
    reasons: list[str] = Field(default_factory=list)


class OutputCheck(BaseModel):
    """Guardrail verdict on a drafted reply."""

    ok: bool
    problems: list[str] = Field(default_factory=list)


class DraftVariant(BaseModel):
    label: str = Field(description="Short name such as 'Short', 'Detailed', 'Polite decline'")
    body: str = Field(description="The reply text, without sign-off or signature")


class DraftSet(BaseModel):
    variants: list[DraftVariant] = Field(min_length=2, max_length=3)  # spec: 2–3 variants


class PreferenceSuggestion(BaseModel):
    """A preference the memory agent learned from how the user edited a draft."""

    text: str = Field(max_length=300)
    reason: str = Field(max_length=200)


class PreferenceSuggestions(BaseModel):
    suggestions: list[PreferenceSuggestion] = Field(default_factory=list, max_length=3)
