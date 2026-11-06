"""Context for drafting and scheduling: preferences (always) + the most relevant notes and
past replies (RAG over pgvector)."""

import uuid
from typing import Any

from sqlalchemy import select

from app.db import open_session
from app.errors import AppError
from app.logging import get_logger
from app.models import Email, PreferenceNote, SentEmbedding
from app.services.autonomy import email_domain, is_vip
from app.services.embeddings import embed_query
from app.services.preferences import load_preferences

log = get_logger(__name__)

TOP_NOTES = 3
TOP_EXAMPLES = 3
DOMAIN_BOOST = 0.1  # past replies to the same company are better style examples


async def _top_notes(user_id: uuid.UUID, vector: list[float] | None) -> list[str]:
    """Most relevant active notes; notes without an embedding are used as a fallback."""
    async with open_session() as db:
        active = select(PreferenceNote).where(
            PreferenceNote.user_id == user_id, PreferenceNote.status == "active"
        )
        if vector is not None:
            ranked = active.where(PreferenceNote.embedding.is_not(None)).order_by(
                PreferenceNote.embedding.cosine_distance(vector)
            )
            notes = [note.text for note in await db.scalars(ranked.limit(TOP_NOTES))]
        else:
            notes = []
        if len(notes) < TOP_NOTES:
            recent = active.where(PreferenceNote.embedding.is_(None)).order_by(
                PreferenceNote.created_at.desc()
            )
            notes += [note.text for note in await db.scalars(recent.limit(TOP_NOTES - len(notes)))]
        return notes


async def _top_examples(user_id: uuid.UUID, vector: list[float], sender: str) -> list[str]:
    """Past replies most similar to this email; +0.1 when sent to the same domain."""
    distance = SentEmbedding.embedding.cosine_distance(vector).label("distance")
    statement = (
        select(SentEmbedding.chunk, Email.to, distance)
        .join(Email, Email.id == SentEmbedding.email_id)
        .where(SentEmbedding.user_id == user_id)
        .order_by(distance)
        .limit(TOP_EXAMPLES * 3)
    )
    async with open_session() as db:
        rows = (await db.execute(statement)).all()
    domain = email_domain(sender)
    scored = [
        (
            1
            - float(row.distance)
            + (DOMAIN_BOOST if any(email_domain(to) == domain for to in row.to) else 0),
            row.chunk,
        )
        for row in rows
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk for _, chunk in scored[:TOP_EXAMPLES]]


async def get_context(user_id: uuid.UUID, email: dict[str, Any]) -> dict[str, Any]:
    """Everything an agent may know about the user for this email (JSON-safe dict)."""
    prefs = await load_preferences(user_id)
    sender = str(email.get("from_email", ""))
    query = f"{email.get('subject', '')}\n{(email.get('body_text') or '')[:1500]}"

    vector: list[float] | None = None
    try:
        vector = await embed_query(query)
    except AppError as error:  # retrieval is a bonus: drafting still works without it
        log.warning("retrieval_embedding_failed", code=error.code)

    return {
        "signature": prefs.signature,
        "sign_off": prefs.sign_off,
        "tone": prefs.tone,
        "language": prefs.language,
        "timezone": prefs.timezone,
        "vip_senders": prefs.vip_senders,
        "sender_is_vip": is_vip(sender, prefs.vip_senders),
        "meeting_preferences": prefs.meeting_preferences.model_dump(mode="json"),
        "notes": await _top_notes(user_id, vector),
        "examples": await _top_examples(user_id, vector, sender) if vector else [],
    }
