"""Disconnect Google, and delete everything the app stores about a user."""

import contextlib

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import AppError
from app.google import gmail_api
from app.google.oauth import revoke_token
from app.logging import get_logger
from app.models import OAuthToken, User
from app.security.crypto import decrypt

log = get_logger(__name__)

# LangGraph's own checkpoint tables (created by AsyncPostgresSaver.setup()).
CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


async def _stop_watch(user: User) -> None:
    if not settings.pubsub_topic:
        return
    try:
        await gmail_api.stop_watch(user.id)
    except AppError as error:  # the token may already be revoked: nothing more to stop
        log.info("stop_watch_skipped", user_id=str(user.id), code=error.code)


async def disconnect_google(db: AsyncSession, user: User) -> None:
    """Stop Gmail push, revoke the Google token and forget it. Synced data is kept."""
    await _stop_watch(user)
    row = await db.scalar(select(OAuthToken).where(OAuthToken.user_id == user.id))
    if row is not None:
        secret = row.refresh_token_enc or row.access_token_enc
        with contextlib.suppress(AppError):  # undecryptable token: nothing left to revoke
            await revoke_token(decrypt(secret))
        await db.delete(row)
    user.status = "needs_reconnect"
    await db.commit()
    log.info("google_disconnected", user_id=str(user.id))


async def _delete_checkpoints(db: AsyncSession, user: User) -> None:
    """Remove this user's paused/finished graph runs (thread ids start with email:<user_id>:)."""
    # NOTE: deleted with SQL by thread-id prefix; one statement per table instead of one
    # adelete_thread call per email.
    prefix = f"email:{user.id}:%"
    for table in CHECKPOINT_TABLES:
        exists = await db.scalar(
            text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table}"}
        )
        if exists:
            await db.execute(
                text(f"DELETE FROM public.{table} WHERE thread_id LIKE :prefix"), {"prefix": prefix}
            )


async def delete_account(db: AsyncSession, user: User) -> None:
    """Disconnect Google, then delete the user; every user-owned row is removed by cascade."""
    await disconnect_google(db, user)
    await _delete_checkpoints(db, user)
    await db.execute(delete(User).where(User.id == user.id))
    await db.commit()
    log.info("account_deleted", user_id=str(user.id))
