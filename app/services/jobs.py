"""Work behind the internal routes: Gmail push, the 10-minute sweep, and daily maintenance.

Everything runs inside the request (Cloud Run throttles CPU after the response is sent).
"""

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError
from sqlalchemy import or_, select, update

from app.config import settings
from app.db import open_session
from app.errors import AppError, BadRequest, NeedsReconnect
from app.google import gmail_api
from app.logging import get_logger
from app.models import Approval, Digest, Email, SyncState, User
from app.services import digest as digest_service
from app.services.preferences import load_preferences
from app.services.sync import SyncResult, sync_user

log = get_logger(__name__)

SWEEP_IDLE = timedelta(minutes=10)
SWEEP_TIME_BUDGET_SECONDS = 480  # stop starting new work well before the 600s request timeout
WATCH_RENEW_BEFORE = timedelta(days=2)
APPROVAL_LIFETIME = timedelta(days=7)
BODY_RETENTION = timedelta(days=30)


class PushNotice(BaseModel):
    """The JSON inside a Gmail Pub/Sub message."""

    emailAddress: str  # noqa: N815 (Gmail's field name)
    historyId: int  # noqa: N815


def decode_push(data: str) -> PushNotice:
    """Decode the base64 `message.data` of a Pub/Sub push."""
    try:
        return PushNotice.model_validate(json.loads(base64.b64decode(data)))
    except (ValueError, ValidationError) as error:
        raise BadRequest("Not a Gmail notification.") from error


async def handle_push(data: str) -> Literal["synced", "busy", "ignored"]:
    """Sync the mailbox in the notification. "busy" means: ask Pub/Sub to redeliver later."""
    notice = decode_push(data)
    async with open_session() as db:
        user = await db.scalar(select(User).where(User.email == notice.emailAddress.lower()))
    if user is None or user.status != "active":
        return "ignored"  # unknown or disconnected mailbox: acknowledge so it is not retried
    try:
        result = await sync_user(user.id)
    except NeedsReconnect:
        return "ignored"  # the user must reconnect; retrying would not help
    return "busy" if result.status == "already_syncing" else "synced"


class SweepResult(BaseModel):
    synced: int = 0
    skipped: int = 0
    failed: int = 0
    digests: int = 0


async def _users_to_sync() -> list[User]:
    cutoff = datetime.now(UTC) - SWEEP_IDLE
    async with open_session() as db:
        rows = await db.scalars(
            select(User)
            .join(SyncState, SyncState.user_id == User.id)
            .where(
                User.status == "active",
                or_(SyncState.last_synced_at.is_(None), SyncState.last_synced_at < cutoff),
            )
            .order_by(SyncState.last_synced_at.nulls_first())
        )
        return list(rows)


async def _sync_one(user: User, result: SweepResult) -> None:
    try:
        outcome: SyncResult = await sync_user(user.id)
    except AppError as error:
        log.warning("sweep_sync_failed", user_id=str(user.id), code=error.code)
        result.failed += 1
        return
    if outcome.status == "already_syncing":
        result.skipped += 1
    else:
        result.synced += 1


async def _digest_if_due(user: User, result: SweepResult) -> None:
    prefs = await load_preferences(user.id)
    today = datetime.now(ZoneInfo(prefs.timezone)).date()
    async with open_session() as db:
        has_today = (
            await db.scalar(
                select(Digest.id).where(Digest.user_id == user.id, Digest.date == today)
            )
            is not None
        )
    if not digest_service.is_digest_due(prefs.timezone, has_today):
        return
    try:
        await digest_service.build_digest(user.id)
        result.digests += 1
    except AppError as error:
        log.warning("digest_failed", user_id=str(user.id), code=error.code)


async def run_sweep() -> SweepResult:
    """Catch-up sync for idle mailboxes, then today's digest where due. Time-boxed."""
    started = time.monotonic()
    result = SweepResult()
    for user in await _users_to_sync():
        if time.monotonic() - started > SWEEP_TIME_BUDGET_SECONDS:
            break  # the next sweep (10 minutes later) continues
        await _sync_one(user, result)
    async with open_session() as db:
        active = list(
            await db.scalars(select(User).where(User.status == "active", User.onboarded.is_(True)))
        )
    for user in active:
        if time.monotonic() - started > SWEEP_TIME_BUDGET_SECONDS:
            break
        await _digest_if_due(user, result)
    log.info("sweep_finished", **result.model_dump())
    return result


class MaintenanceResult(BaseModel):
    watches_renewed: int = 0
    watches_failed: int = 0
    approvals_expired: int = 0
    bodies_cleared: int = 0


async def renew_watches(result: MaintenanceResult) -> None:
    """Gmail watches expire after 7 days: renew those expiring within 2 days."""
    if not settings.pubsub_topic:
        return
    soon = datetime.now(UTC) + WATCH_RENEW_BEFORE
    async with open_session() as db:
        states = list(
            await db.scalars(
                select(SyncState)
                .join(User, User.id == SyncState.user_id)
                .where(
                    User.status == "active",
                    or_(SyncState.watch_expires_at.is_(None), SyncState.watch_expires_at < soon),
                )
            )
        )
        for state in states:
            try:
                state.watch_expires_at = (await gmail_api.start_watch(state.user_id)).expires_at
                result.watches_renewed += 1
            except AppError as error:
                log.warning("watch_renew_failed", user_id=str(state.user_id), code=error.code)
                result.watches_failed += 1
        await db.commit()


async def run_maintenance() -> MaintenanceResult:
    """Renew watches, expire week-old approvals, and clear email bodies older than 30 days."""
    result = MaintenanceResult()
    await renew_watches(result)
    now = datetime.now(UTC)
    async with open_session() as db:
        expired = await db.execute(
            update(Approval)
            .where(Approval.status == "pending", Approval.created_at < now - APPROVAL_LIFETIME)
            .values(status="expired")
        )
        cleared = await db.execute(
            update(Email)
            .where(Email.received_at < now - BODY_RETENTION, Email.body_text.is_not(None))
            .values(body_text=None)
        )
        await db.commit()
    result.approvals_expired = int(expired.rowcount or 0)  # type: ignore[attr-defined]
    result.bodies_cleared = int(cleared.rowcount or 0)  # type: ignore[attr-defined]
    log.info("maintenance_finished", **result.model_dump())
    return result
