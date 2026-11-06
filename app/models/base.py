"""Declarative base and the shared columns every table has."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base class for all tables. Python types map to Postgres types here."""

    type_annotation_map = {  # noqa: RUF012 (SQLAlchemy reads this class attribute)
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        list[str]: ARRAY(Text),
    }


class IdAndTimestamps:
    """`id`, `created_at` and `updated_at` columns shared by every table."""

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    created_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        default=utc_now, onupdate=utc_now, server_default=func.now()
    )


class OwnedByUser:
    """`user_id` column for tables that belong to one user (rows deleted with the user)."""

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
