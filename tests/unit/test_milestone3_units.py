"""OIDC check for internal routes, chat streaming events, push decoding, digest timing."""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import Request
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents import chat as chat_agent
from app.config import settings
from app.errors import BadRequest, Unauthorized
from app.schemas.chat import ChatStepEvent, ChatTokenEvent, Citation
from app.schemas.google import GmailThreadSummary
from app.security import oidc
from app.security.rate_limit import user_key
from app.security.session import create_signed_token
from app.services.digest import is_digest_due
from app.services.jobs import decode_push
from app.tools import CHAT_TOOLS, gmail_tools
from app.tools.context import AgentContext

SA = "internal-invoker@project.iam.gserviceaccount.com"


def request_with(headers: dict[str, str] | None = None, cookies: str = "") -> Request:
    raw = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    if cookies:
        raw.append((b"cookie", cookies.encode()))
    return Request({"type": "http", "headers": raw, "client": ("1.2.3.4", 1234)})


@pytest.fixture
def internal_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "internal_audience", "https://api.example.run.app")
    monkeypatch.setattr(settings, "internal_invoker_sa", SA)


@pytest.mark.parametrize("header", [None, "Basic abc", "Bearer "])
async def test_internal_call_without_bearer_token_is_rejected(
    internal_settings: None, header: str | None
) -> None:
    with pytest.raises(Unauthorized):
        await oidc.require_internal_caller(
            request_with({"Authorization": header} if header else None)
        )


async def test_internal_call_with_invalid_token_is_rejected(
    internal_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid(_: str) -> dict[str, Any]:
        raise ValueError("Token has wrong audience")

    monkeypatch.setattr(oidc, "_verify", invalid)
    with pytest.raises(Unauthorized):
        await oidc.require_internal_caller(request_with({"Authorization": "Bearer tok"}))


@pytest.mark.parametrize(
    ("claims", "allowed"),
    [
        ({"email": SA, "email_verified": True}, True),
        ({"email": SA, "email_verified": False}, False),
        ({"email": "someone@gmail.com", "email_verified": True}, False),
        ({}, False),
    ],
)
async def test_only_the_invoker_service_account_is_allowed(
    internal_settings: None, monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any], allowed: bool
) -> None:
    monkeypatch.setattr(oidc, "_verify", lambda _: claims)
    call = oidc.require_internal_caller(request_with({"Authorization": "Bearer tok"}))
    if allowed:
        await call
    else:
        with pytest.raises(Unauthorized):
            await call


def test_rate_limit_key_is_per_user() -> None:
    token = create_signed_token({"sub": "user-42"}, timedelta(minutes=5))
    assert user_key(request_with(cookies=f"session={token}")) == "user:user-42"
    assert user_key(request_with()) == "ip:1.2.3.4"


def test_decode_push() -> None:
    data = base64.b64encode(
        json.dumps({"emailAddress": "Me@Example.com", "historyId": 123}).encode()
    ).decode()
    notice = decode_push(data)
    assert notice.emailAddress == "Me@Example.com" and notice.historyId == 123
    with pytest.raises(BadRequest):
        decode_push(base64.b64encode(b"not json").decode())


def test_digest_is_due_once_after_8_local() -> None:
    morning = datetime(2026, 11, 9, 2, 0, tzinfo=UTC)  # 07:30 in India
    later = datetime(2026, 11, 9, 3, 0, tzinfo=UTC)  # 08:30 in India
    assert not is_digest_due("Asia/Kolkata", has_today=False, now=morning)
    assert is_digest_due("Asia/Kolkata", has_today=False, now=later)
    assert not is_digest_due("Asia/Kolkata", has_today=True, now=later)


def test_chat_tools_are_read_only_or_hand_off() -> None:
    names = {tool.name for tool in CHAT_TOOLS}
    assert names == {
        "search_threads",
        "get_thread",
        "search_attachments",
        "search_my_sent_mail",
        "list_events",
        "find_free_slots",
        "start_draft_flow",
        "start_schedule_flow",
    }


def test_citations_come_from_mail_tools_only() -> None:
    hits = json.dumps([{"thread_id": "t1", "subject": "Invoice", "email_id": "e1"}, {"oops": 1}])
    assert chat_agent.citations_from(
        ToolMessage(hits, tool_call_id="1", name="search_threads")
    ) == [Citation(thread_id="t1", subject="Invoice", email_id="e1")]
    assert chat_agent.citations_from(ToolMessage(hits, tool_call_id="2", name="list_events")) == []
    assert (
        chat_agent.citations_from(ToolMessage("not json", tool_call_id="3", name="get_thread"))
        == []
    )


class ToolCallingFake(FakeMessagesListChatModel):
    """Fake chat model that accepts tools (it just replays scripted messages)."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ToolCallingFake":
        return self


async def test_chat_streams_steps_tokens_and_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    model = ToolCallingFake(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {"name": "search_threads", "args": {"query": "from:ravi invoice"}, "id": "c1"}
                ],
            ),
            AIMessage("Ravi sent the invoice on Monday."),
        ]
    )
    monkeypatch.setattr(chat_agent, "get_chat_model", lambda tier: model)

    async def fake_search(user_id: uuid.UUID, query: str, limit: int) -> list[GmailThreadSummary]:
        return [
            GmailThreadSummary(
                thread_id="t1",
                subject="Invoice",
                last_from="Ravi",
                snippet="",
                last_date=None,
                message_count=1,
            )
        ]

    async def no_stored(context: AgentContext, ids: list[str]) -> dict[str, str]:
        return {"t1": "email-1"}

    monkeypatch.setattr(gmail_tools.gmail_api, "search_threads", fake_search)
    monkeypatch.setattr(gmail_tools, "_stored_email_ids", no_stored)

    citations = chat_agent.CitationTracker()
    context = AgentContext(user_id=uuid.uuid4(), timezone="Asia/Kolkata")
    events = [
        event
        async for event in chat_agent.run_chat(
            [HumanMessage("where is the invoice?")], context, citations
        )
    ]

    assert events[0] == ChatStepEvent(tool="search_threads", label="Searching mail…")
    assert ChatTokenEvent(text="Ravi sent the invoice on Monday.") in events
    assert citations.best() == [Citation(thread_id="t1", subject="Invoice", email_id="email-1")]


def test_opened_conversations_are_cited_before_search_hits() -> None:
    tracker = chat_agent.CitationTracker()
    hits = [{"thread_id": f"t{n}", "subject": f"Hit {n}"} for n in range(6)]
    tracker.add(ToolMessage(json.dumps(hits), tool_call_id="1", name="search_threads"))
    assert [c.thread_id for c in tracker.best()] == ["t0", "t1", "t2"]  # top search hits only
    opened = {"thread_id": "t4", "subject": "Hit 4", "email_id": "e4"}
    tracker.add(ToolMessage(json.dumps(opened), tool_call_id="2", name="get_thread"))
    assert [c.thread_id for c in tracker.best()] == ["t4"]
