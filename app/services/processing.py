"""Work done after an email is stored: triage received mail, embed sent mail."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import open_session
from app.errors import AppError
from app.logging import get_logger
from app.models import Email, EmailInsight
from app.services import flows
from app.services.activity import log_activity
from app.services.embeddings import embed_sent_emails
from app.services.insights import has_insight

log = get_logger(__name__)

RETRY_WINDOW = timedelta(days=3)  # untriaged mail older than this is left alone


async def process_email(email: Email) -> bool:
    """Run the email graph (received mail) or embed it (sent mail). True if it succeeded.

    Failures are logged and never stop the sync; untriaged mail is retried on later syncs.
    """
    try:
        if email.is_sent:
            await embed_sent_emails(email.user_id, [email])
            return True
        if await has_insight(email.id):  # idempotent: never triage twice
            return True
        await flows.run_new_email(email)
        return True
    except AppError as error:
        log.warning("email_processing_failed", email_id=str(email.id), code=error.code)
        await log_activity(
            email.user_id,
            "pipeline",
            "process_email",
            status="error",
            email_id=email.id,
            detail={"code": error.code},
        )
    except Exception as error:  # a bug in one email must not block the whole mailbox
        log.error(
            "email_processing_crashed",
            email_id=str(email.id),
            error_type=type(error).__name__,
            exc_info=error,
        )
        await log_activity(
            email.user_id,
            "pipeline",
            "process_email",
            status="error",
            email_id=email.id,
            detail={"code": "internal_error"},
        )
    return False


async def retry_untriaged(user_id: uuid.UUID, budget: int) -> int:
    """Triage recent received emails that have no insight yet (e.g. the LLM was rate-limited)."""
    if budget <= 0:
        return 0
    since = datetime.now(UTC) - RETRY_WINDOW
    async with open_session() as db:
        pending = list(
            await db.scalars(
                select(Email)
                .outerjoin(EmailInsight, EmailInsight.email_id == Email.id)
                .where(
                    Email.user_id == user_id,
                    Email.is_sent.is_(False),
                    Email.received_at >= since,
                    EmailInsight.id.is_(None),
                )
                .order_by(Email.received_at)
                .limit(budget)
            )
        )
    done = 0
    for email in pending:
        if await process_email(email):
            done += 1
    return done
