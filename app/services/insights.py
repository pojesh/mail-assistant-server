"""Save triage results (email_insights rows)."""

import uuid

from sqlalchemy import select

from app.config import settings
from app.db import open_session
from app.llm.provider import model_for
from app.models import EmailInsight
from app.schemas.llm import InputCheck, TriageResult


async def has_insight(email_id: uuid.UUID) -> bool:
    async with open_session() as db:
        return (
            await db.scalar(select(EmailInsight.id).where(EmailInsight.email_id == email_id))
            is not None
        )


async def save_insight(
    user_id: uuid.UUID, email_id: uuid.UUID, triage: TriageResult, check: InputCheck
) -> None:
    """Insert or replace the insight for an email."""
    async with open_session() as db:
        insight = await db.scalar(select(EmailInsight).where(EmailInsight.email_id == email_id))
        if insight is None:
            insight = EmailInsight(user_id=user_id, email_id=email_id)
            db.add(insight)
        insight.category = triage.category
        insight.priority = triage.priority
        insight.sentiment = triage.sentiment
        insight.sentiment_score = triage.sentiment_score
        insight.summary = triage.summary
        insight.action_items = [item.model_dump() for item in triage.action_items]
        insight.meeting_intent = (
            triage.meeting_intent.model_dump() if triage.meeting_intent else None
        )
        insight.needs_reply = triage.needs_reply
        insight.suspicious = check.suspicious
        insight.suspicious_reasons = check.reasons
        insight.model = model_for("fast")
        insight.provider = settings.llm_provider
        await db.commit()
