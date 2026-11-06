"""Gmail write operations: AI labels and reply drafts. There is NO send function.

Only deterministic graph nodes call these, after the approval/autonomy gate.
Never wrap them as LLM tools.
"""

import base64
import uuid
from email.message import EmailMessage

from app.google.clients import get_gmail, run_google

# --- Labels -------------------------------------------------------------------------------

CATEGORY_LABELS = {
    "action_required": "AI/Action",
    "fyi": "AI/FYI",
    "meeting_request": "AI/Meeting",
    "newsletter": "AI/Newsletter",
    "promotion": "AI/Promotion",
    "other": "AI/Other",
}
SUSPICIOUS_LABEL = "AI/Suspicious"
ALL_AI_LABELS = [*CATEGORY_LABELS.values(), SUSPICIOUS_LABEL]


async def ensure_labels(user_id: uuid.UUID) -> dict[str, str]:
    """Create any missing AI/* labels. Returns label name -> Gmail label id."""
    gmail = await get_gmail(user_id)
    response = await run_google(gmail.users().labels().list(userId="me"))
    existing = {label["name"]: label["id"] for label in response.get("labels", [])}
    for name in ALL_AI_LABELS:
        if name in existing:
            continue
        body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
        created = await run_google(gmail.users().labels().create(userId="me", body=body))
        existing[name] = created["id"]
    return {name: existing[name] for name in ALL_AI_LABELS}


async def apply_labels(
    user_id: uuid.UUID, message_id: str, add: list[str], remove: list[str]
) -> None:
    """Add/remove labels (by name) on a message. Only the labeling graph node calls this."""
    label_ids = await ensure_labels(user_id)
    body = {
        "addLabelIds": [label_ids[name] for name in add if name in label_ids],
        "removeLabelIds": [label_ids[name] for name in remove if name in label_ids],
    }
    gmail = await get_gmail(user_id)
    await run_google(gmail.users().messages().modify(userId="me", id=message_id, body=body))


# --- Drafts (never sent) -------------------------------------------------------------------


def build_reply_message(
    to: str, subject: str, body: str, in_reply_to: str | None, references: str | None
) -> str:
    """RFC 2822 reply, base64url-encoded as the Gmail API expects."""
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


async def create_draft(
    user_id: uuid.UUID,
    thread_id: str,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None,
    references: str | None = None,
) -> str:
    """Save a reply as a Gmail draft in the same thread. Returns the draft id. Never sends."""
    raw = build_reply_message(to, subject, body, in_reply_to, references)
    gmail = await get_gmail(user_id)
    request = (
        gmail.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": raw, "threadId": thread_id}})
    )
    response = await run_google(request)
    return str(response["id"])
