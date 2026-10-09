"""Run a coroutine to completion without touching the current event loop.

``asyncio.run`` calls ``set_event_loop(None)`` on exit, which breaks the
session-scoped loop that the browser tests (``tests/e2e``,
``tests/unit/checkout``) share under pytest-asyncio.  Synchronous tests use
``run_sync`` instead, so both kinds can live in one pytest session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
