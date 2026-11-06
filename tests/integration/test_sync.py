"""sync_user against a real database with a fake Gmail."""

from sqlalchemy import func, select, text

from app.db import get_engine, open_session
from app.models import Attachment, Email, SyncState, User
from app.schemas.google import AttachmentInfo, HistoryChanges
from app.services.sync import sync_user
from tests.factories import parsed_email
from tests.integration.conftest import FakeGmail


async def stored_gmail_ids() -> list[str]:
    async with open_session() as db:
        return list(await db.scalars(select(Email.gmail_id).order_by(Email.received_at)))


async def history_id_of(user: User) -> str | None:
    async with open_session() as db:
        return await db.scalar(select(SyncState.history_id).where(SyncState.user_id == user.id))


async def test_first_sync_imports_newest_inbox_mail(user: User, fake_gmail: FakeGmail) -> None:
    attachment = AttachmentInfo(filename="invoice.pdf", mime_type="application/pdf", size_bytes=10)
    fake_gmail.add(parsed_email("a", minutes=1, attachments=[attachment]))
    fake_gmail.add(parsed_email("b", minutes=2))

    result = await sync_user(user.id)

    assert result.status == "synced" and result.processed == 2 and result.remaining == 0
    assert await stored_gmail_ids() == ["a", "b"]
    assert await history_id_of(user) == "1000"  # read from the profile before listing
    assert fake_gmail.processed == ["a", "b"]  # each new email is handed to triage once
    async with open_session() as db:
        assert await db.scalar(select(func.count()).select_from(Attachment)) == 1


async def test_history_sync_skips_already_stored_messages(
    user: User, fake_gmail: FakeGmail
) -> None:
    fake_gmail.add(parsed_email("a"))
    await sync_user(user.id)
    fake_gmail.add(parsed_email("b", minutes=5))
    fake_gmail.history = HistoryChanges(added_message_ids=["a", "b"], latest_history_id="1005")
    fake_gmail.fetched.clear()

    result = await sync_user(user.id)

    assert result.processed == 1
    assert fake_gmail.fetched == ["b"]  # "a" was not fetched again
    assert await history_id_of(user) == "1005"


async def test_cap_leaves_history_id_until_everything_is_stored(
    user: User, fake_gmail: FakeGmail
) -> None:
    fake_gmail.add(parsed_email("seed"))
    await sync_user(user.id)
    ids = [f"m{number:02d}" for number in range(12)]  # MAX_EMAILS_PER_RUN is 10 in tests
    for number, gmail_id in enumerate(ids):
        fake_gmail.add(parsed_email(gmail_id, minutes=number + 1))
    fake_gmail.history = HistoryChanges(added_message_ids=ids, latest_history_id="2000")

    first = await sync_user(user.id)
    assert (first.processed, first.remaining) == (10, 2)
    assert await history_id_of(user) == "1000"  # not advanced yet

    second = await sync_user(user.id)
    assert (second.processed, second.remaining) == (2, 0)
    assert await history_id_of(user) == "2000"


async def test_failed_message_is_retried_next_time(user: User, fake_gmail: FakeGmail) -> None:
    fake_gmail.add(parsed_email("seed"))
    await sync_user(user.id)
    fake_gmail.add(parsed_email("flaky", minutes=1))
    fake_gmail.failing_ids.add("flaky")
    fake_gmail.history = HistoryChanges(added_message_ids=["flaky"], latest_history_id="3000")

    result = await sync_user(user.id)
    assert result.remaining == 1
    assert await history_id_of(user) == "1000"

    fake_gmail.failing_ids.clear()
    await sync_user(user.id)
    assert "flaky" in await stored_gmail_ids()
    assert await history_id_of(user) == "3000"


async def test_deleted_and_irrelevant_messages_are_skipped(
    user: User, fake_gmail: FakeGmail
) -> None:
    fake_gmail.add(parsed_email("seed"))
    await sync_user(user.id)
    fake_gmail.add(parsed_email("spam", labels=["SPAM"]), in_inbox=False)
    fake_gmail.history = HistoryChanges(
        added_message_ids=["gone", "spam"], latest_history_id="4000"
    )

    result = await sync_user(user.id)
    assert result.remaining == 0
    assert await stored_gmail_ids() == ["seed"]
    assert await history_id_of(user) == "4000"


async def test_expired_history_falls_back_to_recent_inbox(
    user: User, fake_gmail: FakeGmail
) -> None:
    async with open_session() as db:
        state = await db.scalar(select(SyncState).where(SyncState.user_id == user.id))
        assert state is not None
        state.history_id = "1"
        await db.commit()
    fake_gmail.history_expired = True
    fake_gmail.add(parsed_email("a"))

    result = await sync_user(user.id)
    assert result.processed == 1
    assert await history_id_of(user) == "1000"


async def test_read_state_follows_gmail(user: User, fake_gmail: FakeGmail) -> None:
    fake_gmail.add(parsed_email("a"))
    await sync_user(user.id)
    fake_gmail.history = HistoryChanges(read_message_ids=["a"], latest_history_id="1001")

    await sync_user(user.id)
    async with open_session() as db:
        assert await db.scalar(select(Email.is_unread).where(Email.gmail_id == "a")) is False


async def test_concurrent_sync_reports_already_syncing(user: User, fake_gmail: FakeGmail) -> None:
    async with get_engine().connect() as connection, connection.begin():
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": str(user.id)}
        )
        result = await sync_user(user.id)
    assert result.status == "already_syncing"
    assert fake_gmail.fetched == []
