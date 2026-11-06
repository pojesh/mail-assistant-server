"""Custom exceptions and the handlers that turn them into `{error: {code, message}}` responses.

Responses never include stack traces, tokens or email content.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import get_logger

log = get_logger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    """Shape of every error response from the API."""

    error: ErrorBody


class AppError(Exception):
    """Base class for errors that are safe to show to the user."""

    status_code = 500
    code = "internal_error"
    default_message = "Something went wrong. Please try again."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class BadRequest(AppError):
    status_code = 400
    code = "bad_request"
    default_message = "The request is not valid."


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"
    default_message = "Please sign in."


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"
    default_message = "You are not allowed to do this."


class NotFound(AppError):
    status_code = 404
    code = "not_found"
    default_message = "Not found."


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    default_message = "This item was changed. Refresh and try again."


class NeedsReconnect(AppError):
    status_code = 409
    code = "needs_reconnect"
    default_message = "Your Google connection expired. Please reconnect Google."


class SlotUnavailable(AppError):
    status_code = 409
    code = "slot_unavailable"
    default_message = "That time is no longer free."


class GoogleApiError(AppError):
    status_code = 502
    code = "google_api_error"
    default_message = "Google did not respond as expected. Please try again."


class LLMUnavailable(AppError):
    status_code = 503
    code = "llm_unavailable"
    default_message = "The AI model is unavailable right now. Please try again shortly."


def error_json(status_code: int, code: str, message: str) -> JSONResponse:
    """Build the standard error response."""
    body = ErrorResponse(error=ErrorBody(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump())


async def handle_app_error(request: Request, error: Exception) -> JSONResponse:
    """Known application errors: return their code and safe message."""
    assert isinstance(error, AppError)
    log.info("request_failed", path=request.url.path, code=error.code)
    return error_json(error.status_code, error.code, error.message)


async def handle_validation_error(request: Request, error: Exception) -> JSONResponse:
    """Invalid input: say which fields are wrong, without echoing the submitted values."""
    assert isinstance(error, RequestValidationError)
    fields = [".".join(str(part) for part in item["loc"]) for item in error.errors()]
    return error_json(422, "validation_error", f"Invalid fields: {', '.join(fields)}")


async def handle_http_error(request: Request, error: Exception) -> JSONResponse:
    """Framework errors such as 404 for unknown routes or 405 for wrong methods."""
    assert isinstance(error, StarletteHTTPException)
    return error_json(error.status_code, "http_error", str(error.detail))


async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
    """Anything else: log it for us, return a generic message to the user."""
    log.error(
        "unhandled_error",
        path=request.url.path,
        error_type=type(error).__name__,
        exc_info=error,
    )
    return error_json(500, AppError.code, AppError.default_message)


def register_error_handlers(app: FastAPI) -> None:
    """Attach all exception handlers to the app."""
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.add_exception_handler(Exception, handle_unexpected_error)


# Documented on every user route so the generated client knows the error shape.
ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    "4XX": {"model": ErrorResponse, "description": "Client error"},
    "5XX": {"model": ErrorResponse, "description": "Server error"},
}
