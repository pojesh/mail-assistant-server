"""Internal routes, called only by Pub/Sub (Gmail push) and Cloud Scheduler.

Every route requires a Google-signed OIDC token for the internal-invoker service account.
They are left out of the public OpenAPI schema.
"""

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from app.security.oidc import require_internal_caller
from app.services import jobs

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(require_internal_caller)],
)


class PubSubMessage(BaseModel):
    data: str
    messageId: str | None = None  # noqa: N815 (Pub/Sub's field name)


class PubSubPush(BaseModel):
    message: PubSubMessage
    subscription: str | None = None


class PushResult(BaseModel):
    status: str


@router.post("/gmail-push", response_model=PushResult)
async def gmail_push(body: PubSubPush, response: Response) -> PushResult:
    """New mail. Returns 503 if this mailbox is already syncing, so Pub/Sub redelivers later."""
    status = await jobs.handle_push(body.message.data)
    if status == "busy":
        response.status_code = 503
    return PushResult(status=status)


@router.post("/sync-sweep", response_model=jobs.SweepResult)
async def sync_sweep() -> jobs.SweepResult:
    """Every 10 minutes: catch-up sync for idle mailboxes, then digests that are due."""
    return await jobs.run_sweep()


@router.post("/daily-maintenance", response_model=jobs.MaintenanceResult)
async def daily_maintenance() -> jobs.MaintenanceResult:
    """Daily: renew Gmail watches, expire old approvals, apply the 30-day body retention."""
    return await jobs.run_maintenance()
