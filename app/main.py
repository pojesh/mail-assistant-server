"""FastAPI application: user routes under /api, internal routes under /internal."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.db import get_engine
from app.errors import ERROR_RESPONSES, register_error_handlers
from app.graphs.checkpointer import open_checkpointer
from app.graphs.email_graph import init_email_graph
from app.logging import configure_logging, get_logger
from app.openapi_extras import build_openapi
from app.routes import (
    account,
    activity,
    approvals,
    attachments,
    auth,
    calendar,
    chat,
    dashboard,
    emails,
    health,
    internal,
    notifications,
    onboarding,
    preferences,
)
from app.security.rate_limit import handle_rate_limit, limiter
from app.security.session import require_csrf

log = get_logger(__name__)


def operation_id(route: APIRoute) -> str:
    """Use the route function's name as a stable OpenAPI operation id (e.g. `list_emails`)."""
    return route.name


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown hooks."""
    log.info("api_starting", env=settings.app_env, llm_provider=settings.llm_provider)
    async with open_checkpointer() as checkpointer:
        init_email_graph(checkpointer)
        yield
    await get_engine().dispose()


def create_app() -> FastAPI:
    """Build the FastAPI app with middleware, error handlers and routers."""
    configure_logging(settings.log_level, json_logs=settings.is_production)

    app = FastAPI(
        title="Mail Assistant API",
        version="0.1.0",
        lifespan=lifespan,
        generate_unique_id_function=operation_id,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, handle_rate_limit)

    # Every user route checks the CSRF header on state-changing requests.
    user_routers = [
        health,
        auth,
        emails,
        approvals,
        preferences,
        onboarding,
        calendar,
        chat,
        attachments,
        activity,
        notifications,
        dashboard,
        account,
    ]
    for module in user_routers:
        app.include_router(
            module.router,
            prefix="/api",
            responses=ERROR_RESPONSES,
            dependencies=[Depends(require_csrf)],
        )
    # Internal routes check a Google OIDC token instead (Pub/Sub, Cloud Scheduler).
    app.include_router(internal.router)
    app.openapi = lambda: build_openapi(app)  # type: ignore[method-assign]
    return app


app = create_app()
