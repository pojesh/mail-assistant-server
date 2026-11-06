"""Turn a Gmail API message (format=full) into a clean ParsedEmail.

- Body: prefer text/plain, fall back to HTML converted to text; quoted replies and
  signatures are stripped.
- Attachments: metadata only (never downloaded).
- Invites: a `text/calendar` part is parsed into InviteInfo.
"""

import base64
import html
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any

from icalendar import Calendar

from app.google.text import html_to_text, strip_quoted_text
from app.schemas.google import AttachmentInfo, InviteInfo, ParsedEmail

BODY_LIMIT = 20_000  # characters stored in emails.body_text
CHARSET_PATTERN = re.compile(r'charset="?([\w.-]+)"?', re.IGNORECASE)


def decode_header_value(value: str) -> str:
    """Decode RFC 2047 encoded words (=?UTF-8?...?=) into plain text."""
    try:
        return str(make_header(decode_header(value))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return value.strip()


def _headers(part: dict[str, Any]) -> dict[str, str]:
    """Header name (lower-case) -> first value."""
    result: dict[str, str] = {}
    for header in part.get("headers", []):
        result.setdefault(header["name"].lower(), header["value"])
    return result


def _walk(part: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield this MIME part and all nested parts, depth first."""
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk(child)


def _decode_data(part: dict[str, Any]) -> str:
    """Decode a part's base64url body using its declared charset."""
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    match = CHARSET_PATTERN.search(_headers(part).get("content-type", ""))
    charset = match.group(1) if match else "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:  # unknown charset name
        return raw.decode("utf-8", errors="replace")


def _is_attachment(part: dict[str, Any]) -> bool:
    """A real attachment has a filename and an attachment id; inline images are skipped."""
    if not part.get("filename") or not part.get("body", {}).get("attachmentId"):
        return False
    disposition = _headers(part).get("content-disposition", "").lower()
    return not (disposition.startswith("inline") and part.get("mimeType", "").startswith("image/"))


def _ical_time(value: Any) -> str | None:
    """iCalendar date/datetime -> ISO 8601 string."""
    return value.isoformat() if hasattr(value, "isoformat") else None


def parse_invite(ics_text: str) -> InviteInfo | None:
    """Read UID, METHOD, DTSTART, DTEND, ORGANIZER, SUMMARY from an iCalendar payload."""
    try:
        calendar = Calendar.from_ical(ics_text)
        event = next(iter(calendar.walk("VEVENT")), None)
        if event is None:
            return None
        organizer = str(event.get("ORGANIZER", "")).removeprefix("mailto:").removeprefix("MAILTO:")
        return InviteInfo(
            uid=str(event.get("UID")) if event.get("UID") else None,
            method=str(calendar.get("METHOD")) if calendar.get("METHOD") else None,
            start=_ical_time(event.decoded("DTSTART")) if event.get("DTSTART") else None,
            end=_ical_time(event.decoded("DTEND")) if event.get("DTEND") else None,
            organizer=organizer or None,
            summary=str(event.get("SUMMARY")) if event.get("SUMMARY") else None,
        )
    except (ValueError, KeyError, IndexError, TypeError):
        return None  # a broken invite should never break the whole sync


def _received_at(headers: dict[str, str], message: dict[str, Any]) -> datetime:
    """Gmail's internalDate (ms since epoch) is reliable; the Date header is a fallback."""
    internal = message.get("internalDate")
    if internal:
        return datetime.fromtimestamp(int(internal) / 1000, tz=UTC)
    try:
        parsed = parsedate_to_datetime(headers.get("date", ""))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _addresses(value: str) -> list[str]:
    """'A <a@x.com>, b@y.com' -> ['a@x.com', 'b@y.com']"""
    return [email.lower() for _, email in getaddresses([value]) if email]


def _collect_parts(
    payload: dict[str, Any],
) -> tuple[str, str, list[AttachmentInfo], InviteInfo | None]:
    """Find the plain-text body, HTML body, attachments and invite in one pass."""
    plain, html_body, attachments, invite = "", "", [], None
    for part in _walk(payload):
        mime_type = part.get("mimeType", "")
        if _is_attachment(part):
            attachments.append(
                AttachmentInfo(
                    filename=part["filename"],
                    mime_type=mime_type or "application/octet-stream",
                    size_bytes=int(part.get("body", {}).get("size", 0)),
                    attachment_id=part["body"]["attachmentId"],
                )
            )
        elif mime_type == "text/plain" and not plain:
            plain = _decode_data(part)
        elif mime_type == "text/html" and not html_body:
            html_body = _decode_data(part)
        elif mime_type == "text/calendar" and invite is None:
            # NOTE: only inline calendar parts are read; Google and Outlook invites include one.
            invite = parse_invite(_decode_data(part))
    return plain, html_body, attachments, invite


def parse_message(message: dict[str, Any]) -> ParsedEmail:
    """Convert a Gmail `users.messages.get(format=full)` response into a ParsedEmail."""
    payload = message.get("payload", {})
    headers = _headers(payload)
    from_name, from_email = parseaddr(decode_header_value(headers.get("from", "")))
    plain, html_body, attachments, invite = _collect_parts(payload)

    body = plain if plain.strip() else html_to_text(html_body)
    return ParsedEmail(
        gmail_id=message["id"],
        thread_id=message.get("threadId", message["id"]),
        label_ids=message.get("labelIds", []),
        from_email=from_email.lower() or "unknown@unknown",
        from_name=from_name or None,
        to=_addresses(headers.get("to", "")),
        cc=_addresses(headers.get("cc", "")),
        subject=decode_header_value(headers.get("subject", "")),
        snippet=html.unescape(message.get("snippet", "")),
        body_text=strip_quoted_text(body)[:BODY_LIMIT],
        body_html=html_body or None,
        received_at=_received_at(headers, message),
        message_id_header=headers.get("message-id"),
        in_reply_to=headers.get("in-reply-to"),
        references_header=headers.get("references"),
        attachments=attachments,
        invite=invite,
    )
