"""Inbox list, email detail and "Sync now"."""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.schemas.emails import EmailDetail, EmailListResponse, FlowStartResponse, SyncResponse
from app.schemas.llm import Category, Sentiment
from app.security.rate_limit import FLOW_LIMIT, limit
from app.security.session import current_user
from app.services import email_views, flows, sync
from app.services.emails import EmailFilters
from app.services.emails import list_emails as query_emails

router = APIRouter(tags=["emails"])


@router.get("/emails", response_model=EmailListResponse)
async def list_emails(
    category: Category | None = None,
    priority: int | None = Query(None, ge=1, le=5, description="Minimum priority"),
    sentiment: Sentiment | None = None,
    q: str | None = Query(None, max_length=200),
    unread: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> EmailListResponse:
    """Received emails with their AI insights, newest first."""
    filters = EmailFilters(
        category=category, min_priority=priority, sentiment=sentiment, query=q, unread=unread
    )
    rows, next_cursor = await query_emails(db, user.id, filters, cursor, limit)
    items = [
        email_views.to_list_item(email, insight, has_files) for email, insight, has_files in rows
    ]
    return EmailListResponse(items=items, next_cursor=next_cursor)


@router.get("/emails/{email_id}", response_model=EmailDetail)
async def get_email(
    email_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> EmailDetail:
    """One email with insights, attachments and the rest of its thread."""
    return await email_views.get_email_detail(db, user.id, email_id)


@router.post("/emails/{email_id}/draft", response_model=FlowStartResponse)
@limit(FLOW_LIMIT)
async def draft_reply(
    request: Request,
    email_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> FlowStartResponse:
    """Draft reply variants now (they go to Approvals; nothing is sent)."""
    return await flows.start_flow(db, user.id, email_id, "draft")


@router.post("/emails/{email_id}/schedule", response_model=FlowStartResponse)
@limit(FLOW_LIMIT)
async def find_a_time(
    request: Request,
    email_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> FlowStartResponse:
    """Run the scheduling assistant for this email (same rules and auto-scheduling policy)."""
    return await flows.start_flow(db, user.id, email_id, "schedule")


@router.post("/sync", response_model=SyncResponse)
async def sync_now(user: User = Depends(current_user)) -> SyncResponse:
    """Fetch new mail now. Runs exactly the same code as the Gmail push handler."""
    result = await sync.sync_user(user.id)
    return SyncResponse(
        status=result.status, processed=result.processed, remaining=result.remaining
    )
