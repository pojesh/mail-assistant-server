"""Structured logging setup: JSON logs in production, readable console logs in development.

Never log email bodies, tokens or other personal content — log ids and counts instead.
"""

import logging
import sys

import structlog


def configure_logging(level: str, json_logs: bool) -> None:
    """Configure structlog and the standard library logger once at startup."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a named structured logger."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
