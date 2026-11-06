"""Preference notes: free-text rules from the user, and suggestions learned from edits."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFound
from app.models import PreferenceNote
from app.schemas.preferences import NoteCreate, NoteStatus, NoteUpdate, PreferenceNoteOut
from app.services.embeddings import embed_note


def to_out(note: PreferenceNote) -> PreferenceNoteOut:
    return PreferenceNoteOut(
        id=note.id,
        text=note.text,
        source=note.source,
        status=note.status,
        created_at=note.created_at,
    )


async def list_notes(
    db: AsyncSession, user_id: uuid.UUID, status: NoteStatus | None
) -> list[PreferenceNoteOut]:
    statement = select(PreferenceNote).where(PreferenceNote.user_id == user_id)
    if status:
        statement = statement.where(PreferenceNote.status == status)
    rows = await db.scalars(statement.order_by(PreferenceNote.created_at.desc()))
    return [to_out(note) for note in rows]


async def _get_note(db: AsyncSession, user_id: uuid.UUID, note_id: uuid.UUID) -> PreferenceNote:
    note = await db.scalar(
        select(PreferenceNote).where(
            PreferenceNote.id == note_id, PreferenceNote.user_id == user_id
        )
    )
    if note is None:
        raise NotFound("Note not found.")
    return note


async def create_note(db: AsyncSession, user_id: uuid.UUID, body: NoteCreate) -> PreferenceNoteOut:
    """Add an active note written by the user, and embed it for retrieval."""
    note = PreferenceNote(user_id=user_id, text=body.text.strip(), source="user", status="active")
    db.add(note)
    await db.commit()
    await embed_note(note.id)
    return to_out(note)


async def update_note(
    db: AsyncSession, user_id: uuid.UUID, note_id: uuid.UUID, body: NoteUpdate
) -> PreferenceNoteOut:
    """Edit text and/or accept/reject. Accepted or edited notes are (re-)embedded."""
    note = await _get_note(db, user_id, note_id)
    text_changed = body.text is not None and body.text.strip() != note.text
    if body.text is not None:
        note.text = body.text.strip()
    if body.status is not None:
        note.status = body.status
    if text_changed:
        note.embedding = None
    await db.commit()
    if note.status == "active" and note.embedding is None:
        await embed_note(note.id)
        await db.refresh(note)
    return to_out(note)


async def delete_note(db: AsyncSession, user_id: uuid.UUID, note_id: uuid.UUID) -> None:
    note = await _get_note(db, user_id, note_id)
    await db.delete(note)
    await db.commit()
