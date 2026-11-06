"""Sign in with Google, sign out, and the current user."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.errors import AppError
from app.google import oauth
from app.logging import get_logger
from app.models import SyncState, User
from app.schemas.common import MeResponse, OkResponse
from app.security.session import (
    clear_session_cookies,
    create_signed_token,
    current_user,
    read_signed_token,
    set_session_cookies,
)
from app.services import accounts, sync

log = get_logger(__name__)
router = APIRouter(tags=["auth"])

STATE_COOKIE = "oauth_state"
STATE_LIFETIME = timedelta(minutes=10)


def _redirect_to_app(path: str) -> RedirectResponse:
    """Redirect the browser to a page of the web app."""
    return RedirectResponse(settings.app_base_url.rstrip("/") + path, status_code=303)


def _sign_in_error(code: str) -> RedirectResponse:
    """Send the user back to the landing page with an error code the UI can explain."""
    response = _redirect_to_app(f"/?auth_error={code}")
    response.delete_cookie(STATE_COOKIE, path="/api/auth")
    return response


@router.get("/auth/google/login", response_class=RedirectResponse, status_code=303)
async def google_login() -> RedirectResponse:
    """Start Google sign-in. State and PKCE verifier are kept in a short-lived signed cookie."""
    consent = oauth.create_consent_url()
    cookie_value = create_signed_token(
        {"state": consent.state, "verifier": consent.code_verifier}, STATE_LIFETIME
    )
    response = RedirectResponse(consent.url, status_code=303)
    response.set_cookie(
        STATE_COOKIE,
        cookie_value,
        max_age=int(STATE_LIFETIME.total_seconds()),
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/api/auth",
    )
    return response


@router.get("/auth/google/callback", response_class=RedirectResponse, status_code=303)
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Finish Google sign-in, store tokens, start the Gmail watch, and open the app."""
    if error:  # e.g. the user pressed "Cancel" on the consent screen
        return _sign_in_error("access_denied")

    saved = read_signed_token(request.cookies.get(STATE_COOKIE, ""))
    if not code or not state or not saved or saved.get("state") != state:
        return _sign_in_error("invalid_state")

    try:
        tokens = await oauth.exchange_code(code, saved["verifier"])
    except AppError:
        return _sign_in_error("sign_in_failed")
    if oauth.missing_scopes(tokens.scopes):
        return _sign_in_error("missing_permissions")

    user = await accounts.sign_in_user(db, tokens)
    await sync.prepare_mailbox(user.id)
    log.info("user_signed_in", user_id=str(user.id))

    response = _redirect_to_app("/inbox" if user.onboarded else "/onboarding")
    response.delete_cookie(STATE_COOKIE, path="/api/auth")
    set_session_cookies(response, user.id)
    return response


@router.post("/auth/logout", response_model=OkResponse)
async def logout(response: Response) -> OkResponse:
    """Clear the session cookies. (Google access stays connected; see /account/disconnect.)"""
    clear_session_cookies(response)
    return OkResponse()


def me_response(user: User, last_synced_at: datetime | None = None) -> MeResponse:
    """The signed-in user as returned by /api/me."""
    return MeResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        picture_url=user.picture_url,
        status="needs_reconnect" if user.status == "needs_reconnect" else "active",
        onboarded=user.onboarded,
        llm_provider=settings.llm_provider,
        last_synced_at=last_synced_at,
    )


@router.get("/me", response_model=MeResponse)
async def get_me(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> MeResponse:
    """The signed-in user, their Google connection status and when mail was last synced."""
    last_synced_at = await db.scalar(
        select(SyncState.last_synced_at).where(SyncState.user_id == user.id)
    )
    return me_response(user, last_synced_at)
