"""Chat history: load a conversation for the agent, save messages, list sessions."""

import uuid

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFound
from app.models import ChatMessage
from app.schemas.chat import (
    ChatMessageOut,
    ChatSessionDetail,
    ChatSessionSummary,
    ChatStep,
    Citation,
)
from app.schemas.preferences import PreferencesBody
from app.tools.context import AgentContext

HISTORY_MESSAGES = 20  # recent turns sent to the model (free tiers limit tokens)
TITLE_LENGTH = 60


async def load_history(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> list[AnyMessage]:
    """The latest messages of a session, oldest first, as LangChain messages."""
    rows = list(
        await db.scalars(
            select(ChatMessage)
            .where(ChatMessage.user_id == user_id, ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(HISTORY_MESSAGES)
        )
    )
    rows.reverse()
    return [
        HumanMessage(row.content) if row.role == "user" else AIMessage(row.content) for row in rows
    ]


async def save_message(
    db: AsyncSession,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    role: str,
    content: str,
    steps: list[ChatStep] | None = None,
    citations: list[Citation] | None = None,
) -> ChatMessage:
    row = ChatMessage(
        user_id=user_id,
        session_id=session_id,
        role=role,
        content=content,
        steps=[step.model_dump() for step in steps or []],
        citations=[citation.model_dump() for citation in citations or []],
    )
    db.add(row)
    await db.commit()
    return row


def agent_context(user_id: uuid.UUID, prefs: PreferencesBody) -> AgentContext:
    """What chat tools may know: the user's meeting rules and timezone."""
    return AgentContext(
        user_id=user_id,
        timezone=prefs.timezone,
        context={
            "timezone": prefs.timezone,
            "meeting_preferences": prefs.meeting_preferences.model_dump(mode="json"),
            "sender_is_vip": False,
            "notes": [],
            "proposed_times": [],
        },
    )


async def list_sessions(db: AsyncSession, user_id: uuid.UUID) -> list[ChatSessionSummary]:
    """Conversations, most recently active first."""
    stats = (
        await db.execute(
            select(
                ChatMessage.session_id,
                func.count(),
                func.max(ChatMessage.created_at),
                func.min(ChatMessage.created_at),
            )
            .where(ChatMessage.user_id == user_id)
            .group_by(ChatMessage.session_id)
            .order_by(func.max(ChatMessage.created_at).desc())
            .limit(50)
        )
    ).all()
    summaries = []
    for session_id, count, last_at, first_at in stats:
        first = await db.scalar(
            select(ChatMessage.content).where(
                ChatMessage.session_id == session_id,
                ChatMessage.role == "user",
                ChatMessage.created_at == first_at,
            )
        )
        title = (first or "Conversation").strip().replace("\n", " ")
        summaries.append(
            ChatSessionSummary(
                session_id=session_id,
                title=title[:TITLE_LENGTH],
                message_count=count,
                updated_at=last_at,
            )
        )
    return summaries


async def get_session(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> ChatSessionDetail:
    rows = list(
        await db.scalars(
            select(ChatMessage)
            .where(ChatMessage.user_id == user_id, ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at)
        )
    )
    if not rows:
        raise NotFound("Conversation not found.")
    return ChatSessionDetail(
        session_id=session_id,
        messages=[
            ChatMessageOut(
                id=row.id,
                role="user" if row.role == "user" else "assistant",
                content=row.content,
                steps=[ChatStep.model_validate(step) for step in row.steps],
                citations=[Citation.model_validate(item) for item in row.citations],
                created_at=row.created_at,
            )
            for row in rows
        ],
    )
