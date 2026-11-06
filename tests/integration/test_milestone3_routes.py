"""Chat SSE, attachments, internal routes, activity, notifications, dashboard, digest, account."""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select, text

from app.agents import chat as chat_agent
from app.config import settings
from app.db import get_engine, open_session
from app.google import calendar_api
from app.main import app
from app.models import (
    ActivityLog,
    Approval,
    Email,
    EmailInsight,
    Notification,
    SchedulingAction,
    User,
)
from app.schemas.calendar import CalendarEvent
from app.schemas.digest import DigestText
from app.schemas.google import AttachmentInfo
from app.security import oidc
from app.services import account, digest
from app.services.emails import save_email
from tests.factories import parsed_email
from tests.integration.conftest import FakeGmail
from tests.unit.test_milestone3_units import ToolCallingFake

SA = "internal-invoker@project.iam.gserviceaccount.com"


async def store(user: User, gmail_id: str, **kwargs: Any) -> Email:
    async with open_session() as db:
        email = await save_email(db, user.id, parsed_email(gmail_id, **kwargs))
    assert email is not None
    return email


def sse_events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")
    ]


async def test_chat_streams_and_saves_the_conversation(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    model = ToolCallingFake(responses=[AIMessage("You have no meetings tomorrow.")])
    monkeypatch.setattr(chat_agent, "get_chat_model", lambda tier: model)

    response = await client.post("/api/chat", json={"message": "Any meetings tomorrow?"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    assert [event["type"] for event in events] == ["token", "done"]
    done = events[-1]
    assert done["content"] == "You have no meetings tomorrow."

    sessions = (await client.get("/api/chat/sessions")).json()
    assert sessions[0]["title"] == "Any meetings tomorrow?" and sessions[0]["message_count"] == 2
    detail = (await client.get(f"/api/chat/sessions/{done['session_id']}")).json()
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]


async def test_chat_reports_model_failure_as_an_error_event(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    class Broken(ToolCallingFake):
        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            raise ValueError("invalid argument")  # not retryable

    monkeypatch.setattr(chat_agent, "get_chat_model", lambda tier: Broken(responses=[]))
    events = sse_events((await client.post("/api/chat", json={"message": "hi"})).text)
    assert events == [{"type": "error", "code": "llm_unavailable", "message": events[0]["message"]}]


async def test_chat_is_rate_limited_per_user(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    monkeypatch.setattr(
        chat_agent, "get_chat_model", lambda tier: ToolCallingFake(responses=[AIMessage("ok")] * 30)
    )
    statuses = [
        (await client.post("/api/chat", json={"message": "hi"})).status_code for _ in range(21)
    ]
    assert statuses[:20] == [200] * 20 and statuses[20] == 429


async def test_attachment_search(client: AsyncClient, user: User) -> None:
    await store(
        user,
        "a",
        subject="October invoice",
        attachments=[AttachmentInfo(filename="Invoice_Oct.pdf", mime_type="application/pdf")],
    )
    await store(
        user,
        "b",
        subject="Deck",
        attachments=[
            AttachmentInfo(filename="pitch-deck.pptx", mime_type="application/vnd.ms-powerpoint")
        ],
    )
    names = lambda items: [item["filename"] for item in items]  # noqa: E731
    assert names((await client.get("/api/attachments?q=invoice")).json()) == ["Invoice_Oct.pdf"]
    assert names((await client.get("/api/attachments?q=invoce")).json()) == [
        "Invoice_Oct.pdf"
    ]  # typo: trigram
    by_sender = names((await client.get("/api/attachments?q=ravi")).json())  # sender name match
    assert sorted(by_sender) == ["Invoice_Oct.pdf", "pitch-deck.pptx"]
    assert len((await client.get("/api/attachments")).json()) == 2


def internal_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.setattr(settings, "internal_audience", "https://api.example.run.app")
    monkeypatch.setattr(settings, "internal_invoker_sa", SA)
    monkeypatch.setattr(
        oidc,
        "_verify",
        lambda token: {"email": SA, "email_verified": True} if token == "good" else {},
    )
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer good"},
    )


def push_body(email: str) -> dict[str, Any]:
    data = base64.b64encode(json.dumps({"emailAddress": email, "historyId": 1}).encode()).decode()
    return {
        "message": {"data": data, "messageId": "1"},
        "subscription": "projects/p/subscriptions/s",
    }


async def test_internal_routes_require_oidc(monkeypatch: pytest.MonkeyPatch) -> None:
    internal_client(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        assert (await anonymous.post("/internal/sync-sweep")).status_code == 401
        assert (
            await anonymous.post("/internal/sync-sweep", headers={"Authorization": "Bearer bad"})
        ).status_code == 401


async def test_gmail_push(
    monkeypatch: pytest.MonkeyPatch, user: User, fake_gmail: FakeGmail
) -> None:
    fake_gmail.add(parsed_email("pushed"))
    async with internal_client(monkeypatch) as client:
        unknown = await client.post("/internal/gmail-push", json=push_body("nobody@example.com"))
        assert unknown.status_code == 200 and unknown.json()["status"] == "ignored"
        synced = await client.post("/internal/gmail-push", json=push_body(user.email.upper()))
        assert synced.json()["status"] == "synced" and fake_gmail.processed == ["pushed"]

        async with get_engine().connect() as lock, lock.begin():
            await lock.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": str(user.id)}
            )
            busy = await client.post("/internal/gmail-push", json=push_body(user.email))
        assert busy.status_code == 503  # Pub/Sub will redeliver later
        bad = await client.post("/internal/gmail-push", json={"message": {"data": "!!"}})
        assert bad.status_code == 400


async def test_sweep_syncs_idle_users_and_builds_due_digests(
    monkeypatch: pytest.MonkeyPatch, user: User, fake_gmail: FakeGmail
) -> None:
    fake_gmail.add(parsed_email("swept"))
    monkeypatch.setattr(
        digest, "is_digest_due", lambda timezone, has_today, now=None: not has_today
    )

    async def fake_write(facts: dict[str, Any], timezone: str, today: str) -> DigestText:
        return DigestText(
            headline="Quiet day", summary=f"{len(facts['needs_attention'])} emails need you."
        )

    async def no_events(user_id: uuid.UUID, start: datetime, end: datetime) -> list[CalendarEvent]:
        return []

    monkeypatch.setattr(digest, "write_digest", fake_write)
    monkeypatch.setattr(calendar_api, "list_events", no_events)
    async with internal_client(monkeypatch) as client:
        result = (await client.post("/internal/sync-sweep")).json()
        assert result["synced"] == 1 and result["digests"] == 1
        again = (await client.post("/internal/sync-sweep")).json()
        assert again["synced"] == 0 and again["digests"] == 0  # recently synced, digest exists


async def test_daily_maintenance_expires_and_applies_retention(
    monkeypatch: pytest.MonkeyPatch, user: User
) -> None:
    old = await store(user, "old", minutes=0)
    async with open_session() as db:
        email = await db.get(Email, old.id)
        assert email is not None
        email.received_at = datetime.now(UTC) - timedelta(days=40)
        db.add(
            Approval(
                user_id=user.id,
                email_id=old.id,
                kind="draft",
                thread_id="t",
                created_at=datetime.now(UTC) - timedelta(days=8),
            )
        )
        await db.commit()
    async with internal_client(monkeypatch) as client:
        result = (await client.post("/internal/daily-maintenance")).json()
    assert result["approvals_expired"] == 1 and result["bodies_cleared"] == 1
    async with open_session() as db:
        assert await db.scalar(select(Email.body_text).where(Email.id == old.id)) is None


async def test_activity_notifications_dashboard(client: AsyncClient, user: User) -> None:
    email = await store(user, "a")
    async with open_session() as db:
        db.add(
            EmailInsight(
                user_id=user.id,
                email_id=email.id,
                category="action_required",
                priority=4,
                sentiment="urgent",
                sentiment_score=0.9,
                summary="Pay now",
                model="m",
                provider="google",
            )
        )
        action = SchedulingAction(
            user_id=user.id,
            action="create_event",
            mode="auto",
            calendar_event_id="e1",
            undo_until=datetime.now(UTC) + timedelta(hours=2),
        )
        db.add(action)
        await db.flush()
        db.add(
            ActivityLog(
                user_id=user.id,
                actor="executor",
                action="create_event",
                status="auto",
                detail={"scheduling_action_id": str(action.id)},
            )
        )
        db.add(
            Notification(
                user_id=user.id, type="auto_action", title="Added meeting", undo_action_id=action.id
            )
        )
        await db.commit()
    async with open_session() as db:  # make the email recent, so it is inside the 7-day window
        stored = await db.get(Email, email.id)
        assert stored is not None
        stored.received_at = datetime.now(UTC)
        await db.commit()

    activity = (await client.get("/api/activity")).json()
    assert activity["items"][0]["undo_action_id"] == str(action.id)
    bell = (await client.get("/api/notifications")).json()
    assert bell["unread_count"] == 1 and bell["items"][0]["can_undo"]
    assert (await client.post("/api/notifications/read", json={})).status_code == 200
    assert (await client.get("/api/notifications")).json()["unread_count"] == 0

    board = (await client.get("/api/dashboard")).json()
    assert board["by_category"] == {"action_required": 1} and board["by_sentiment"] == {"urgent": 1}
    assert board["priority_queue"][0]["summary"] == "Pay now" and board["auto_actions_today"] == 1
    assert (await client.get("/api/digest/today")).json() is None


async def test_disconnect_and_delete_account(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient, user: User
) -> None:
    revoked: list[str] = []

    async def fake_revoke(token: str) -> None:
        revoked.append(token)

    monkeypatch.setattr(account, "revoke_token", fake_revoke)
    await store(user, "a")
    assert (await client.post("/api/account/disconnect")).status_code == 200
    assert revoked == ["refresh-token"]
    assert (await client.get("/api/me")).json()["status"] == "needs_reconnect"

    assert (
        await client.request("DELETE", "/api/account", json={"confirm": "nope"})
    ).status_code == 422
    response = await client.request("DELETE", "/api/account", json={"confirm": "delete"})
    assert response.status_code == 200
    async with open_session() as db:
        assert (
            await db.scalar(select(func.count()).select_from(User).where(User.id == user.id)) == 0
        )
        assert await db.scalar(select(func.count()).select_from(Email)) == 0
    assert (await client.get("/api/me")).status_code == 401  # signed out
