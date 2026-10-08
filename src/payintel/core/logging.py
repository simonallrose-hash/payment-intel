"""structlog JSON logging with request_id / scan_run_id / org_id context (NFR-M-05)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TextIO

import structlog

CONTEXT_KEYS = ("request_id", "scan_run_id", "org_id")


def configure_logging(
    level: str = "INFO", json_output: bool = True, *, stream: TextIO | None = None
) -> None:
    """Configure structlog once per process. JSON lines to stdout by default."""
    out = stream or sys.stdout
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=out),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(level=level.upper(), stream=out, format="%(message)s")


def get_logger(name: str) -> Any:
    """Bound logger carrying the component name as `logger`."""
    return structlog.get_logger().bind(logger=name)


@contextmanager
def log_context(**values: str | int | None) -> Iterator[None]:
    """Bind trace ids (request_id, scan_run_id, org_id, ...) for the duration of a block."""
    bound = {k: v for k, v in values.items() if v is not None}
    tokens = structlog.contextvars.bind_contextvars(**bound)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
