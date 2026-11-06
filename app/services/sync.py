"""Gmail sync: the one code path used by Pub/Sub push, the sweep job and "Sync now".

1. Take a per-user Postgres advisory lock (only one sync per user at a time).
2. Find new message ids from Gmail history (or the newest inbox mail on the first sync).
3. Store up to MAX_EMAILS_PER_RUN new messages (already-stored ones are skipped) and run
   the email graph on each received one (sent ones are embedded as style examples).
4. Advance the stored history id only when everything up to it has been stored.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select, text

from app.config import settings
from app.db import get_engine, open_session
from app.errors import AppError, GoogleApiError, NotFound
from app.google import gmail_api
from app.logging import get_logger
from app.models import SyncState
from app.schemas.google import HistoryChanges
from app.services import emails as email_service
from app.services import processing

log = get_logger(__name__)


class SyncResult(BaseModel):
    status: Literal["synced", "already_syncing"]
    processed: int = 0
    remaining: int = 0


async def sync_user(user_id: uuid.UUID) -> SyncResult:
    """Sync one mailbox. Returns status "already_syncing" if another sync holds the lock."""
    async with get_engine().connect() as lock_connection, lock_connection.begin():
        # The lock is released automatically when this transaction ends.
        locked = await lock_connection.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"), {"key": str(user_id)}
        )
        if not locked:
            log.info("sync_skipped_locked", user_id=str(user_id))
            return SyncResult(status="already_syncing")
        return await _sync_locked(user_id)


async def _sync_locked(user_id: uuid.UUID) -> SyncResult:
    """The sync itself; only called while holding the user's lock."""
    history_id = await _load_history_id(user_id)
    changes = await _find_changes(user_id, history_id)

    async with open_session() as db:
        await email_service.set_unread(db, user_id, changes.read_message_ids, unread=False)
        await email_service.set_unread(db, user_id, changes.unread_message_ids, unread=True)
        new_ids = await email_service.find_unstored_ids(db, user_id, changes.added_message_ids)

    batch = new_ids[: settings.max_emails_per_run]
    # Leftover budget first retries emails whose triage failed on an earlier sync.
    await processing.retry_untriaged(user_id, settings.max_emails_per_run - len(batch))
    processed = 0
    for message_id in batch:
        if await _store_message(user_id, message_id):
            processed += 1

    remaining = len(new_ids) - processed
    # Only move forward when nothing is left behind; otherwise the next sync re-reads the
    # same history and skips the messages that are already stored.
    await _save_sync_state(user_id, changes.latest_history_id if remaining == 0 else history_id)
    log.info("sync_finished", user_id=str(user_id), processed=processed, remaining=remaining)
    return SyncResult(status="synced", processed=processed, remaining=remaining)


async def _load_history_id(user_id: uuid.UUID) -> str | None:
    async with open_session() as db:
        return await db.scalar(select(SyncState.history_id).where(SyncState.user_id == user_id))


async def _find_changes(user_id: uuid.UUID, history_id: str | None) -> HistoryChanges:
    """Changes since the last sync; on the first sync (or expired history) the newest inbox mail."""
    if history_id:
        try:
            return await gmail_api.list_history(user_id, history_id)
        except gmail_api.HistoryExpired:
            log.info("sync_history_expired", user_id=str(user_id))

    # NOTE: the first sync imports only the newest MAX_EMAILS_PER_RUN inbox emails, not the
    # whole mailbox. The history id is read first, so mail arriving meanwhile is not missed.
    latest_history_id = await gmail_api.get_profile_history_id(user_id)
    recent_ids = await gmail_api.list_recent_inbox_ids(user_id, settings.max_emails_per_run)
    return HistoryChanges(added_message_ids=recent_ids, latest_history_id=latest_history_id)


async def _store_message(user_id: uuid.UUID, message_id: str) -> bool:
    """Fetch, parse and store one message. False means "failed, try again next sync"."""
    try:
        parsed = await gmail_api.get_message(user_id, message_id)
    except NotFound:
        return True  # deleted in Gmail since the notification: nothing to store
    except GoogleApiError:
        log.warning("sync_message_failed", user_id=str(user_id), message_id=message_id)
        return False

    if not gmail_api.is_relevant(parsed.label_ids):
        return True
    async with open_session() as db:
        email = await email_service.save_email(db, user_id, parsed)
    if email is not None:
        await processing.process_email(email)
    return True


async def _save_sync_state(user_id: uuid.UUID, history_id: str | None) -> None:
    """Remember the history id and when we last synced."""
    async with open_session() as db:
        state = await db.scalar(select(SyncState).where(SyncState.user_id == user_id))
        if state is None:
            state = SyncState(user_id=user_id)
            db.add(state)
        state.history_id = history_id
        state.last_synced_at = datetime.now(UTC)
        await db.commit()


async def prepare_mailbox(user_id: uuid.UUID) -> None:
    """After sign-in: start the Gmail watch. Never blocks sign-in if it fails."""
    if not settings.pubsub_topic:
        log.info("gmail_watch_skipped_no_topic", user_id=str(user_id))
        return
    try:
        watch = await gmail_api.start_watch(user_id)
    except AppError as error:
        log.warning("gmail_watch_failed", user_id=str(user_id), code=error.code)
        return
    async with open_session() as db:
        state = await db.scalar(select(SyncState).where(SyncState.user_id == user_id))
        if state is not None:
            state.watch_expires_at = watch.expires_at
            await db.commit()
