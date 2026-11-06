"""/api/me, /api/emails, /api/emails/{id} and /api/sync over HTTP."""

import uuid

from httpx import AsyncClient

from app.db import open_session
from app.models import EmailInsight, User
from app.schemas.google import AttachmentInfo
from app.services.emails import save_email
from tests.factories import parsed_email
from tests.integration.conftest import FakeGmail, client_for, create_user


async def store(user_id: uuid.UUID, gmail_id: str, minutes: int = 0, **kwargs: object) -> uuid.UUID:
    async with open_session() as db:
        email = await save_email(db, user_id, parsed_email(gmail_id, minutes=minutes, **kwargs))
        assert email is not None
        return email.id


async def add_insight(
    user_id: uuid.UUID, email_id: uuid.UUID, category: str, priority: int
) -> None:
    async with open_session() as db:
        db.add(
            EmailInsight(
                user_id=user_id,
                email_id=email_id,
                category=category,
                priority=priority,
                sentiment="neutral",
                sentiment_score=0.5,
                summary="Summary",
                model="test-model",
                provider="google",
            )
        )
        await db.commit()


async def test_me(client: AsyncClient, user: User) -> None:
    response = await client.get("/api/me")
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "me@example.com" and body["status"] == "active"
    assert body["llm_provider"] == "google"


async def test_list_newest_first_with_pagination(client: AsyncClient, user: User) -> None:
    for number in range(5):
        await store(user.id, f"m{number}", minutes=number)
    await store(user.id, "sent", minutes=10, labels=["SENT"])  # sent mail is not in the inbox

    first = (await client.get("/api/emails?limit=3")).json()
    assert [item["subject"] for item in first["items"]] == ["Hello"] * 3
    assert first["next_cursor"]
    second = (await client.get(f"/api/emails?limit=3&cursor={first['next_cursor']}")).json()
    assert len(second["items"]) == 2 and second["next_cursor"] is None
    all_ids = {item["id"] for item in first["items"] + second["items"]}
    assert len(all_ids) == 5


async def test_filters_by_insight_and_text(client: AsyncClient, user: User) -> None:
    invoice_id = await store(user.id, "a", subject="Invoice 50% due")
    other_id = await store(user.id, "b", subject="Lunch?")
    await add_insight(user.id, invoice_id, "action_required", 4)
    await add_insight(user.id, other_id, "fyi", 2)

    by_category = (await client.get("/api/emails?category=action_required")).json()["items"]
    assert [item["id"] for item in by_category] == [str(invoice_id)]
    by_priority = (await client.get("/api/emails?priority=3")).json()["items"]
    assert [item["id"] for item in by_priority] == [str(invoice_id)]
    by_text = (await client.get("/api/emails?q=50%25")).json()["items"]  # "%" is literal
    assert [item["id"] for item in by_text] == [str(invoice_id)]
    assert by_category[0]["insight"]["category"] == "action_required"


async def test_invalid_filter_value_is_422(client: AsyncClient) -> None:
    response = await client.get("/api/emails?category=nonsense")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_detail_includes_thread_and_attachments(
    client: AsyncClient, user: User, fake_gmail: FakeGmail
) -> None:
    attachment = AttachmentInfo(filename="deck.pdf", mime_type="application/pdf")
    first_id = await store(user.id, "a", thread_id="t1", attachments=[attachment])
    await store(user.id, "b", minutes=5, thread_id="t1")

    response = await client.get(f"/api/emails/{first_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["attachments"][0]["filename"] == "deck.pdf"
    assert [message["snippet"] for message in body["thread"]] == ["snippet b"]
    assert body["insight"] is None
    assert body["body_html"] is None  # fake Gmail has no such message: not an error


async def test_other_users_email_is_not_found(user: User) -> None:
    other = await create_user("someone@example.com")
    email_id = await store(other.id, "secret")
    async with client_for(user.id) as client:
        response = await client.get(f"/api/emails/{email_id}")
    assert response.status_code == 404


async def test_sync_now(client: AsyncClient, fake_gmail: FakeGmail) -> None:
    fake_gmail.add(parsed_email("a"))
    response = await client.post("/api/sync")
    assert response.status_code == 200
    assert response.json() == {"status": "synced", "processed": 1, "remaining": 0}


async def test_logout_clears_cookies(client: AsyncClient) -> None:
    response = await client.post("/api/auth/logout")
    assert response.status_code == 200
    assert 'session=""' in response.headers.get("set-cookie", "")
