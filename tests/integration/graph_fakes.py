"""Fakes for graph tests: scripted agents, an in-memory Gmail (writes) and Google Calendar."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.agents import drafting, guardrail
from app.agents import triage as triage_agent
from app.errors import GoogleApiError
from app.google import calendar_api, gmail_writes
from app.graphs import nodes_execute, nodes_schedule
from app.schemas.calendar import CalendarEvent, NewEvent
from app.schemas.llm import (
    DraftSet,
    DraftVariant,
    InputCheck,
    OutputCheck,
    PreferenceSuggestion,
    TriageResult,
)
from app.schemas.scheduling import SchedulingDecision, Slot
from app.services import embeddings, retrieval

IST = ZoneInfo("Asia/Kolkata")


def future_slot(days_ahead: int = 3, hour: int = 10, minutes: int = 30) -> Slot:
    """A weekday slot at least `days_ahead` days from now, in IST."""
    day = datetime.now(IST).date() + timedelta(days=days_ahead)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=IST)
    return Slot(
        start=start.isoformat(), end=(start + timedelta(minutes=minutes)).isoformat(), reason="Fits"
    )


def triage_result(**fields: Any) -> TriageResult:
    base: dict[str, Any] = {
        "category": "action_required",
        "priority": 3,
        "sentiment": "neutral",
        "sentiment_score": 0.9,
        "summary": "Ravi asks for the report.",
        "needs_reply": True,
    }
    return TriageResult.model_validate({**base, **fields})


def decision(**fields: Any) -> SchedulingDecision:
    base: dict[str, Any] = {
        "action": "propose_times",
        "needs_reply": True,
        "reason": "You are free then.",
        "confidence": 0.9,
    }
    return SchedulingDecision.model_validate({**base, **fields})


@dataclass
class FakeWorld:
    """Scripted agent answers plus a record of every Google write."""

    input_check: InputCheck = field(default_factory=lambda: InputCheck(suspicious=False))
    triage: TriageResult = field(default_factory=triage_result)
    decision: SchedulingDecision | None = None
    output_checks: list[OutputCheck] = field(default_factory=list)  # consumed in order; default ok
    suggestions: list[PreferenceSuggestion] = field(default_factory=list)
    events: list[CalendarEvent] = field(default_factory=list)
    fail_drafts: int = 0  # how many create_draft calls fail before succeeding

    draft_calls: int = 0
    draft_problems: list[list[str] | None] = field(default_factory=list)
    scheduling_calls: int = 0
    labels: list[list[str]] = field(default_factory=list)
    saved_drafts: list[dict[str, Any]] = field(default_factory=list)
    created_events: list[NewEvent] = field(default_factory=list)
    responses: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    # --- agents ---
    async def check_input(self, email: dict[str, Any]) -> InputCheck:
        return self.input_check

    async def run_triage(self, email: dict[str, Any], timezone: str) -> TriageResult:
        return self.triage

    async def draft_replies(
        self,
        email: Any,
        triage: Any,
        context: Any,
        scheduling_text: Any = None,
        problems: Any = None,
    ) -> DraftSet:
        self.draft_calls += 1
        self.draft_problems.append(problems)
        return DraftSet(
            variants=[
                DraftVariant(label="Short", body="Hi Ravi,\n\nSure, sending it today."),
                DraftVariant(
                    label="Detailed",
                    body="Hi Ravi,\n\nThanks for the reminder. I will send it today.",
                ),
            ]
        )

    async def check_output(self, draft: str, email: Any, facts: str) -> OutputCheck:
        return self.output_checks.pop(0) if self.output_checks else OutputCheck(ok=True)

    async def decide_scheduling(
        self, email: Any, triage: Any, context: Any, user_id: uuid.UUID
    ) -> SchedulingDecision:
        self.scheduling_calls += 1
        assert self.decision is not None, "test forgot to script a scheduling decision"
        return self.decision

    async def suggest(self, original: str, final: str) -> list[PreferenceSuggestion]:
        return self.suggestions

    # --- Gmail writes ---
    async def apply_labels(
        self, user_id: uuid.UUID, message_id: str, add: list[str], remove: list[str]
    ) -> None:
        self.labels.append(add)

    async def create_draft(
        self,
        user_id: uuid.UUID,
        thread_id: str,
        to: str,
        subject: str,
        body: str,
        in_reply_to: Any,
        references: Any = None,
    ) -> str:
        if self.fail_drafts > 0:
            self.fail_drafts -= 1
            raise GoogleApiError()
        self.saved_drafts.append(
            {"thread_id": thread_id, "to": to, "subject": subject, "body": body}
        )
        return f"draft-{len(self.saved_drafts)}"

    # --- Calendar ---
    async def list_events(
        self, user_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        return list(self.events)

    async def find_event_by_ical_uid(self, user_id: uuid.UUID, uid: str) -> CalendarEvent | None:
        return next((event for event in self.events if event.ical_uid == uid), None)

    async def create_event(self, user_id: uuid.UUID, event: NewEvent) -> CalendarEvent:
        self.created_events.append(event)
        return CalendarEvent(id=f"new-{len(self.created_events)}", start=event.start, end=event.end)

    async def respond_to_invite(
        self, user_id: uuid.UUID, event_id: str, response: str
    ) -> CalendarEvent:
        self.responses.append((event_id, response))
        return CalendarEvent(id=event_id, start="", end="")

    async def delete_event(self, user_id: uuid.UUID, event_id: str) -> None:
        self.deleted.append(event_id)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(guardrail, "check_input", self.check_input)
        monkeypatch.setattr(guardrail, "check_output", self.check_output)
        monkeypatch.setattr(triage_agent, "triage", self.run_triage)
        monkeypatch.setattr(drafting, "draft_replies", self.draft_replies)
        monkeypatch.setattr(nodes_schedule, "decide_scheduling", self.decide_scheduling)
        monkeypatch.setattr(nodes_execute, "suggest_preferences", self.suggest)
        monkeypatch.setattr(gmail_writes, "apply_labels", self.apply_labels)
        monkeypatch.setattr(gmail_writes, "create_draft", self.create_draft)
        for name in (
            "list_events",
            "find_event_by_ical_uid",
            "create_event",
            "respond_to_invite",
            "delete_event",
        ):
            monkeypatch.setattr(calendar_api, name, getattr(self, name))

        async def fake_query(text: str) -> list[float]:
            return [0.1] * 768

        async def fake_documents(texts: list[str]) -> list[list[float]]:
            return [[0.1] * 768 for _ in texts]

        monkeypatch.setattr(retrieval, "embed_query", fake_query)
        monkeypatch.setattr(embeddings, "embed_documents", fake_documents)
