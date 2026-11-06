"""Approvals rows: create, list, claim for a decision, and record the outcome."""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import open_session
from app.errors import Conflict, NotFound
from app.logging import get_logger
from app.models import Approval
from app.schemas.approvals import ApprovalDecision, ApprovalOut, ApprovalPayload, ApprovalStatus

log = get_logger(__name__)


def to_out(row: Approval) -> ApprovalOut:
    """Database row -> API model."""
    try:
        payload = ApprovalPayload.model_validate(row.payload)
    except ValidationError:
        log.warning("approval_payload_invalid", approval_id=str(row.id))
        payload = ApprovalPayload.model_validate(
            {"email": {"subject": "", "from_email": "", "from_name": None, "received_at": ""}}
        )
    return ApprovalOut(
        id=row.id,
        kind=row.kind,
        status=row.status,
        email_id=row.email_id,
        payload=payload,
        warnings=[str(warning) for warning in row.warnings],
        result=row.result,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


async def create_approval(
    user_id: uuid.UUID,
    email_id: uuid.UUID | None,
    kind: str,
    graph_thread_id: str,
    payload: ApprovalPayload,
    warnings: list[str],
) -> uuid.UUID:
    """Queue an item for the user; any older pending item for the same email becomes stale."""
    async with open_session() as db:
        if email_id is not None:
            await db.execute(
                update(Approval)
                .where(Approval.email_id == email_id, Approval.status == "pending")
                .values(status="stale")
            )
        row = Approval(
            user_id=user_id,
            email_id=email_id,
            kind=kind,
            thread_id=graph_thread_id,
            payload=payload.model_dump(mode="json"),
            warnings=warnings,
        )
        db.add(row)
        await db.commit()
        return row.id


async def list_approvals(
    db: AsyncSession, user_id: uuid.UUID, status: ApprovalStatus
) -> list[ApprovalOut]:
    rows = await db.scalars(
        select(Approval)
        .where(Approval.user_id == user_id, Approval.status == status)
        .order_by(Approval.created_at.desc())
        .limit(100)
    )
    return [to_out(row) for row in rows]


async def get_approval(db: AsyncSession, user_id: uuid.UUID, approval_id: uuid.UUID) -> Approval:
    row = await db.scalar(
        select(Approval).where(Approval.id == approval_id, Approval.user_id == user_id)
    )
    if row is None:
        raise NotFound("Approval not found.")
    return row


async def claim_for_decision(
    db: AsyncSession, user_id: uuid.UUID, approval_id: uuid.UUID, decision: ApprovalDecision
) -> Approval:
    """Atomically move pending -> approved/rejected, so a double click cannot run it twice."""
    row = await get_approval(db, user_id, approval_id)
    new_status = "approved" if decision.action == "approve" else "rejected"
    result = await db.execute(
        update(Approval)
        .where(Approval.id == approval_id, Approval.status == "pending")
        .values(status=new_status, decided_at=datetime.now(UTC))
    )
    await db.commit()
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise Conflict(f"This item is already {row.status}.")
    await db.refresh(row)
    return row


async def record_outcome(
    approval_id: uuid.UUID, status: ApprovalStatus, result: dict[str, Any] | None
) -> None:
    """Store what happened after the decision (execution result, error, or staleness)."""
    values: dict[str, Any] = {"status": status, "result": result}
    if status == "pending":
        values["decided_at"] = None
    async with open_session() as db:
        await db.execute(update(Approval).where(Approval.id == approval_id).values(**values))
        await db.commit()
