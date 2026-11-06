"""Tool sets per agent. Every tool is read-only: no tool can send, draft, label or book.

Write functions live in app/google/gmail_writes.py and the write half of calendar_api.py,
and are called only by graph execution nodes after the approval/autonomy gate.
"""

from langchain_core.tools import BaseTool

from app.tools.calendar_tools import (
    check_availability,
    find_free_slots,
    find_invite_event,
    get_meeting_preferences,
    list_events,
)
from app.tools.flow_tools import start_draft_flow, start_schedule_flow
from app.tools.gmail_tools import get_thread, search_threads
from app.tools.search_tools import search_attachments, search_my_sent_mail

SCHEDULING_TOOLS: list[BaseTool] = [
    get_meeting_preferences,
    check_availability,
    find_free_slots,
    find_invite_event,
    list_events,
]

CHAT_TOOLS: list[BaseTool] = [
    search_threads,
    get_thread,
    search_attachments,
    search_my_sent_mail,
    list_events,
    find_free_slots,
    start_draft_flow,  # hands off to the email graph (approvals); writes nothing itself
    start_schedule_flow,
]

DIGEST_TOOLS: list[BaseTool] = [search_threads, list_events]
