"""End-to-end user flows with the real agents: the whole email graph (guardrail -> triage ->
labels -> RAG context -> drafting/scheduling -> approval -> execute) plus the user's actions
through the real API (approve, edit, undo) and a chat hand-off.

Only Google is faked: Gmail writes and Google Calendar are recorded in memory, so nothing is
ever sent, drafted or booked for real. Each flow lists the steps it checked; checks marked
"(info)" are reported but do not fail the flow.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.agents.chat import CitationTracker, run_chat
from app.db import open_session
from app.google import calendar_api, gmail_api, gmail_writes
from app.graphs.email_graph import init_email_graph
from app.main import app
from app.models import ActivityLog, Approval, Email, EmailInsight, SchedulingAction, User
from app.schemas.calendar import CalendarEvent, NewEvent
from app.schemas.chat import ChatStepEvent
from app.schemas.google import ParsedEmail
from app.schemas.preferences import MeetingPreferences
from app.schemas.scheduling import Slot
from app.security.session import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, create_signed_token
from app.services import chat as chat_service
from app.services import flows
from app.services.availability import rule_context
from app.services.emails import save_email
from app.services.preferences import load_preferences
from tests.evals import scheduling_eval, triage_eval
from tests.evals.dates import iso_at
from tests.scenarios.conftest import measure, truncate_all
from tests.scenarios.mailbox import Mailbox, create_user
from tests.scenarios.recorder import Recorder

PROFILE, SCHEDULING_CASES, DAYS = scheduling_eval.load(None)
SCHEDULING = {case["id"]: case for case in SCHEDULING_CASES}
TRIAGE = {case["id"]: case for case in triage_eval.load_cases(None)}
SIGN_OFF = "Best,\nPat Lee"


@dataclass
class FakeGoogle:
    """Records every Gmail/Calendar write; serves a fixed calendar."""

    events: list[CalendarEvent] = field(default_factory=list)
    labels: list[list[str]] = field(default_factory=list)
    drafts: list[dict[str, Any]] = field(default_factory=list)
    created: list[NewEvent] = field(default_factory=list)
    responses: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

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
        self.drafts.append({"thread_id": thread_id, "to": to, "subject": subject, "body": body})
        return f"draft-{len(self.drafts)}"

    async def list_events(
        self, user_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        return list(self.events)

    async def find_event_by_ical_uid(self, user_id: uuid.UUID, uid: str) -> CalendarEvent | None:
        return next((event for event in self.events if event.ical_uid == uid), None)

    async def create_event(self, user_id: uuid.UUID, event: NewEvent) -> CalendarEvent:
        self.created.append(event)
        return CalendarEvent(id=f"new-{len(self.created)}", start=event.start, end=event.end)

    async def respond_to_invite(
        self, user_id: uuid.UUID, event_id: str, response: str
    ) -> CalendarEvent:
        self.responses.append((event_id, response))
        return CalendarEvent(id=event_id, start="", end="")

    async def delete_event(self, user_id: uuid.UUID, event_id: str) -> None:
        self.deleted.append(event_id)

    def install(self, patch: pytest.MonkeyPatch) -> None:
        patch.setattr(gmail_writes, "apply_labels", self.apply_labels)
        patch.setattr(gmail_writes, "create_draft", self.create_draft)
        for name in (
            "list_events",
            "find_event_by_ical_uid",
            "create_event",
            "respond_to_invite",
            "delete_event",
        ):
            patch.setattr(calendar_api, name, getattr(self, name))


@dataclass
class FlowWorld:
    user: User
    google: FakeGoogle
    client: AsyncClient
    patch: pytest.MonkeyPatch


def client_for(user_id: uuid.UUID) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    client.cookies.set(
        SESSION_COOKIE, create_signed_token({"sub": str(user_id)}, timedelta(hours=1))
    )
    client.cookies.set(CSRF_COOKIE, "csrf-value")
    client.headers[CSRF_HEADER] = "csrf-value"
    return client


@pytest.fixture
async def world(scenario_database: None) -> AsyncIterator[FlowWorld]:
    init_email_graph(InMemorySaver())
    user = await create_user(PROFILE["meeting_preferences"])
    google = FakeGoogle()
    with pytest.MonkeyPatch.context() as patch:
        google.install(patch)
        async with client_for(user.id) as client:
            yield FlowWorld(user, google, client, patch)
    await truncate_all()


# --- helpers --------------------------------------------------------------------------------------


def parsed(email: dict[str, Any], gmail_id: str) -> ParsedEmail:
    """An eval email dict (tests/evals/triage_eval.to_email) as a synced Gmail message."""
    return ParsedEmail(
        gmail_id=gmail_id,
        thread_id=f"th-{gmail_id}",
        label_ids=["INBOX", "UNREAD"],
        from_email=email["from_email"],
        from_name=email.get("from_name"),
        to=["pat@example.com"],
        subject=email["subject"],
        snippet=email["body_text"][:140],
        body_text=email["body_text"],
        received_at=datetime.now(UTC) - timedelta(minutes=5),
        message_id_header=f"<{gmail_id}@mail.example>",
        invite=email.get("invite"),
    )


async def store(user: User, message: ParsedEmail) -> Email:
    async with open_session() as db:
        row = await save_email(db, user.id, message)
    assert row is not None
    return row


async def rows(model: Any, *where: Any) -> list[Any]:
    async with open_session() as db:
        return list(await db.scalars(select(model).where(*where)))


async def insight_of(email: Email) -> EmailInsight | None:
    found = await rows(EmailInsight, EmailInsight.email_id == email.id)
    return found[0] if found else None


def finish(
    recorder: Recorder, flow_id: str, title: str, checks: dict[str, bool], details: dict[str, Any]
) -> None:
    required = {name: ok for name, ok in checks.items() if not name.startswith("(info)")}
    row = {
        "id": flow_id,
        "title": title,
        "checks": checks,
        "details": details,
        "passed": all(required.values()),
    }
    recorder.add("flows", row)
    failed = [name for name, ok in required.items() if not ok]
    assert not failed, f"{title}: failed steps {failed}; details {details}"


# --- flows ----------------------------------------------------------------------------------------


async def test_flow_client_question_to_gmail_draft(world: FlowWorld, recorder: Recorder) -> None:
    """A client asks a question -> triage -> RAG finds a past answer -> draft variants wait in
    Approvals -> the user edits one and approves -> it is saved as a Gmail draft (never sent)."""
    history = Mailbox.build(list(TRIAGE.values()), DAYS)
    sent_only = Mailbox(messages=[m for m in history.messages if "SENT" in m.label_ids], events=[])
    await sent_only.seed(world.user.id)  # the user's past replies, embedded for retrieval
    email = await store(
        world.user,
        ParsedEmail(
            gmail_id="flow-lena",
            thread_id="th-flow-lena",
            label_ids=["INBOX", "UNREAD"],
            from_email="lena@datapipe.io",
            from_name="Lena Fischer",
            to=["pat@example.com"],
            subject="Export API rate limit?",
            snippet="What is the rate limit on your export endpoint?",
            body_text="Hi Pat,\n\nWe are planning a nightly sync with your platform. "
            "What is the rate limit on "
            "the export endpoint, and can it be raised for a one-off backfill?\n\nThanks,\nLena",
            received_at=datetime.now(UTC) - timedelta(minutes=3),
            message_id_header="<flow-lena@mail.example>",
        ),
    )

    with measure() as meter:
        await flows.run_new_email(email)
    recorder.time("flows", "New email -> approval ready", meter)

    insight = await insight_of(email)
    pending = await rows(Approval, Approval.email_id == email.id, Approval.status == "pending")
    variants = pending[0].payload.get("variants", []) if pending else []
    retrieval = await rows(
        ActivityLog, ActivityLog.email_id == email.id, ActivityLog.action == "retrieve_context"
    )
    examples = retrieval[0].detail.get("examples", 0) if retrieval else 0
    checks = {
        "triaged as needing a reply": bool(insight and insight.needs_reply),
        "category is action_required": bool(insight and insight.category == "action_required"),
        "Gmail label applied": bool(world.google.labels),
        "RAG retrieved past replies": examples >= 1,
        "draft approval waiting with 2-3 variants": len(pending) == 1 and 2 <= len(variants) <= 3,
        "sign-off and signature added by code": bool(variants)
        and all(v["body"].endswith(SIGN_OFF) for v in variants),
        "nothing written to Gmail before approval": not world.google.drafts,
        "(info) draft reuses the standing answer (600 requests/min)": any(
            "600" in v["body"] for v in variants
        ),
    }
    details: dict[str, Any] = {
        "category": insight.category if insight else None,
        "variants": [v["label"] for v in variants],
        "examples_retrieved": examples,
    }
    if pending and variants:
        edited = variants[0]["body"].replace(
            SIGN_OFF, "I can also share our backfill guide.\n\n" + SIGN_OFF
        )
        with measure() as meter:
            response = await world.client.post(
                f"/api/approvals/{pending[0].id}/decide",
                json={"action": "approve", "variant_index": 0, "edited_body": edited},
            )
        recorder.time("flows", "Approve -> Gmail draft", meter)
        body = response.json()
        checks["user approval executed"] = (
            response.status_code == 200 and body.get("outcome") == "done"
        )
        checks["Gmail draft has the user's edit"] = (
            bool(world.google.drafts) and "backfill guide" in world.google.drafts[0]["body"]
        )
        checks["draft replies to the sender in the same thread"] = bool(world.google.drafts) and (
            world.google.drafts[0]["to"] == "lena@datapipe.io"
            and world.google.drafts[0]["thread_id"] == "th-flow-lena"
        )
        details["draft_preview"] = (
            world.google.drafts[0]["body"][:300] if world.google.drafts else None
        )
    finish(
        recorder,
        "reply-flow",
        "Client question -> RAG draft -> edit + approve -> Gmail draft",
        checks,
        details,
    )


async def test_flow_newsletter_is_left_alone(world: FlowWorld, recorder: Recorder) -> None:
    email = await store(world.user, parsed(triage_eval.to_email(TRIAGE["nl-01"]), "flow-nl"))
    with measure() as meter:
        await flows.run_new_email(email)
    recorder.time("flows", "New email -> triaged (no action)", meter)
    insight = await insight_of(email)
    approvals = await rows(Approval, Approval.email_id == email.id)
    checks = {
        "triaged as newsletter": bool(insight and insight.category == "newsletter"),
        "newsletter label applied": [gmail_writes.CATEGORY_LABELS["newsletter"]]
        in world.google.labels,
        "no approval created": not approvals,
        "no Gmail draft": not world.google.drafts,
    }
    finish(
        recorder,
        "newsletter-flow",
        "Newsletter -> labelled and left alone",
        checks,
        {"category": insight.category if insight else None},
    )


async def test_flow_prompt_injection_is_blocked(world: FlowWorld, recorder: Recorder) -> None:
    email = await store(world.user, parsed(triage_eval.to_email(TRIAGE["inj-01"]), "flow-inj"))
    with measure() as meter:
        await flows.run_new_email(email)
    recorder.time("flows", "New email -> triaged (no action)", meter)
    insight = await insight_of(email)
    approvals = await rows(Approval, Approval.email_id == email.id)
    checks = {
        "flagged as suspicious": bool(insight and insight.suspicious),
        "suspicious label applied": any(
            gmail_writes.SUSPICIOUS_LABEL in labels for labels in world.google.labels
        ),
        "no approval created": not approvals,
        "nothing written to Gmail or Calendar": not world.google.drafts
        and not world.google.created,
    }
    finish(
        recorder,
        "injection-flow",
        "Prompt-injection email -> flagged, automation blocked",
        checks,
        {"reasons": insight.suspicious_reasons if insight else None},
    )


async def test_flow_trusted_meeting_is_booked_then_undone(
    world: FlowWorld, recorder: Recorder
) -> None:
    """A colleague from a trusted domain proposes a free time -> the event is created
    automatically (within the user's limits) -> the user undoes it from the app."""
    case = SCHEDULING["s07-trusted-request-concrete"]
    email_data, events = scheduling_eval.build_case(case, PROFILE, DAYS)
    world.google.events = events
    email = await store(world.user, parsed(email_data, "flow-s07"))
    with measure() as meter:
        await flows.run_new_email(email)
    recorder.time("flows", "New email -> meeting booked", meter)

    actions = await rows(SchedulingAction, SchedulingAction.email_id == email.id)
    pending = await rows(Approval, Approval.email_id == email.id, Approval.status == "pending")
    created = world.google.created[0] if world.google.created else None
    checks = {
        "event created automatically": bool(actions)
        and actions[0].mode == "auto"
        and created is not None,
        "at the time the sender proposed": created is not None
        and created.start.startswith(iso_at(DAYS, "D2", "11:00", PROFILE["timezone"])[:16]),
        "sender invited": created is not None and "priya@acme.com" in created.attendees,
        "no approval needed": not pending,
    }
    details: dict[str, Any] = {
        "action": actions[0].action if actions else None,
        "event": created.model_dump(include={"summary", "start", "end"}) if created else None,
    }
    if actions:
        response = await world.client.post(f"/api/scheduling-actions/{actions[0].id}/undo")
        undone = await rows(SchedulingAction, SchedulingAction.id == actions[0].id)
        checks["user undo removes the event"] = response.status_code == 200 and bool(
            world.google.deleted
        )
        checks["action marked undone"] = bool(undone) and undone[0].undone
    finish(
        recorder,
        "auto-schedule-flow",
        "Trusted meeting request -> auto-booked -> user undoes it",
        checks,
        details,
    )


async def test_flow_vague_request_gets_proposed_times(world: FlowWorld, recorder: Recorder) -> None:
    """A vague 'let's catch up' -> 3 valid slots proposed in a reply draft -> approved -> Gmail
    draft; nothing is booked because the sender has not picked a time yet."""
    case = SCHEDULING["s09-vague-request"]
    email_data, events = scheduling_eval.build_case(case, PROFILE, DAYS)
    world.google.events = events
    email = await store(world.user, parsed(email_data, "flow-s09"))
    with measure() as meter:
        await flows.run_new_email(email)
    recorder.time("flows", "New email -> approval ready", meter)

    pending = await rows(Approval, Approval.email_id == email.id, Approval.status == "pending")
    payload = pending[0].payload if pending else {}
    slots = [Slot.model_validate(slot) for slot in payload.get("slots", [])]
    prefs = MeetingPreferences.model_validate(PROFILE["meeting_preferences"])
    ctx = rule_context(prefs, PROFILE["timezone"], False)
    violations = scheduling_eval.rule_violations(slots, ctx, events, None)
    variants = payload.get("variants", [])
    checks = {
        "approval waiting with proposed slots": len(pending) == 1 and len(slots) >= 2,
        "every proposed slot follows the hard rules": bool(slots) and not violations,
        "reply variants drafted": bool(variants),
        "nothing booked before the sender picks": not world.google.created,
    }
    details: dict[str, Any] = {"slots": [slot.start for slot in slots], "violations": violations}
    if pending and variants:
        with measure() as meter:
            response = await world.client.post(
                f"/api/approvals/{pending[0].id}/decide",
                json={"action": "approve", "variant_index": 0},
            )
        recorder.time("flows", "Approve -> Gmail draft", meter)
        checks["approved reply saved as Gmail draft"] = (
            response.status_code == 200 and len(world.google.drafts) == 1
        )
    finish(
        recorder,
        "propose-times-flow",
        "Vague meeting request -> 3 valid slots -> approve -> Gmail draft",
        checks,
        details,
    )


async def test_flow_chat_hands_off_a_draft(world: FlowWorld, recorder: Recorder) -> None:
    """In chat the user asks for a reply -> the agent finds the email and hands it to the
    draft flow -> the draft waits in Approvals; chat itself writes nothing."""
    mailbox = Mailbox.build(list(TRIAGE.values()), DAYS).only({"act-01", "fyi-01", "mtg-02"})
    email_cases = await mailbox.seed(world.user.id, embed_sent=False)
    world.patch.setattr(gmail_api, "search_threads", mailbox.search_threads)
    world.patch.setattr(gmail_api, "get_thread", mailbox.get_thread)
    priya_q3 = next(email_id for email_id, case in email_cases.items() if case == "act-01")
    context = chat_service.agent_context(world.user.id, await load_preferences(world.user.id))

    used: list[str] = []
    parts: list[str] = []
    with measure() as meter:
        async for event in run_chat(
            [
                HumanMessage(
                    "Please draft a reply to Priya's email about the Q3 report saying "
                    "I'll send my comments by Thursday."
                )
            ],
            context,
            CitationTracker(),
        ):
            if isinstance(event, ChatStepEvent):
                used.append(event.tool)
            else:
                parts.append(event.text)
    recorder.time("flows", "Chat request -> draft in Approvals", meter)

    pending = await rows(
        Approval, Approval.email_id == uuid.UUID(priya_q3), Approval.status == "pending"
    )
    answer = "".join(parts)
    checks = {
        "agent searched the mail": "search_threads" in used,
        "agent handed off to the draft flow": "start_draft_flow" in used,
        "draft waiting in Approvals for that email": len(pending) == 1,
        "chat wrote nothing to Gmail itself": not world.google.drafts,
        "(info) reply mentions Approvals": "approval" in answer.lower(),
    }
    finish(
        recorder,
        "chat-handoff-flow",
        "Chat: 'draft a reply' -> search -> hand-off -> Approvals",
        checks,
        {"tools": used, "answer": answer[:300]},
    )
