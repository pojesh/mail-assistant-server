"""Verify that /internal/* calls come from our Pub/Sub subscription or Cloud Scheduler.

Both send a Google-signed OIDC ID token for the `internal-invoker` service account, with
the api service URL as audience. Anything else gets 401.
"""

import asyncio
from functools import lru_cache
from typing import Any

import requests
from fastapi import Request
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from app.config import settings
from app.errors import Unauthorized
from app.logging import get_logger

log = get_logger(__name__)


@lru_cache
def _google_request() -> GoogleRequest:
    """Reused HTTP session for fetching Google's public signing keys."""
    return GoogleRequest(session=requests.Session())


def _verify(token: str) -> dict[str, Any]:
    claims: dict[str, Any] = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
        token, _google_request(), audience=settings.internal_audience
    )
    return claims


def is_allowed_caller(claims: dict[str, Any]) -> bool:
    """Only the internal-invoker service account, with a verified email."""
    return bool(
        settings.internal_invoker_sa
        and claims.get("email") == settings.internal_invoker_sa
        and claims.get("email_verified") is True
    )


async def require_internal_caller(request: Request) -> None:
    """Dependency for /internal/*: a valid Google OIDC token from our service account."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or not settings.internal_audience:
        raise Unauthorized("Missing or invalid internal credentials.")
    try:
        claims = await asyncio.to_thread(_verify, token)
    except ValueError as error:  # bad signature, wrong audience, expired
        log.warning("internal_token_rejected", reason=str(error)[:120])
        raise Unauthorized("Missing or invalid internal credentials.") from error
    if not is_allowed_caller(claims):
        log.warning("internal_caller_not_allowed")
        raise Unauthorized("Missing or invalid internal credentials.")
