"""Write wrappers: RFC 2822 drafts, own-attendee-only invite responses, read-only tools."""

import base64
import email
import inspect
import uuid
from typing import Any

import pytest

from app.google import calendar_api, gmail_writes
from app.tools import SCHEDULING_TOOLS

WRITE_FUNCTIONS = {
    "create_draft",
    "apply_labels",
    "create_event",
    "respond_to_invite",
    "delete_event",
    "send",
}


def decode(raw: str) -> email.message.Message:
    return email.message_from_bytes(base64.urlsafe_b64decode(raw.encode()))


def test_reply_message_has_threading_headers() -> None:
    raw = gmail_writes.build_reply_message(
        "ravi@x.com", "Invoice", "Hello Ravi\n\nThanks!", "<m2@x.com>", "<m1@x.com>"
    )
    message = decode(raw)
    assert message["To"] == "ravi@x.com"
    assert message["Subject"] == "Re: Invoice"
    assert message["In-Reply-To"] == "<m2@x.com>"
    assert message["References"] == "<m1@x.com> <m2@x.com>"
    assert "Thanks!" in message.get_payload(decode=True).decode()
    assert "+" not in raw and "/" not in raw


def test_reply_subject_is_not_doubled() -> None:
    assert (
        decode(gmail_writes.build_reply_message("a@b.c", "RE: Plan", "x", None, None))["Subject"]
        == "RE: Plan"
    )


async def test_create_draft_uses_thread_and_never_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Drafts:
        def create(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"id": "draft-1"}

    class Users:
        def drafts(self) -> Drafts:
            return Drafts()

        def messages(self) -> None:
            raise AssertionError("must not touch messages (no sending)")

    class Gmail:
        def users(self) -> Users:
            return Users()

    async def get_gmail(_: uuid.UUID) -> Gmail:
        return Gmail()

    async def run_google(request: Any) -> Any:
        return request

    monkeypatch.setattr(gmail_writes, "get_gmail", get_gmail)
    monkeypatch.setattr(gmail_writes, "run_google", run_google)
    draft_id = await gmail_writes.create_draft(
        uuid.uuid4(), "thread-9", "a@b.c", "Hi", "Body", "<m@x>"
    )
    assert draft_id == "draft-1"
    assert calls[0]["body"]["message"]["threadId"] == "thread-9"


async def test_respond_to_invite_only_changes_own_attendee(monkeypatch: pytest.MonkeyPatch) -> None:
    patched: list[dict[str, Any]] = []
    event = {
        "id": "e1",
        "start": {"dateTime": "2026-11-10T10:00:00+05:30"},
        "end": {"dateTime": "2026-11-10T10:30:00+05:30"},
        "attendees": [
            {"email": "organizer@x.com", "organizer": True, "responseStatus": "accepted"},
            {"email": "me@x.com", "self": True, "responseStatus": "needsAction"},
            {"email": "other@x.com", "responseStatus": "declined"},
        ],
    }

    class Events:
        def get(self, **kwargs: Any) -> dict[str, Any]:
            return event

        def patch(self, **kwargs: Any) -> dict[str, Any]:
            patched.append(kwargs)
            return {**event, "attendees": kwargs["body"]["attendees"]}

    class Calendar:
        def events(self) -> Events:
            return Events()

    async def get_calendar(_: uuid.UUID) -> Calendar:
        return Calendar()

    async def run_google(request: Any) -> Any:
        return request

    monkeypatch.setattr(calendar_api, "get_calendar", get_calendar)
    monkeypatch.setattr(calendar_api, "run_google", run_google)
    result = await calendar_api.respond_to_invite(uuid.uuid4(), "e1", "accepted")

    attendees = patched[0]["body"]["attendees"]
    assert [person["responseStatus"] for person in attendees] == [
        "accepted",
        "accepted",
        "declined",
    ]
    assert attendees[0] == event["attendees"][0] and attendees[2] == event["attendees"][2]
    assert patched[0]["sendUpdates"] == "all"
    assert result.my_response == "accepted"


def test_no_write_function_is_reachable_from_agent_tools() -> None:
    names = {tool.name for tool in SCHEDULING_TOOLS}
    assert names == {
        "get_meeting_preferences",
        "check_availability",
        "find_free_slots",
        "find_invite_event",
        "list_events",
    }
    assert not names & WRITE_FUNCTIONS
    for tool in SCHEDULING_TOOLS:
        source = inspect.getsource(inspect.getmodule(tool.coroutine))  # type: ignore[arg-type]
        assert "gmail_writes" not in source
        for name in ("create_event", "respond_to_invite", "delete_event", "create_draft"):
            assert f".{name}(" not in source


def test_gmail_modules_have_no_send_function() -> None:
    from app.google import gmail_api

    for module in (gmail_api, gmail_writes):
        assert not [name for name in dir(module) if "send" in name.lower()]
