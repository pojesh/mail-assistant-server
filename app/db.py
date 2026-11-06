"""Database engine and sessions (SQLAlchemy async + asyncpg on Supabase's transaction pooler)."""

from collections.abc import AsyncIterator
from functools import lru_cache
from uuid import uuid4

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


def _unique_statement_name() -> str:
    """Give every prepared statement a unique name.

    Supabase's transaction pooler may hand each transaction a different server connection,
    so reusing named prepared statements across transactions would fail.
    """
    return f"__asyncpg_{uuid4()}__"


@lru_cache
def get_engine() -> AsyncEngine:
    """Create the shared async engine on first use (importing the app never connects)."""
    connect_args: dict[str, object] = {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": _unique_statement_name,
    }
    execution_options: dict[str, object] = {}
    if settings.database_schema:  # tests only
        # Every table name is rewritten to "<schema>.<table>", so a query can never fall back
        # to the real tables in `public`. `public` stays on the search path only so that
        # extension types such as `vector` resolve.
        execution_options["schema_translate_map"] = {None: settings.database_schema}
        connect_args["server_settings"] = {"search_path": f"{settings.database_schema}, public"}
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args=connect_args,
        execution_options=execution_options,
    )


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the shared engine."""
    # NOTE: expire_on_commit=False so objects stay readable after commit
    # (async sessions cannot lazy-load expired attributes).
    return async_sessionmaker(get_engine(), expire_on_commit=False)


def open_session() -> AsyncSession:
    """Open a session for service code: `async with open_session() as session: ...`."""
    return get_session_factory()()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields one session per request."""
    async with open_session() as session:
        yield session
