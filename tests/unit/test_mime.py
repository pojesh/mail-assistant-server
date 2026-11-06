"""MIME parsing: headers, bodies, attachments, invites, quote and signature stripping."""

import base64

from app.google.mime import BODY_LIMIT, decode_header_value, parse_invite, parse_message
from app.google.text import html_to_text, strip_quoted_text
from tests.factories import attachment_part, gmail_message, text_part

INVITE_ICS = """BEGIN:VCALENDAR
METHOD:REQUEST
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
BEGIN:VEVENT
DTSTART:20261110T043000Z
DTEND:20261110T050000Z
UID:abc123@google.com
ORGANIZER;CN=Priya:mailto:priya@example.com
SUMMARY:Project sync
END:VEVENT
END:VCALENDAR
"""


def test_parses_headers_and_plain_body() -> None:
    parsed = parse_message(gmail_message())
    assert parsed.from_email == "ravi@example.com"  # lower-cased
    assert parsed.from_name == "Ravi Kumar"
    assert parsed.to == ["me@example.com"]
    assert parsed.subject == "Invoice for October"
    assert parsed.body_text == "Hello, please see attached."
    assert parsed.snippet == "Please find the invoice & details"  # HTML entities decoded
    assert parsed.message_id_header == "<m1@example.com>"
    assert parsed.received_at.tzinfo is not None


def test_decodes_encoded_word_headers() -> None:
    assert decode_header_value("=?UTF-8?B?w4lxdWlwZQ==?=") == "Équipe"
    assert decode_header_value("Plain subject") == "Plain subject"


def test_falls_back_to_html_when_no_plain_text() -> None:
    html = (
        "<html><head><style>p{}</style></head>"
        "<body><p>Hi <b>there</b></p><script>x()</script></body></html>"
    )
    parsed = parse_message(gmail_message(parts=[text_part(html, "text/html")]))
    assert "Hi" in parsed.body_text and "there" in parsed.body_text
    assert "x()" not in parsed.body_text and "p{}" not in parsed.body_text
    assert parsed.body_html == html


def test_collects_attachment_metadata_but_skips_inline_images() -> None:
    parts = [
        text_part("See attached"),
        attachment_part("invoice.pdf"),
        attachment_part("logo.png", "image/png", inline=True),
    ]
    parsed = parse_message(gmail_message(parts=parts))
    assert [item.filename for item in parsed.attachments] == ["invoice.pdf"]
    assert parsed.attachments[0].mime_type == "application/pdf"
    assert parsed.attachments[0].attachment_id == "att-invoice.pdf"


def test_detects_calendar_invite() -> None:
    parts = [text_part("You are invited"), text_part(INVITE_ICS, "text/calendar")]
    parsed = parse_message(gmail_message(parts=parts))
    assert parsed.invite is not None
    assert parsed.invite.uid == "abc123@google.com"
    assert parsed.invite.method == "REQUEST"
    assert parsed.invite.organizer == "priya@example.com"
    assert parsed.invite.summary == "Project sync"
    assert parsed.invite.start == "2026-11-10T04:30:00+00:00"


def test_broken_invite_returns_none() -> None:
    assert parse_invite("this is not a calendar") is None


def test_handles_non_utf8_charset() -> None:
    part = text_part("Café", charset="latin-1")
    part["body"]["data"] = base64.urlsafe_b64encode("Café".encode("latin-1")).decode()
    parsed = parse_message(gmail_message(parts=[part]))
    assert parsed.body_text == "Café"


def test_truncates_long_bodies() -> None:
    parsed = parse_message(gmail_message(parts=[text_part("a" * (BODY_LIMIT + 500))]))
    assert len(parsed.body_text) == BODY_LIMIT


def test_strips_gmail_quoted_reply() -> None:
    text = (
        "Sounds good, see you then.\n\n"
        "On Mon, 3 Nov 2026 at 10:00, Ravi <ravi@x.com> wrote:\n"
        "> Can we meet?\n"
    )
    assert strip_quoted_text(text) == "Sounds good, see you then."


def test_strips_wrapped_quote_header_and_signature() -> None:
    text = "Thanks!\n-- \nRavi Kumar\nCEO\n"
    assert strip_quoted_text(text) == "Thanks!"
    wrapped = "Yes.\nOn Mon, 3 Nov 2026 at 10:00, Ravi Kumar\n<ravi@x.com> wrote:\nold text"
    assert strip_quoted_text(wrapped) == "Yes."


def test_strips_outlook_original_message() -> None:
    text = "Approved.\n\n-----Original Message-----\nFrom: A\nSent: today\nold"
    assert strip_quoted_text(text) == "Approved."


def test_keeps_normal_text_that_starts_with_on() -> None:
    text = "On Monday we launch.\nPlease review the plan."
    assert strip_quoted_text(text) == text


def test_html_to_text_empty() -> None:
    assert html_to_text("") == ""
