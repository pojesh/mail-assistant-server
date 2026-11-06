"""Health check used by Cloud Run and the web app."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


@router.get("/health", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    """Return ok when the process is up (no external calls, so it stays fast)."""
    return HealthResponse(status="ok")
