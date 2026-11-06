"""Integration fixtures: a fresh `test_suite` schema, signed-in users and a fake Gmail."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.db import get_engine, open_session
from app.errors import GoogleApiError, NotFound
from app.google import gmail_api
from app.main import app
from app.models import Base, Email, OAuthToken, Preferences, SyncState, User
from app.schemas.google import HistoryChanges, ParsedEmail
from app.security.crypto import encrypt
from app.security.session import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, create_signed_token
from app.services import processing
from tests.conftest import HAS_TEST_DATABASE, TEST_SCHEMA

if not HAS_TEST_DATABASE:
    pytest.skip("TEST_DATABASE_URL is not set", allow_module_level=True)


async def assert_tables_are_isolated() -> None:
    """Stop the whole run unless every table the tests use lives in the test schema.

    Tables are created with explicit schema names (the engine's schema_translate_map), so
    this should always hold; the check makes sure tests can never touch real data.
    """
    async with get_engine().connect() as connection:
        rows = await connection.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"),
            {"schema": TEST_SCHEMA},
        )
        created = {row[0] for row in rows}
    missing = set(Base.metadata.tables) - created
    if missing:
        pytest.exit(f"Refusing to run: tables not isolated in {TEST_SCHEMA}: {sorted(missing)}", 2)


@pytest.fixture(scope="session", autouse=True)
async def database() -> AsyncIterator[None]:
    """Create all tables in a throwaway schema once per test run, drop it at the end."""
    engine = get_engine()
    assert engine.get_execution_options().get("schema_translate_map") == {None: TEST_SCHEMA}
    async with engine.begin() as connection:
        await connection.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
        await connection.execute(text(f"CREATE SCHEMA {TEST_SCHEMA}"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)
    await assert_tables_are_isolated()
    yield
    async with engine.begin() as connection:
        await connection.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_tables() -> AsyncIterator[None]:
    """Every test starts with empty tables. Names are schema-qualified: never `public`."""
    yield
    names = ", ".join(f'{TEST_SCHEMA}."{table.name}"' for table in Base.metadata.sorted_tables)
    async with get_engine().begin() as connection:
        await connection.execute(text(f"TRUNCATE {names} CASCADE"))


async def create_user(
    email: str = "me@example.com", token_expires_in: timedelta = timedelta(hours=1)
) -> User:
    """A signed-up user with tokens, preferences and sync state."""
    async with open_session() as db:
        user = User(email=email, name="Me", onboarded=True)
        db.add(user)
        await db.flush()
        db.add(
            OAuthToken(
                user_id=user.id,
                access_token_enc=encrypt("access-token"),
                refresh_token_enc=encrypt("refresh-token"),
                expires_at=datetime.now(UTC) + token_expires_in,
                scopes=[],
            )
        )
        db.add(Preferences(user_id=user.id))
        db.add(SyncState(user_id=user.id))
        await db.commit()
        return user


@pytest.fixture
async def user() -> User:
    return await create_user()


def client_for(user_id: uuid.UUID | None) -> AsyncClient:
    """HTTP client for the app, signed in as `user_id` (with a valid CSRF pair)."""
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    if user_id:
        client.cookies.set(
            SESSION_COOKIE, create_signed_token({"sub": str(user_id)}, timedelta(hours=1))
        )
        client.cookies.set(CSRF_COOKIE, "csrf-value")
        client.headers[CSRF_HEADER] = "csrf-value"
    return client


@pytest.fixture
async def client(user: User) -> AsyncIterator[AsyncClient]:
    async with client_for(user.id) as signed_in:
        yield signed_in


class FakeGmail:
    """In-memory stand-in for the Gmail read functions used by sync."""

    def __init__(self) -> None:
        self.messages: dict[str, ParsedEmail] = {}
        self.inbox_ids: list[str] = []
        self.history: HistoryChanges | None = None
        self.history_expired = False
        self.profile_history_id = "1000"
        self.failing_ids: set[str] = set()
        self.fetched: list[str] = []
        self.processed: list[str] = []  # emails handed to the triage step

    def add(self, email: ParsedEmail, in_inbox: bool = True) -> None:
        self.messages[email.gmail_id] = email
        if in_inbox:
            self.inbox_ids.append(email.gmail_id)

    async def get_profile_history_id(self, user_id: uuid.UUID) -> str:
        return self.profile_history_id

    async def list_recent_inbox_ids(self, user_id: uuid.UUID, limit: int) -> list[str]:
        return self.inbox_ids[-limit:]

    async def list_history(self, user_id: uuid.UUID, start_history_id: str) -> HistoryChanges:
        if self.history_expired or self.history is None:
            raise gmail_api.HistoryExpired()
        return self.history

    async def get_message(self, user_id: uuid.UUID, message_id: str) -> ParsedEmail:
        self.fetched.append(message_id)
        if message_id in self.failing_ids:
            raise GoogleApiError()
        if message_id not in self.messages:
            raise NotFound()
        return self.messages[message_id]


@pytest.fixture
def fake_gmail(monkeypatch: pytest.MonkeyPatch) -> FakeGmail:
    """Fake Gmail reads. Processing (triage graph) is recorded, not run: sync tests test sync."""
    fake = FakeGmail()
    for name in ("get_profile_history_id", "list_recent_inbox_ids", "list_history", "get_message"):
        monkeypatch.setattr(gmail_api, name, getattr(fake, name))

    async def record_processing(email: Email) -> bool:
        fake.processed.append(email.gmail_id)
        return True

    monkeypatch.setattr(processing, "process_email", record_processing)
    return fake
