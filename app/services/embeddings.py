"""Embeddings for retrieval: sent emails (writing-style examples) and preference notes."""

import asyncio
import uuid

from sqlalchemy import select

from app.agents.email_format import cap_text
from app.db import open_session
from app.errors import AppError
from app.google import gmail_api
from app.google.text import strip_quoted_text
from app.llm.provider import get_embeddings
from app.llm.retry import call_with_retry
from app.logging import get_logger
from app.models import Email, PreferenceNote, SentEmbedding
from app.services import emails as email_service

log = get_logger(__name__)

BATCH_SIZE = 50
CHUNK_LIMIT = 1_500  # characters per sent-mail example
PAUSE_BETWEEN_BATCHES = 1.0  # seconds; keeps free-tier embedding quotas happy
STYLE_EXAMPLES_ON_ONBOARDING = 200


async def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed texts for storage, in batches of 50."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start : start + BATCH_SIZE]

        async def embed_batch(batch: list[str] = batch) -> list[list[float]]:
            return await get_embeddings().aembed_documents(batch, task_type="RETRIEVAL_DOCUMENT")

        vectors += await call_with_retry(embed_batch, purpose="embed_documents")
        if start + BATCH_SIZE < len(texts):
            await asyncio.sleep(PAUSE_BETWEEN_BATCHES)
    return vectors


async def embed_query(text: str) -> list[float]:
    """Embed a search query (uses the retrieval-query task type)."""
    return await call_with_retry(
        lambda: get_embeddings().aembed_query(text, task_type="RETRIEVAL_QUERY"),
        purpose="embed_query",
    )


def style_chunk(email: Email) -> str:
    """The part of a sent email that shows how the user writes (quotes removed, capped)."""
    return cap_text(strip_quoted_text(email.body_text or ""), CHUNK_LIMIT)


async def embed_sent_emails(user_id: uuid.UUID, sent: list[Email]) -> int:
    """Store one embedded chunk per sent email that has text. Returns how many were added."""
    candidates = [(email, style_chunk(email)) for email in sent]
    candidates = [(email, chunk) for email, chunk in candidates if len(chunk) >= 20]
    if not candidates:
        return 0
    async with open_session() as db:
        existing = set(
            await db.scalars(
                select(SentEmbedding.email_id).where(
                    SentEmbedding.email_id.in_([email.id for email, _ in candidates])
                )
            )
        )
    candidates = [(email, chunk) for email, chunk in candidates if email.id not in existing]
    vectors = await embed_documents([chunk for _, chunk in candidates])
    async with open_session() as db:
        for (email, chunk), vector in zip(candidates, vectors, strict=True):
            db.add(SentEmbedding(user_id=user_id, email_id=email.id, chunk=chunk, embedding=vector))
        await db.commit()
    return len(candidates)


async def embed_note(note_id: uuid.UUID) -> None:
    """Embed one preference note. On failure the note still works (just not via search)."""
    async with open_session() as db:
        note = await db.get(PreferenceNote, note_id)
        if note is None:
            return
        try:
            note.embedding = (await embed_documents([note.text]))[0]
        except AppError as error:
            log.warning("note_embedding_failed", note_id=str(note_id), code=error.code)
            return
        await db.commit()


async def learn_writing_style(user_id: uuid.UUID, limit: int = STYLE_EXAMPLES_ON_ONBOARDING) -> int:
    """Onboarding: import and embed the user's most recent sent emails. Returns the count."""
    message_ids = await gmail_api.list_sent_ids(user_id, limit)
    async with open_session() as db:
        new_ids = await email_service.find_unstored_ids(db, user_id, message_ids)

    for message_id in new_ids:
        try:
            parsed = await gmail_api.get_message(user_id, message_id)
        except AppError as error:
            log.info("style_message_skipped", code=error.code)
            continue
        async with open_session() as db:
            await email_service.save_email(db, user_id, parsed)

    async with open_session() as db:
        sent = list(
            await db.scalars(
                select(Email).where(Email.user_id == user_id, Email.gmail_id.in_(message_ids))
            )
        )
    added = await embed_sent_emails(user_id, sent)
    log.info("writing_style_learned", user_id=str(user_id), emails=len(sent), embedded=added)
    return added
