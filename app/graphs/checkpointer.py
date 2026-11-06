"""LangGraph checkpointer on Postgres (Supabase SESSION pooler: prepared statements work there)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings


@asynccontextmanager
async def open_checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    """Open a small connection pool and create LangGraph's tables if needed (`setup()`)."""
    pool: AsyncConnectionPool = AsyncConnectionPool(
        conninfo=settings.checkpoint_database_url,
        min_size=1,
        max_size=5,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await saver.setup()
        yield saver
    finally:
        await pool.close()
