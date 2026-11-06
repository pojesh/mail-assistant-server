"""Small response models shared by many routes."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class OkResponse(BaseModel):
    ok: bool = True


class MeResponse(BaseModel):
    """The signed-in user, as shown in the app shell."""

    id: uuid.UUID
    email: str
    name: str | None
    picture_url: str | None
    status: Literal["active", "needs_reconnect"]
    onboarded: bool
    llm_provider: Literal["google", "groq"]
    last_synced_at: datetime | None = None  # for "Synced 2m ago" in the top bar
