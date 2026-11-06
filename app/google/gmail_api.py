"""Typed async wrappers around the Gmail API (read operations and the watch).

There is deliberately NO send-email function. Write operations (labels, drafts) live in
`gmail_writes.py` and are only called by graph execution nodes, never by LLM tools.
"""

import html
import uuid
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.errors import NotFound
from app.google.clients import get_gmail, run_google
from app.google.mime import decode_header_value, parse_message
from app.schemas.google import (
    GmailThread,
    GmailThreadMessage,
    GmailThreadSummary,
    HistoryChanges,
    ParsedEmail,
    WatchResult,
)

KEEP_LABELS = {"INBOX", "SENT"}  # we store received and sent mail
SKIP_LABELS = {"DRAFT", "SPAM", "TRASH", "CHAT"}
MAX_HISTORY_PAGES = 20  # 500 records each; more than this means "start over"


class HistoryExpired(Exception):
    """The stored history id is too old (or too far behind) to continue from."""


def is_relevant(label_ids: list[str]) -> bool:
    """Only inbox and sent messages are stored; drafts, spam, trash and chats are ignored."""
    labels = set(label_ids)
    return bool(labels & KEEP_LABELS) and not labels & SKIP_LABELS


async def get_profile_history_id(user_id: uuid.UUID) -> str:
    """The mailbox's current history id (`users.getProfile`)."""
    gmail = await get_gmail(user_id)
    profile = await run_google(gmail.users().getProfile(userId="me"))
    return str(profile["historyId"])


async def start_watch(user_id: uuid.UUID) -> WatchResult:
    """Ask Gmail to push new-inbox notifications to our Pub/Sub topic (expires after 7 days)."""
    gmail = await get_gmail(user_id)
    body = {"topicName": settings.pubsub_topic, "labelIds": ["INBOX"]}
    response = await run_google(gmail.users().watch(userId="me", body=body))
    expires_at = datetime.fromtimestamp(int(response["expiration"]) / 1000, tz=UTC)
    return WatchResult(history_id=str(response["historyId"]), expires_at=expires_at)


async def stop_watch(user_id: uuid.UUID) -> None:
    """Stop push notifications for this mailbox."""
    gmail = await get_gmail(user_id)
    await run_google(gmail.users().stop(userId="me"))


def _collect_changes(record: dict[str, Any], changes: HistoryChanges, seen: set[str]) -> None:
    """Add one history record's relevant changes to `changes`."""
    for item in record.get("messagesAdded", []):
        message = item["message"]
        if message["id"] not in seen and is_relevant(message.get("labelIds", [])):
            seen.add(message["id"])
            changes.added_message_ids.append(message["id"])
    for item in record.get("labelsRemoved", []):
        if "UNREAD" in item.get("labelIds", []):
            changes.read_message_ids.append(item["message"]["id"])
    for item in record.get("labelsAdded", []):
        if "UNREAD" in item.get("labelIds", []):
            changes.unread_message_ids.append(item["message"]["id"])


async def list_history(user_id: uuid.UUID, start_history_id: str) -> HistoryChanges:
    """Everything that changed since `start_history_id` (`users.history.list`, all pages)."""
    gmail = await get_gmail(user_id)
    changes = HistoryChanges(latest_history_id=start_history_id)
    seen: set[str] = set()
    page_token: str | None = None

    for _ in range(MAX_HISTORY_PAGES):
        request = (
            gmail.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded", "labelAdded", "labelRemoved"],
                maxResults=500,
                pageToken=page_token,
            )
        )
        try:
            response = await run_google(request)
        except NotFound as error:  # Gmail keeps history for about a week
            raise HistoryExpired() from error
        changes.latest_history_id = str(response.get("historyId", changes.latest_history_id))
        for record in response.get("history", []):
            _collect_changes(record, changes, seen)
        page_token = response.get("nextPageToken")
        if not page_token:
            return changes
    raise HistoryExpired()


async def list_recent_inbox_ids(user_id: uuid.UUID, limit: int) -> list[str]:
    """Ids of the newest inbox messages, oldest first (used for the first sync)."""
    gmail = await get_gmail(user_id)
    request = gmail.users().messages().list(userId="me", labelIds=["INBOX"], maxResults=limit)
    response = await run_google(request)
    ids = [message["id"] for message in response.get("messages", [])]
    return list(reversed(ids))


async def get_message(user_id: uuid.UUID, message_id: str) -> ParsedEmail:
    """One full message, parsed (`users.messages.get(format=full)`)."""
    gmail = await get_gmail(user_id)
    request = gmail.users().messages().get(userId="me", id=message_id, format="full")
    return parse_message(await run_google(request))


async def list_sent_ids(user_id: uuid.UUID, limit: int = 200) -> list[str]:
    """Ids of the user's most recent sent messages (style examples for drafting)."""
    gmail = await get_gmail(user_id)
    ids: list[str] = []
    page_token: str | None = None
    while len(ids) < limit:
        request = (
            gmail.users()
            .messages()
            .list(
                userId="me",
                q="in:sent",
                maxResults=min(100, limit - len(ids)),
                pageToken=page_token,
            )
        )
        response = await run_google(request)
        ids += [message["id"] for message in response.get("messages", [])]
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return ids[:limit]


THREAD_BODY_LIMIT = 2_000  # characters per message when an agent reads a thread
MAX_THREAD_MESSAGES = 8  # the latest messages only


def _header(message: dict[str, Any], name: str) -> str:
    for header in message.get("payload", {}).get("headers", []):
        if header["name"].lower() == name.lower():
            return str(header["value"])
    return ""


def _thread_summary(thread: dict[str, Any], snippet: str) -> GmailThreadSummary:
    messages = thread.get("messages", [])
    first, last = (messages[0], messages[-1]) if messages else ({}, {})
    internal = last.get("internalDate")
    return GmailThreadSummary(
        thread_id=thread["id"],
        subject=decode_header_value(_header(first, "Subject")),
        last_from=decode_header_value(_header(last, "From")),
        snippet=html.unescape(snippet),
        last_date=datetime.fromtimestamp(int(internal) / 1000, tz=UTC) if internal else None,
        message_count=len(messages),
    )


async def search_threads(
    user_id: uuid.UUID, query: str, limit: int = 10
) -> list[GmailThreadSummary]:
    """Gmail search (same syntax as the Gmail search box), newest first, metadata only."""
    gmail = await get_gmail(user_id)
    listing = await run_google(gmail.users().threads().list(userId="me", q=query, maxResults=limit))
    summaries = []
    for item in listing.get("threads", []):
        request = (
            gmail.users()
            .threads()
            .get(
                userId="me",
                id=item["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
        )
        summaries.append(_thread_summary(await run_google(request), item.get("snippet", "")))
    return summaries


async def get_thread(user_id: uuid.UUID, thread_id: str) -> GmailThread:
    """A whole conversation with cleaned, capped bodies (latest messages only)."""
    gmail = await get_gmail(user_id)
    thread = await run_google(gmail.users().threads().get(userId="me", id=thread_id, format="full"))
    parsed = [parse_message(message) for message in thread.get("messages", [])]
    messages = [
        GmailThreadMessage(
            gmail_id=item.gmail_id,
            from_email=item.from_email,
            from_name=item.from_name,
            date=item.received_at,
            body=item.body_text[:THREAD_BODY_LIMIT],
        )
        for item in parsed[-MAX_THREAD_MESSAGES:]
    ]
    return GmailThread(
        thread_id=thread_id, subject=parsed[0].subject if parsed else "", messages=messages
    )
