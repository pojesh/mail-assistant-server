"""Onboarding helpers: suggest VIPs, learn the writing style, finish setup."""

import re
from collections import Counter

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.google import gmail_writes
from app.logging import get_logger
from app.models import Email, User
from app.services.activity import log_activity

log = get_logger(__name__)

AUTOMATED_SENDER = re.compile(
    r"(no-?reply|do-?not-?reply|notification|mailer|alerts?|news(letter)?|updates?)@", re.IGNORECASE
)
SUGGESTION_COUNT = 10


class VipSuggestion(BaseModel):
    email: str
    name: str | None
    messages: int  # emails exchanged with this person (sent + received)


async def suggest_vips(db: AsyncSession, user: User) -> list[VipSuggestion]:
    """The people the user exchanges the most mail with (automated senders are skipped)."""
    rows = (
        await db.execute(
            select(Email.is_sent, Email.to, Email.from_email, Email.from_name).where(
                Email.user_id == user.id
            )
        )
    ).all()
    counts: Counter[str] = Counter()
    names: dict[str, str] = {}
    for is_sent, to, from_email, from_name in rows:
        people = to if is_sent else [from_email]
        for address in people:
            if address != user.email and not AUTOMATED_SENDER.search(address):
                counts[address] += 1
        if not is_sent and from_name:
            names.setdefault(from_email, from_name)
    return [
        VipSuggestion(email=address, name=names.get(address), messages=count)
        for address, count in counts.most_common(SUGGESTION_COUNT)
    ]


async def complete_onboarding(db: AsyncSession, user: User) -> User:
    """Create the AI/* Gmail labels and mark the user as onboarded."""
    try:
        await gmail_writes.ensure_labels(user.id)
    except AppError as error:  # labels are re-tried on every labeling step anyway
        log.warning("ensure_labels_failed", user_id=str(user.id), code=error.code)
    user.onboarded = True
    await db.commit()
    await log_activity(user.id, "user", "complete_onboarding")
    return user
