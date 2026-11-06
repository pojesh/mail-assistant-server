"""Preferences page: structured preferences, meeting rules, auto-scheduling, and notes."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.schemas.common import OkResponse
from app.schemas.preferences import (
    NoteCreate,
    NoteStatus,
    NoteUpdate,
    PreferenceNoteOut,
    PreferencesBody,
)
from app.security.session import current_user
from app.services import notes
from app.services import preferences as preference_service

router = APIRouter(tags=["preferences"])


@router.get("/preferences", response_model=PreferencesBody)
async def get_preferences(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> PreferencesBody:
    """Everything the assistant uses about the user (except notes)."""
    row = await preference_service.get_or_create_row(db, user.id)
    await db.commit()
    return preference_service.to_body(row)


@router.put("/preferences", response_model=PreferencesBody)
async def update_preferences(
    body: PreferencesBody, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> PreferencesBody:
    """Replace the structured preferences (validated: timezone, time ranges, limits)."""
    return await preference_service.save_preferences(db, user.id, body)


@router.get("/preferences/notes", response_model=list[PreferenceNoteOut])
async def list_preference_notes(
    status: NoteStatus | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PreferenceNoteOut]:
    """Notes, optionally only active / suggested / rejected ones."""
    return await notes.list_notes(db, user.id, status)


@router.post("/preferences/notes", response_model=PreferenceNoteOut, status_code=201)
async def create_preference_note(
    body: NoteCreate, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> PreferenceNoteOut:
    return await notes.create_note(db, user.id, body)


@router.patch("/preferences/notes/{note_id}", response_model=PreferenceNoteOut)
async def update_preference_note(
    note_id: uuid.UUID,
    body: NoteUpdate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> PreferenceNoteOut:
    """Edit a note, or accept (status=active) / reject a learned suggestion."""
    return await notes.update_note(db, user.id, note_id, body)


@router.delete("/preferences/notes/{note_id}", response_model=OkResponse)
async def delete_preference_note(
    note_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> OkResponse:
    await notes.delete_note(db, user.id, note_id)
    return OkResponse()
