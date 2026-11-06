"""Chat with your inbox: an SSE stream of steps and tokens, plus conversation history."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.chat import CitationTracker, run_chat
from app.db import get_db, open_session
from app.errors import AppError
from app.logging import get_logger
from app.models import User
from app.schemas.chat import (
    ChatDoneEvent,
    ChatErrorEvent,
    ChatRequest,
    ChatSessionDetail,
    ChatSessionSummary,
    ChatStep,
    ChatStepEvent,
    ChatTokenEvent,
)
from app.security.rate_limit import CHAT_LIMIT, limit
from app.security.session import current_user
from app.services import chat as chat_service
from app.services.preferences import load_preferences

log = get_logger(__name__)
router = APIRouter(tags=["chat"])

SSE_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Server-Sent Events: step, token, done, error",
        "content": {
            "text/event-stream": {"schema": {"$ref": "#/components/schemas/ChatStreamEvent"}}
        },
    }
}
NO_ANSWER = "I could not find an answer to that. Try rephrasing or naming the sender or date."


def sse(event: ChatStepEvent | ChatTokenEvent | ChatDoneEvent | ChatErrorEvent) -> str:
    """One Server-Sent Event: `event: <type>` and a JSON `data:` line."""
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


async def chat_events(user_id: uuid.UUID, body: ChatRequest) -> AsyncIterator[str]:
    """Run the agent and stream its events. All work happens inside the request (Cloud Run)."""
    session_id = body.session_id or uuid.uuid4()
    steps: list[ChatStep] = []
    tokens: list[str] = []
    citations = CitationTracker()
    try:
        # NOTE: sessions are opened here, not via Depends: FastAPI closes request-scoped
        # dependencies before a streaming body finishes.
        async with open_session() as db:
            history = await chat_service.load_history(db, user_id, session_id)
            await chat_service.save_message(db, user_id, session_id, "user", body.message)
        context = chat_service.agent_context(user_id, await load_preferences(user_id))
        history.append(HumanMessage(body.message))

        async for event in run_chat(history, context, citations):
            if isinstance(event, ChatStepEvent):
                steps.append(ChatStep(tool=event.tool, label=event.label))
            else:
                tokens.append(event.text)
            yield sse(event)

        answer = "".join(tokens).strip() or NO_ANSWER
        cited = citations.best()
        async with open_session() as db:
            saved = await chat_service.save_message(
                db, user_id, session_id, "assistant", answer, steps, cited
            )
        yield sse(
            ChatDoneEvent(
                session_id=session_id,
                message_id=saved.id,
                content=answer,
                citations=cited,
                steps=steps,
            )
        )
    except AppError as error:
        yield sse(ChatErrorEvent(code=error.code, message=error.message))
    except Exception as error:  # the stream has started: report the error as an event
        log.error("chat_stream_crashed", error_type=type(error).__name__, exc_info=error)
        yield sse(ChatErrorEvent(code="internal_error", message=AppError.default_message))


@router.post("/chat", response_class=StreamingResponse, responses=SSE_RESPONSE)
@limit(CHAT_LIMIT)
async def chat(
    request: Request, body: ChatRequest, user: User = Depends(current_user)
) -> StreamingResponse:
    """Ask about your mail and calendar. Streams `step`, `token`, then `done` (or `error`)."""
    return StreamingResponse(
        chat_events(user.id, body),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/sessions", response_model=list[ChatSessionSummary])
async def list_chat_sessions(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> list[ChatSessionSummary]:
    return await chat_service.list_sessions(db, user.id)


@router.get("/chat/sessions/{session_id}", response_model=ChatSessionDetail)
async def get_chat_session(
    session_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> ChatSessionDetail:
    return await chat_service.get_session(db, user.id, session_id)
