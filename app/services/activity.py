"""The activity log: one row per agent or graph step (who, what, tool, outcome)."""

import uuid
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError

from app.db import open_session
from app.logging import get_logger
from app.models import ActivityLog

log = get_logger(__name__)

ActivityStatus = Literal["ok", "error", "blocked", "auto"]


async def log_activity(
    user_id: uuid.UUID,
    actor: str,
    action: str,
    *,
    status: ActivityStatus = "ok",
    tool: str | None = None,
    email_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record a step. Failing to write the log never breaks the step itself.

    `detail` must hold ids, labels and short reasons only — never email bodies.
    """
    try:
        async with open_session() as db:
            db.add(
                ActivityLog(
                    user_id=user_id,
                    actor=actor,
                    action=action,
                    tool=tool,
                    email_id=email_id,
                    detail=detail or {},
                    status=status,
                )
            )
            await db.commit()
    except SQLAlchemyError as error:
        log.warning("activity_log_failed", action=action, error_type=type(error).__name__)
