"""App session cookie (a signed JWT) and the double-submit CSRF check.

- `session` cookie: httpOnly JWT holding the user id, valid for 7 days.
- `csrf_token` cookie: readable by the web app, which echoes it in the `X-CSRF-Token`
  header on every state-changing request. A cross-site page cannot read the cookie,
  so it cannot forge the header.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.errors import Forbidden, Unauthorized
from app.models import User

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
SESSION_DAYS = 7
JWT_ALGORITHM = "HS256"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def create_signed_token(claims: dict[str, str], lifetime: timedelta) -> str:
    """Sign a short JWT with SESSION_SECRET."""
    now = datetime.now(UTC)
    payload = {**claims, "iat": now, "exp": now + lifetime}
    return jwt.encode(payload, settings.session_secret, algorithm=JWT_ALGORITHM)


def read_signed_token(token: str) -> dict[str, str] | None:
    """Return the claims of a valid token, or None if it is missing, forged or expired."""
    try:
        claims: dict[str, str] = jwt.decode(
            token, settings.session_secret, algorithms=[JWT_ALGORITHM]
        )
    except jwt.PyJWTError:
        return None
    return claims


def set_session_cookies(response: Response, user_id: uuid.UUID) -> None:
    """Sign the user in: set the session cookie and a fresh CSRF token cookie."""
    max_age = SESSION_DAYS * 24 * 3600
    token = create_signed_token({"sub": str(user_id)}, timedelta(days=SESSION_DAYS))
    secure = settings.is_production
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=secure, samesite="lax"
    )
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
    )


def clear_session_cookies(response: Response) -> None:
    """Sign the user out."""
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """Dependency: the signed-in user, or 401."""
    token = request.cookies.get(SESSION_COOKIE)
    claims = read_signed_token(token) if token else None
    if not claims or "sub" not in claims:
        raise Unauthorized()
    try:
        user_id = uuid.UUID(claims["sub"])
    except ValueError as error:
        raise Unauthorized() from error
    user = await db.get(User, user_id)
    if user is None:
        raise Unauthorized()
    return user


async def require_csrf(request: Request) -> None:
    """Dependency: state-changing requests must echo the CSRF cookie in a header."""
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise Forbidden("Missing or invalid CSRF token. Refresh the page and try again.")
