"""`call_with_retry`: the single way agents call an LLM (timeout + 2 retries with backoff)."""

import asyncio
from collections.abc import Awaitable, Callable

from langchain_core.exceptions import (
    ModelAPIError,
    ModelConnectionError,
    ModelRateLimitError,
    ModelTimeoutError,
    OutputParserException,
)
from pydantic import ValidationError

from app.errors import AppError, LLMUnavailable
from app.logging import get_logger

log = get_logger(__name__)

TIMEOUT_SECONDS = 120
MAX_RETRIES = 2
RATE_LIMIT_DELAYS = (20, 45)  # free tiers reset their per-minute quota within ~60s
OTHER_DELAYS = (2, 5)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_TEXT = ("429", "RESOURCE_EXHAUSTED", "rate limit", "UNAVAILABLE", "overloaded")


def is_rate_limit(error: BaseException) -> bool:
    """Rate-limit errors look different per provider; check the type, status and text."""
    if isinstance(error, ModelRateLimitError) or "RateLimit" in type(error).__name__:
        return True
    if getattr(error, "status_code", None) == 429:
        return True
    text = str(error)
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "rate limit" in text.lower()


def is_retryable(error: BaseException) -> bool:
    """Timeouts, rate limits, 5xx, and malformed structured output are worth another try.

    LangChain's standard error types cover Gemini; Groq's client errors are recognised by
    their status code and class names.
    """
    if isinstance(error, TimeoutError | OutputParserException | ValidationError):
        return True
    if isinstance(error, ModelAPIError | ModelConnectionError | ModelTimeoutError):
        return True
    if is_rate_limit(error) or getattr(error, "status_code", None) in RETRYABLE_STATUS:
        return True
    name = type(error).__name__
    if any(word in name for word in ("Timeout", "Connection", "ServerError", "Unavailable")):
        return True
    return any(word in str(error) for word in RETRYABLE_TEXT)


async def call_with_retry[T](call: Callable[[], Awaitable[T]], purpose: str) -> T:
    """Run `call()` with a timeout, retrying transient failures twice.

    Raises LLMUnavailable (a safe, user-facing error) when all attempts fail.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await asyncio.wait_for(call(), timeout=TIMEOUT_SECONDS)
        except AppError:
            raise  # our own errors (e.g. NeedsReconnect from a tool) are not LLM failures
        except Exception as error:
            if attempt == MAX_RETRIES or not is_retryable(error):
                log.warning("llm_call_failed", purpose=purpose, error_type=type(error).__name__)
                raise LLMUnavailable() from error
            delays = RATE_LIMIT_DELAYS if is_rate_limit(error) else OTHER_DELAYS
            log.info("llm_call_retry", purpose=purpose, attempt=attempt + 1, wait=delays[attempt])
            await asyncio.sleep(delays[attempt])
    raise LLMUnavailable()  # unreachable; keeps type checkers happy
