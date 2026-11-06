"""Approvals queue: drafts, new events and invite responses waiting for the user."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.schemas.approvals import ApprovalDecision, ApprovalList, ApprovalStatus, DecideResponse
from app.security.session import current_user
from app.services import approvals, decisions

router = APIRouter(tags=["approvals"])


@router.get("/approvals", response_model=ApprovalList)
async def list_approvals(
    status: ApprovalStatus = "pending",
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ApprovalList:
    """Approvals with the given status, newest first."""
    return ApprovalList(items=await approvals.list_approvals(db, user.id, status))


@router.post("/approvals/{approval_id}/decide", response_model=DecideResponse)
async def decide_approval(
    approval_id: uuid.UUID,
    decision: ApprovalDecision,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> DecideResponse:
    """Approve (saves the Gmail draft / books or answers the event) or reject."""
    return await decisions.decide(db, user.id, approval_id, decision)
