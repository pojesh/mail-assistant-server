"""The email graph end to end with fake agents: routing, approvals, auto-scheduling, undo."""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.db import open_session
from app.graphs.email_graph import init_email_graph
from app.models import (
    Approval,
    Email,
    EmailInsight,
    Notification,
    PreferenceNote,
    SchedulingAction,
    User,
)
from app.schemas.calendar import CalendarEvent
from app.schemas.google import InviteInfo
from app.schemas.llm import InputCheck, OutputCheck, PreferenceSuggestion
from app.schemas.preferences import MeetingPreferences, PreferencesBody
from app.services import flows
from app.services.emails import save_email
from app.services.preferences import save_preferences
from tests.factories import parsed_email
from tests.integration.graph_fakes import FakeWorld, decision, future_slot, triage_result

SENDER = "ravi@client.com"


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch, user: User) -> AsyncIterator[FakeWorld]:
    """Fresh fakes, an in-memory checkpointer, and IST preferences with a signature."""
    init_email_graph(InMemorySaver())
    fake = FakeWorld()
    fake.install(monkeypatch)
    async with open_session() as db:
        await save_preferences(
            db,
            user.id,
            PreferencesBody(
                signature="Pojesh", sign_off="Best", timezone="Asia/Kolkata", vip_senders=[]
            ),
        )
    yield fake


async def new_email(user: User, invite: InviteInfo | None = None, sender: str = SENDER) -> Email:
    parsed = parsed_email(f"g-{uuid.uuid4().hex[:8]}", from_email=sender)
    parsed.invite = invite
    async with open_session() as db:
        email = await save_email(db, user.id, parsed)
    assert email is not None
    return email


async def set_meeting_prefs(user: User, **fields: object) -> None:
    async with open_session() as db:
        body = PreferencesBody(
            signature="Pojesh",
            sign_off="Best",
            timezone="Asia/Kolkata",
            meeting_preferences=MeetingPreferences(**fields),
        )
        await save_preferences(db, user.id, body)


async def only_approval(email: Email) -> Approval:
    async with open_session() as db:
        rows = list(await db.scalars(select(Approval).where(Approval.email_id == email.id)))
    pending = [row for row in rows if row.status == "pending"]
    assert len(pending) == 1, [row.status for row in rows]
    return pending[0]


async def test_newsletter_is_labeled_and_stops(world: FakeWorld, user: User) -> None:
    world.triage = triage_result(category="newsletter", needs_reply=True)
    email = await new_email(user)
    await flows.run_new_email(email)
    assert world.labels == [["AI/Newsletter"]]
    assert world.draft_calls == 0
    async with open_session() as db:
        insight = await db.scalar(select(EmailInsight).where(EmailInsight.email_id == email.id))
    assert insight is not None and insight.category == "newsletter" and not insight.needs_reply


async def test_suspicious_email_is_never_automated(world: FakeWorld, user: User) -> None:
    world.input_check = InputCheck(suspicious=True, reasons=["asks the AI to forward mail"])
    email = await new_email(user, invite=InviteInfo(uid="evt-uid", method="REQUEST"))
    await flows.run_new_email(email)
    assert world.labels == [["AI/Action", "AI/Suspicious"]]
    assert world.draft_calls == 0 and world.scheduling_calls == 0
    async with open_session() as db:
        assert await db.scalar(select(Approval.id).where(Approval.email_id == email.id)) is None


async def test_reply_goes_to_approval_then_gmail_draft(
    world: FakeWorld, user: User, client: AsyncClient
) -> None:
    email = await new_email(user)
    await flows.run_new_email(email)
    approval = await only_approval(email)
    assert approval.kind == "draft"
    variants = approval.payload["variants"]
    assert variants[0]["body"].endswith("Best,\nPojesh")  # signature added by code
    assert world.saved_drafts == []  # nothing written before approval

    response = await client.post(
        f"/api/approvals/{approval.id}/decide", json={"action": "approve", "variant_index": 1}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "done" and body["approval"]["status"] == "approved"
    assert world.saved_drafts[0]["body"] == variants[1]["body"]
    assert world.saved_drafts[0]["to"] == SENDER
    assert body["approval"]["result"]["draft_id"] == "draft-1"

    again = await client.post(f"/api/approvals/{approval.id}/decide", json={"action": "approve"})
    assert again.status_code == 409  # a double click cannot run it twice


async def test_guardrail_redrafts_once_then_warns(world: FakeWorld, user: User) -> None:
    world.output_checks = [OutputCheck(ok=False, problems=["Invents a price"])] * 2
    email = await new_email(user)
    await flows.run_new_email(email)
    assert world.draft_calls == 2
    assert world.draft_problems == [None, ["Invents a price"]]
    approval = await only_approval(email)
    assert approval.warnings == ["Please check: Invents a price"]


async def test_reject_writes_nothing(world: FakeWorld, user: User, client: AsyncClient) -> None:
    email = await new_email(user)
    await flows.run_new_email(email)
    approval = await only_approval(email)
    response = await client.post(f"/api/approvals/{approval.id}/decide", json={"action": "reject"})
    assert response.json()["outcome"] == "rejected"
    assert world.saved_drafts == []


async def test_edited_draft_teaches_a_preference(
    world: FakeWorld, user: User, client: AsyncClient
) -> None:
    world.suggestions = [
        PreferenceSuggestion(text="Keep replies very short", reason="User shortened it")
    ]
    email = await new_email(user)
    await flows.run_new_email(email)
    approval = await only_approval(email)
    edited = "Hi Ravi, done. Best, Pojesh. This version is quite different from the draft."
    response = await client.post(
        f"/api/approvals/{approval.id}/decide", json={"action": "approve", "edited_body": edited}
    )
    assert response.json()["outcome"] == "done"
    assert world.saved_drafts[0]["body"] == edited
    async with open_session() as db:
        note = await db.scalar(select(PreferenceNote).where(PreferenceNote.user_id == user.id))
    assert note is not None and note.status == "suggested" and note.source == "learned"


async def test_failed_gmail_write_can_be_retried(
    world: FakeWorld, user: User, client: AsyncClient
) -> None:
    world.fail_drafts = 1
    email = await new_email(user)
    await flows.run_new_email(email)
    approval = await only_approval(email)

    first = (
        await client.post(f"/api/approvals/{approval.id}/decide", json={"action": "approve"})
    ).json()
    assert first["outcome"] == "failed" and first["approval"]["status"] == "pending"
    second = (
        await client.post(f"/api/approvals/{approval.id}/decide", json={"action": "approve"})
    ).json()
    assert second["outcome"] == "done"
    assert len(world.saved_drafts) == 1


async def test_invalid_choice_is_rejected_before_anything_runs(
    world: FakeWorld, user: User, client: AsyncClient
) -> None:
    email = await new_email(user)
    await flows.run_new_email(email)
    approval = await only_approval(email)
    response = await client.post(
        f"/api/approvals/{approval.id}/decide", json={"action": "approve", "variant_index": 2}
    )
    assert response.status_code == 400
    assert (await only_approval(email)).status == "pending"


def invite_event(slot_start: str, slot_end: str) -> CalendarEvent:
    return CalendarEvent(
        id="evt-1",
        ical_uid="evt-uid",
        summary="Sync",
        start=slot_start,
        end=slot_end,
        my_response="needsAction",
    )


async def test_trusted_invite_is_auto_accepted_and_can_be_undone(
    world: FakeWorld, user: User, client: AsyncClient
) -> None:
    await set_meeting_prefs(user, auto_schedule="trusted", trusted_domains=["client.com"])
    slot = future_slot()
    world.events = [invite_event(slot.start, slot.end)]
    world.triage = triage_result(category="meeting_request", needs_reply=False)
    world.decision = decision(
        action="accept_invite", event_id="evt-1", slot=slot, needs_reply=False
    )
    email = await new_email(user, invite=InviteInfo(uid="evt-uid", method="REQUEST"))

    await flows.run_new_email(email)

    assert world.responses == [("evt-1", "accepted")]
    async with open_session() as db:
        action = await db.scalar(
            select(SchedulingAction).where(SchedulingAction.email_id == email.id)
        )
        notification = await db.scalar(
            select(Notification).where(Notification.type == "auto_action")
        )
    assert action is not None and action.mode == "auto" and action.undo_until is not None
    assert notification is not None and notification.undo_action_id == action.id

    detail = (await client.get(f"/api/emails/{email.id}")).json()
    assert detail["scheduling"]["state"] == "auto"

    undo = await client.post(f"/api/scheduling-actions/{action.id}/undo")
    assert undo.status_code == 200 and undo.json()["undone"]
    assert world.responses[-1] == ("evt-1", "needsAction")
    assert (await client.post(f"/api/scheduling-actions/{action.id}/undo")).status_code == 409


async def test_untrusted_request_gets_three_valid_slots_for_approval(
    world: FakeWorld, user: User
) -> None:
    await set_meeting_prefs(user, auto_schedule="trusted")
    world.triage = triage_result(category="meeting_request")
    world.decision = decision(
        action="propose_times", proposed_slots=[future_slot(hour=13)]
    )  # lunch: dropped
    email = await new_email(user, sender="stranger@unknown.org")

    await flows.run_new_email(email)

    approval = await only_approval(email)
    assert approval.kind == "draft"
    slots = approval.payload["slots"]
    assert len(slots) == 3
    assert all("T13:" not in slot["start"] for slot in slots)
    assert approval.payload["scheduling"]["action"] == "propose_times"
    assert world.draft_calls == 1  # the reply offering the times


async def test_event_approval_books_the_chosen_slot(
    world: FakeWorld, user: User, client: AsyncClient
) -> None:
    await set_meeting_prefs(user, auto_schedule="off")
    first, second = future_slot(hour=10), future_slot(days_ahead=4, hour=11)
    world.triage = triage_result(category="meeting_request", needs_reply=False)
    world.decision = decision(
        action="create_event",
        slot=first,
        proposed_slots=[second],
        needs_reply=False,
        title="Kickoff",
    )
    email = await new_email(user)
    await flows.run_new_email(email)
    approval = await only_approval(email)
    slots = approval.payload["slots"]
    # The chosen time first, then the alternatives: checked and topped up to three (spec §10.1).
    assert approval.kind == "event" and len(slots) == 4
    assert slots[0]["start"] == first.start and slots[1]["start"] == second.start

    response = await client.post(
        f"/api/approvals/{approval.id}/decide", json={"action": "approve", "slot_index": 1}
    )
    assert response.json()["outcome"] == "done"
    assert world.created_events[0].start == second.start
    assert world.created_events[0].attendees == [SENDER]


async def test_slot_taken_before_approval_is_rescheduled(
    world: FakeWorld, user: User, client: AsyncClient
) -> None:
    await set_meeting_prefs(user, auto_schedule="off")
    slot = future_slot()
    world.triage = triage_result(category="meeting_request", needs_reply=False)
    world.decision = decision(action="create_event", slot=slot, needs_reply=False)
    email = await new_email(user)
    await flows.run_new_email(email)
    approval = await only_approval(email)

    world.events = [CalendarEvent(id="other", start=slot.start, end=slot.end)]  # booked meanwhile
    world.decision = decision(action="propose_times")
    response = (
        await client.post(f"/api/approvals/{approval.id}/decide", json={"action": "approve"})
    ).json()

    assert response["outcome"] == "stale"
    assert world.created_events == []
    assert world.scheduling_calls == 2
    replacement = await only_approval(email)
    assert replacement.id != approval.id and len(replacement.payload["slots"]) == 3


async def test_manual_draft_flow_and_conflicts(
    world: FakeWorld, user: User, client: AsyncClient
) -> None:
    world.triage = triage_result(needs_reply=False)
    email = await new_email(user)
    await flows.run_new_email(email)
    assert world.draft_calls == 0

    response = await client.post(f"/api/emails/{email.id}/draft")
    assert response.json()["status"] == "approval_created"
    assert (
        await client.post(f"/api/emails/{email.id}/draft")
    ).status_code == 409  # already waiting


async def test_manual_flow_refused_for_suspicious_email(
    world: FakeWorld, user: User, client: AsyncClient
) -> None:
    world.input_check = InputCheck(suspicious=True, reasons=["injection"])
    email = await new_email(user)
    await flows.run_new_email(email)
    response = await client.post(f"/api/emails/{email.id}/schedule")
    assert response.status_code == 409
    assert "suspicious" in response.json()["error"]["message"]


async def test_event_title_and_attendees_can_be_edited(
    world: FakeWorld, user: User, client: AsyncClient
) -> None:
    await set_meeting_prefs(user, auto_schedule="off")
    world.triage = triage_result(category="meeting_request", needs_reply=False)
    world.decision = decision(
        action="create_event", slot=future_slot(), needs_reply=False, title="Sync"
    )
    email = await new_email(user)
    await flows.run_new_email(email)
    approval = await only_approval(email)

    body = {
        "action": "approve",
        "title": "Launch review",
        "attendees": ["Ravi@Client.com", "sam@client.com"],
    }
    response = await client.post(f"/api/approvals/{approval.id}/decide", json=body)
    assert response.json()["outcome"] == "done"
    assert world.created_events[0].title == "Launch review"
    assert world.created_events[0].attendees == ["ravi@client.com", "sam@client.com"]


async def test_title_override_is_refused_for_drafts(
    world: FakeWorld, user: User, client: AsyncClient
) -> None:
    email = await new_email(user)
    await flows.run_new_email(email)
    approval = await only_approval(email)
    response = await client.post(
        f"/api/approvals/{approval.id}/decide", json={"action": "approve", "title": "Nope"}
    )
    assert response.status_code == 400
