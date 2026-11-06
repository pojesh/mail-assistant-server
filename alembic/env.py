"""Alembic migration runner.

- Connects with CHECKPOINT_DATABASE_URL (Supabase session pooler) using psycopg.
- Takes a Postgres advisory lock first, so several Cloud Run instances starting at once
  run migrations one after another instead of racing.
- Only manages tables defined in app.models (LangGraph creates its own checkpoint tables).
"""

from typing import Any

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.config import settings
from app.models import Base

MIGRATION_LOCK_ID = 727_001  # any constant number shared by all instances

target_metadata = Base.metadata


def include_name(name: str | None, type_: str, parent_names: dict[str, Any]) -> bool:
    """Ignore tables that are not ours (e.g. LangGraph's checkpoint tables)."""
    if type_ == "table":
        return name in target_metadata.tables
    return True


def run_migrations() -> None:
    """Run migrations while holding the advisory lock."""
    url = context.config.attributes.get("database_url") or settings.sync_checkpoint_url
    engine = create_engine(url, poolclass=pool.NullPool)

    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        connection.commit()
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_name=include_name,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
            connection.commit()


run_migrations()
