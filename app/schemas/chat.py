"""Chat API models, including the Server-Sent Events streamed by POST /api/chat.

The SSE payloads are added to the OpenAPI components (see app/main.py) so the web app gets
types for them too. Each event is sent as `event: <type>` + `data: <json>`.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: uuid.UUID | None = None  # omit to start a new conversation


class Citation(BaseModel):
    """An email the answer is based on (shown as a chip)."""

    thread_id: str
    subject: str
    email_id: str | None = None  # set when the email is stored (opens in the app)


class ChatStep(BaseModel):
    tool: str
    label: str  # e.g. "Searching mail…"


class ChatStepEvent(BaseModel):
    type: Literal["step"] = "step"
    tool: str
    label: str


class ChatTokenEvent(BaseModel):
    type: Literal["token"] = "token"
    text: str


class ChatDoneEvent(BaseModel):
    type: Literal["done"] = "done"
    session_id: uuid.UUID
    message_id: uuid.UUID
    content: str
    citations: list[Citation]
    steps: list[ChatStep]


class ChatErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


ChatStreamEvent = Annotated[
    ChatStepEvent | ChatTokenEvent | ChatDoneEvent | ChatErrorEvent, Field(discriminator="type")
]


class ChatMessageOut(BaseModel):
    id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    steps: list[ChatStep]
    citations: list[Citation]
    created_at: datetime


class ChatSessionSummary(BaseModel):
    session_id: uuid.UUID
    title: str  # the first question, shortened
    message_count: int
    updated_at: datetime


class ChatSessionDetail(BaseModel):
    session_id: uuid.UUID
    messages: list[ChatMessageOut]
