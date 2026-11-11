"""A synthetic mailbox for the chat and flow scenarios.

- Inbox: the 45 triage emails (tests/evals/data/triage_cases.json), spread over ~3 weeks.
- Sent: the user's replies to some of them, in the same threads (so "what did I tell X" works).
- Attachments on a few emails, and a small calendar for the coming days.
- FakeGmail: in-memory stand-ins for gmail_api.search_threads / get_thread. The search
  understands the common Gmail operators (from:, to:, subject:, has:attachment, filename:,
  newer_than:, older_than:, after:, before:, in:, is:, quoted phrases, -negation, OR, groups);
  plain words must all appear (case-insensitive substring match).

Every stored email maps back to its case id ("act-01"...), which is how retrieval is scored.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parseaddr
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.db import open_session
from app.errors import NotFound
from app.google import calendar_api, gmail_api
from app.models import Email, OAuthToken, Preferences, SyncState, User
from app.schemas.calendar import CalendarEvent
from app.schemas.google import (
    AttachmentInfo,
    GmailThread,
    GmailThreadMessage,
    GmailThreadSummary,
    ParsedEmail,
)
from app.schemas.preferences import MeetingPreferences, PreferencesBody
from app.security.crypto import encrypt
from app.services.emails import save_email
from app.services.embeddings import embed_sent_emails
from app.services.preferences import save_preferences
from tests.evals.dates import iso_at

TIMEZONE = "Asia/Kolkata"
USER_EMAIL = "pat@example.com"
USER_NAME = "Pat Lee"

# (reply to case, hours after the original, body). Facts here are what chat questions check.
SENT_REPLIES = [
    (
        "act-02",
        3,
        "Hi Ravi,\n\nApologies for the delay. The corrected invoice (INV-2291) for Rs 4,80,000 "
        "went out today; the earlier one double-counted the support fee.\n\nBest,\nPat",
    ),
    (
        "act-04",
        2,
        "Hi Arun,\n\nThe export endpoint allows 600 requests per minute per API key. For your "
        "bulk migration we can raise it to 2,000 per minute for a week; just tell me the "
        "dates.\n\nBest,\nPat",
    ),
    (
        "act-06",
        1,
        "Hi Meera,\n\nApproved your leave for 12-14 December in the HR portal. "
        "Enjoy the break!\n\nPat",
    ),
    (
        "act-07",
        5,
        "Hi Karthik,\n\nWe need 180 licenses, not 200. Billing address: Acme Labs, 4th floor, "
        "21 MG Road, Bengaluru 560001.\n\nBest,\nPat",
    ),
    (
        "act-08",
        4,
        "Hi Neha,\n\nI am sorry about the bugs. We found the root cause, a caching bug in the sync "
        "service, and will ship a fix by Friday with a full written explanation and a test "
        "plan.\n\nBest,\nPat",
    ),
    (
        "mtg-02",
        1,
        "Hi Priya,\n\nTuesday 11:00 works. I have added the security review and the rollback plan "
        "to the launch checklist.\n\nPat",
    ),
    (
        "mtg-04",
        6,
        "Hi,\n\nThanks! I can do Tuesday 11:00, Wednesday 15:00 or Thursday 10:00 IST for the "
        "interview.\n\nBest,\nPat",
    ),
    ("oth-01", 8, "Lovely photos, Mom! Everyone looks so happy. Will call you this weekend."),
]

ATTACHMENTS = {
    "act-01": [("Q3-report.pdf", "application/pdf", 248_000)],
    "act-09": [("Invoice-November-2026.pdf", "application/pdf", 88_000)],
    "act-10": [
        (
            "Hiring-plan-2027.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            56_000,
        )
    ],
    "fyi-04": [("HDFC-statement-Oct-2026.pdf", "application/pdf", 132_000)],
    "oth-01": [("wedding-photos.zip", "application/zip", 48_000_000)],
}

# Calendar for the coming days (D1/D2/D3 = the next working days, see tests/evals/dates.py).
CALENDAR = [
    ("D1", "10:00", "11:00", "Design review", "priya@acme.com"),
    ("D1", "15:00", "15:30", "1:1 with Sanjay", "sanjay@acme.com"),
    ("D2", "11:00", "11:30", "Launch checklist sync", "priya@acme.com"),
    ("D2", "14:30", "15:30", "Vendor demo (CloudHost)", "sales@cloudhost.com"),
    ("D3", "10:00", "12:00", "Quarterly planning", "sanjay@acme.com"),
]


def case_id_of(thread_id: str) -> str:
    return thread_id.removeprefix("th-")


@dataclass
class Mailbox:
    messages: list[ParsedEmail]
    events: list[CalendarEvent]
    queries: list[str] = field(default_factory=list)  # every Gmail query the agent ran

    @classmethod
    def build(
        cls, cases: list[dict[str, Any]], days: dict[str, date], now: datetime | None = None
    ) -> "Mailbox":
        now = now or datetime.now(ZoneInfo(TIMEZONE))
        by_id = {case["id"]: case for case in cases}
        received: dict[str, ParsedEmail] = {}
        for index, case in enumerate(cases):
            name, address = parseaddr(case["from"])
            received[case["id"]] = ParsedEmail(
                gmail_id=f"g-{case['id']}",
                thread_id=f"th-{case['id']}",
                label_ids=["INBOX", "UNREAD"] if index % 3 else ["INBOX"],
                from_email=address.lower(),
                from_name=name or None,
                to=[USER_EMAIL],
                subject=case["subject"],
                snippet=" ".join(case["body"].split())[:140],
                body_text=case["body"],
                received_at=now - timedelta(hours=5 + index * 11),
                message_id_header=f"<{case['id']}@mail.example>",
                attachments=[
                    AttachmentInfo(filename=n, mime_type=m, size_bytes=s, attachment_id=f"att-{n}")
                    for n, m, s in ATTACHMENTS.get(case["id"], [])
                ],
            )
        sent = []
        for case_id, hours, body in SENT_REPLIES:
            original = received[case_id]
            sent.append(
                ParsedEmail(
                    gmail_id=f"g-{case_id}-reply",
                    thread_id=original.thread_id,
                    label_ids=["SENT"],
                    from_email=USER_EMAIL,
                    from_name=USER_NAME,
                    to=[original.from_email],
                    subject=f"Re: {by_id[case_id]['subject']}",
                    snippet=" ".join(body.split())[:140],
                    body_text=body,
                    received_at=min(
                        original.received_at + timedelta(hours=hours), now - timedelta(minutes=10)
                    ),
                    in_reply_to=original.message_id_header,
                )
            )
        events = [
            CalendarEvent(
                id=f"cal-{n}",
                summary=title,
                start=iso_at(days, day, start, TIMEZONE),
                end=iso_at(days, day, end, TIMEZONE),
                organizer=organizer,
                attendees=[organizer, USER_EMAIL],
                my_response="accepted",
            )
            for n, (day, start, end, title, organizer) in enumerate(CALENDAR)
        ]
        return cls(messages=[*received.values(), *sent], events=events)

    def only(self, case_ids: set[str]) -> "Mailbox":
        """A smaller mailbox with just these threads (for flow scenarios)."""
        kept = [m for m in self.messages if case_id_of(m.thread_id) in case_ids]
        return Mailbox(messages=kept, events=self.events)

    # --- Gmail fakes ---------------------------------------------------------------------------

    def _threads(self) -> dict[str, list[ParsedEmail]]:
        threads: dict[str, list[ParsedEmail]] = {}
        for message in sorted(self.messages, key=lambda m: m.received_at):
            threads.setdefault(message.thread_id, []).append(message)
        return threads

    async def search_threads(
        self, user_id: uuid.UUID, query: str, limit: int = 10
    ) -> list[GmailThreadSummary]:
        self.queries.append(query)
        matcher = compile_query(query)
        hits = []
        for thread_id, messages in self._threads().items():
            if any(matcher(message) for message in messages):
                last = messages[-1]
                hits.append(
                    GmailThreadSummary(
                        thread_id=thread_id,
                        subject=messages[0].subject,
                        last_from=last.from_name or last.from_email,
                        snippet=last.snippet,
                        last_date=last.received_at,
                        message_count=len(messages),
                    )
                )
        hits.sort(key=lambda hit: hit.last_date or datetime.min.replace(tzinfo=UTC), reverse=True)
        return hits[:limit]

    async def get_thread(self, user_id: uuid.UUID, thread_id: str) -> GmailThread:
        messages = self._threads().get(thread_id)
        if not messages:
            raise NotFound("No conversation with that id.")
        return GmailThread(
            thread_id=thread_id,
            subject=messages[0].subject,
            messages=[
                GmailThreadMessage(
                    gmail_id=m.gmail_id,
                    from_email=m.from_email,
                    from_name=m.from_name,
                    date=m.received_at,
                    body=m.body_text[:2000],
                )
                for m in messages[-8:]
            ],
        )

    # --- Calendar fakes ------------------------------------------------------------------------

    async def list_events(
        self, user_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        return [
            event
            for event in self.events
            if datetime.fromisoformat(event.start) < end
            and datetime.fromisoformat(event.end) > start
        ]

    async def find_event_by_ical_uid(self, user_id: uuid.UUID, uid: str) -> CalendarEvent | None:
        return next((event for event in self.events if event.ical_uid == uid), None)

    def install(self, patch: pytest.MonkeyPatch) -> None:
        patch.setattr(gmail_api, "search_threads", self.search_threads)
        patch.setattr(gmail_api, "get_thread", self.get_thread)
        patch.setattr(calendar_api, "list_events", self.list_events)
        patch.setattr(calendar_api, "find_event_by_ical_uid", self.find_event_by_ical_uid)

    # --- Database ------------------------------------------------------------------------------

    async def seed(self, user_id: uuid.UUID, embed_sent: bool = True) -> dict[str, str]:
        """Store every message; embed the sent ones for search_my_sent_mail (real embeddings).
        Returns stored email id -> case id."""
        email_cases: dict[str, str] = {}
        sent_rows: list[Email] = []
        for parsed in self.messages:
            async with open_session() as db:
                row = await save_email(db, user_id, parsed)
            assert row is not None
            email_cases[str(row.id)] = case_id_of(parsed.thread_id)
            if row.is_sent:
                sent_rows.append(row)
        if embed_sent and sent_rows:
            await embed_sent_emails(user_id, sent_rows)
        return email_cases


# --- Gmail query matching ------------------------------------------------------------------------

_GROUP = re.compile(r"(?<![:\w])\(([^()]*)\)")
_TOKEN = re.compile(r'-?[\w.]+:\([^)]*\)|-?[\w.]+:"[^"]*"|-?"[^"]*"|\S+')


def _searchable(message: ParsedEmail) -> str:
    names = " ".join(a.filename for a in message.attachments)
    return " ".join(
        [
            message.from_name or "",
            message.from_email,
            " ".join(message.to),
            message.subject,
            message.body_text,
            names,
        ]
    ).lower()


def _clean(value: str) -> str:
    return value.strip().strip('"').strip("()").strip().lower()


def _age(value: str) -> timedelta | None:
    match = re.fullmatch(r"(\d+)([dmy])", value.lower())
    if not match:
        return None
    number, unit = int(match.group(1)), match.group(2)
    return timedelta(days=number * {"d": 1, "m": 30, "y": 365}[unit])


def _day(value: str) -> date | None:
    try:
        return datetime.strptime(value.replace("-", "/"), "%Y/%m/%d").date()
    except ValueError:
        return None


def _term(token: str) -> Any:
    """One search term -> predicate(message)."""
    negate = token.startswith("-") and len(token) > 1
    token = token[1:] if negate else token
    if token.startswith("GROUP#"):
        inner = compile_query(token[6:])
        predicate = inner
    elif ":" in token and not token.startswith('"'):
        operator, _, raw = token.partition(":")
        operator, value = operator.lower(), _clean(raw)
        words = value.split()
        if operator == "from":
            predicate = lambda m: value in f"{m.from_name or ''} {m.from_email}".lower()  # noqa: E731
        elif operator in ("to", "cc"):
            predicate = lambda m: value in " ".join(m.to + m.cc).lower()  # noqa: E731
        elif operator == "subject":
            predicate = lambda m: all(w in m.subject.lower() for w in words)  # noqa: E731
        elif operator == "has":
            predicate = lambda m: bool(m.attachments) if value == "attachment" else True  # noqa: E731
        elif operator == "filename":
            predicate = lambda m: any(value in a.filename.lower() for a in m.attachments)  # noqa: E731
        elif operator in ("newer_than", "older_than"):
            span = _age(value)
            cutoff = datetime.now(UTC) - span if span else None
            if cutoff is None:
                predicate = lambda m: True  # noqa: E731
            elif operator == "newer_than":
                predicate = lambda m: m.received_at >= cutoff  # noqa: E731
            else:
                predicate = lambda m: m.received_at < cutoff  # noqa: E731
        elif operator in ("after", "before"):
            day = _day(value)
            if day is None:
                predicate = lambda m: True  # noqa: E731
            elif operator == "after":
                predicate = lambda m: m.received_at.date() >= day  # noqa: E731
            else:
                predicate = lambda m: m.received_at.date() < day  # noqa: E731
        elif operator == "in":
            label = {"inbox": "INBOX", "sent": "SENT"}.get(value)
            predicate = (lambda m: label in m.label_ids) if label else (lambda m: True)
        elif operator == "is":
            if value == "unread":
                predicate = lambda m: "UNREAD" in m.label_ids  # noqa: E731
            elif value == "read":
                predicate = lambda m: "UNREAD" not in m.label_ids  # noqa: E731
            else:
                predicate = lambda m: True  # noqa: E731
        else:  # label:, category:, etc. are not modelled: they do not narrow the search
            predicate = lambda m: True  # noqa: E731
    else:
        phrase = _clean(token)
        predicate = (lambda m: phrase in _searchable(m)) if phrase else (lambda m: True)
    return (lambda m: not predicate(m)) if negate else predicate


def compile_query(query: str) -> Any:
    """Gmail query -> predicate(message). Terms are ANDed; OR binds two neighbouring terms."""
    query = _GROUP.sub(lambda match: "GROUP#" + match.group(1).replace(" ", "\u0000"), query)
    tokens = [token.replace("\u0000", " ") for token in _TOKEN.findall(query)]
    alternatives: list[list[Any]] = []  # AND of (OR-groups)
    pending_or = False
    for token in tokens:
        if token.upper() in ("OR", "|"):
            pending_or = True
            continue
        predicate = _term(token)
        if pending_or and alternatives:
            alternatives[-1].append(predicate)
        else:
            alternatives.append([predicate])
        pending_or = False
    return lambda m: all(any(p(m) for p in group) for group in alternatives)


# --- Users ---------------------------------------------------------------------------------------


async def create_user(
    meeting_preferences: dict[str, Any], vip_senders: list[str] | None = None
) -> User:
    """A signed-up user (Pat) with tokens, IST preferences, a signature and meeting rules."""
    async with open_session() as db:
        user = User(email=USER_EMAIL, name=USER_NAME, onboarded=True)
        db.add(user)
        await db.flush()
        db.add(
            OAuthToken(
                user_id=user.id,
                access_token_enc=encrypt("access-token"),
                refresh_token_enc=encrypt("refresh-token"),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes=[],
            )
        )
        db.add(Preferences(user_id=user.id))
        db.add(SyncState(user_id=user.id))
        await db.commit()
    async with open_session() as db:
        await save_preferences(
            db,
            user.id,
            PreferencesBody(
                signature=USER_NAME,
                sign_off="Best",
                timezone=TIMEZONE,
                vip_senders=vip_senders or [],
                meeting_preferences=MeetingPreferences.model_validate(meeting_preferences),
            ),
        )
    return user
