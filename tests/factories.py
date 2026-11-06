"""Builders for test data: Gmail API messages and parsed emails."""

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

from app.schemas.google import AttachmentInfo, ParsedEmail

BASE_TIME = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def b64(text: str) -> str:
    """Encode text the way the Gmail API does (base64url, no padding)."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_message(
    message_id: str = "m1",
    *,
    headers: dict[str, str] | None = None,
    parts: list[dict[str, Any]] | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """A minimal `users.messages.get(format=full)` response."""
    default_headers = {
        "From": "Ravi Kumar <Ravi@Example.com>",
        "To": "me@example.com",
        "Subject": "Invoice for October",
        "Message-ID": f"<{message_id}@example.com>",
    }
    all_headers = {**default_headers, **(headers or {})}
    return {
        "id": message_id,
        "threadId": f"t-{message_id}",
        "labelIds": labels if labels is not None else ["INBOX", "UNREAD"],
        "snippet": "Please find the invoice &amp; details",
        "internalDate": str(int(BASE_TIME.timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": name, "value": value} for name, value in all_headers.items()],
            "parts": parts if parts is not None else [text_part("Hello, please see attached.")],
        },
    }


def text_part(text: str, mime_type: str = "text/plain", charset: str = "utf-8") -> dict[str, Any]:
    return {
        "mimeType": mime_type,
        "headers": [{"name": "Content-Type", "value": f'{mime_type}; charset="{charset}"'}],
        "body": {"size": len(text), "data": b64(text)},
    }


def attachment_part(
    filename: str, mime_type: str = "application/pdf", inline: bool = False
) -> dict[str, Any]:
    disposition = "inline" if inline else "attachment"
    return {
        "mimeType": mime_type,
        "filename": filename,
        "headers": [
            {"name": "Content-Disposition", "value": f'{disposition}; filename="{filename}"'}
        ],
        "body": {"size": 2048, "attachmentId": f"att-{filename}"},
    }


def parsed_email(
    gmail_id: str,
    *,
    minutes: int = 0,
    labels: list[str] | None = None,
    subject: str = "Hello",
    from_email: str = "ravi@example.com",
    thread_id: str | None = None,
    attachments: list[AttachmentInfo] | None = None,
) -> ParsedEmail:
    """A ParsedEmail as returned by gmail_api.get_message."""
    return ParsedEmail(
        gmail_id=gmail_id,
        thread_id=thread_id or f"t-{gmail_id}",
        label_ids=labels if labels is not None else ["INBOX", "UNREAD"],
        from_email=from_email,
        from_name="Ravi",
        to=["me@example.com"],
        subject=subject,
        snippet=f"snippet {gmail_id}",
        body_text=f"body {gmail_id}",
        received_at=BASE_TIME + timedelta(minutes=minutes),
        attachments=attachments or [],
    )
