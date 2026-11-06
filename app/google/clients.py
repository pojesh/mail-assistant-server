"""Per-user Gmail / Calendar API clients and `run_google`, the single way to execute a request.

Clients are built per call with that user's own token and are never shared between users.
"""

import asyncio
import uuid
from typing import Any

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.errors import GoogleApiError, NeedsReconnect, NotFound
from app.google.oauth import get_valid_access_token
from app.logging import get_logger

log = get_logger(__name__)

HTTP_TIMEOUT_SECONDS = 30
RETRY_STATUSES = {429, 500, 502, 503, 504}
RATE_LIMIT_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")
MAX_RETRIES = 2


def _authorized_http(token: str) -> AuthorizedHttp:
    """HTTP transport with the user's token and a timeout (httplib2 has none by default)."""
    return AuthorizedHttp(Credentials(token), http=httplib2.Http(timeout=HTTP_TIMEOUT_SECONDS))


async def get_gmail(user_id: uuid.UUID) -> Any:
    """Build a Gmail API client authorised with the user's own token."""
    token = await get_valid_access_token(user_id)
    return build("gmail", "v1", http=_authorized_http(token), cache_discovery=False)


async def get_calendar(user_id: uuid.UUID) -> Any:
    """Build a Calendar API client authorised with the user's own token."""
    token = await get_valid_access_token(user_id)
    return build("calendar", "v3", http=_authorized_http(token), cache_discovery=False)


def _is_retryable(error: HttpError) -> bool:
    """429/5xx, and Gmail's 403 rate-limit responses, are worth retrying."""
    status = int(error.resp.status)
    if status in RETRY_STATUSES:
        return True
    return status == 403 and any(reason in str(error.content) for reason in RATE_LIMIT_REASONS)


def _raise_mapped(error: HttpError) -> None:
    """Turn a final Google error into one of our own errors."""
    status = int(error.resp.status)
    if status == 401:
        raise NeedsReconnect() from error
    if status in (404, 410):
        raise NotFound("That item no longer exists in Google.") from error
    log.warning("google_api_error", status=status)
    raise GoogleApiError() from error


async def run_google(request: Any) -> Any:
    """Execute a googleapiclient request off the event loop, retrying 429/5xx twice."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await asyncio.to_thread(request.execute)
        except HttpError as error:
            if attempt < MAX_RETRIES and _is_retryable(error):
                await asyncio.sleep(2**attempt)
                continue
            _raise_mapped(error)
        except RefreshError as error:
            raise NeedsReconnect() from error
        except (TransportError, TimeoutError, OSError) as error:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2**attempt)
                continue
            log.warning("google_network_error", error_type=type(error).__name__)
            raise GoogleApiError() from error
    raise GoogleApiError()  # unreachable; keeps type checkers happy
