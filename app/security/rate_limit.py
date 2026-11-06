"""Per-user rate limits (slowapi) for the expensive routes: chat and draft/schedule triggers.

Limits are per Cloud Run instance (in memory), which is enough to stop runaway clients and
protect the free LLM quota.
"""

from collections.abc import Callable
from typing import Any, TypeVar, cast

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.errors import error_json
from app.security.session import SESSION_COOKIE, read_signed_token

CHAT_LIMIT = "20/minute"
FLOW_LIMIT = "10/minute"

RouteFunction = TypeVar("RouteFunction", bound=Callable[..., Any])


def user_key(request: Request) -> str:
    """Limit signed-in users by user id, everyone else by IP address."""
    claims = read_signed_token(request.cookies.get(SESSION_COOKIE, ""))
    if claims and claims.get("sub"):
        return f"user:{claims['sub']}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=user_key)


def limit(rule: str) -> Callable[[RouteFunction], RouteFunction]:
    """Typed wrapper around `limiter.limit` (slowapi has no type hints).

    The decorated route must take a `request: Request` parameter.
    """
    return cast(Callable[[RouteFunction], RouteFunction], limiter.limit(rule))


async def handle_rate_limit(request: Request, error: Exception) -> JSONResponse:
    """429 in the standard error format."""
    assert isinstance(error, RateLimitExceeded)
    return error_json(429, "rate_limited", "Too many requests. Please wait a minute and try again.")
