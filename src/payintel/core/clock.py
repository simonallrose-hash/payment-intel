"""Injectable clock so that tests never depend on wall time (brief rule 9)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FixedClock:
    """Deterministic clock for tests; `advance()` moves it forward."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock requires an aware datetime")
        self._at = at

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = at

    def advance(self, *, seconds: float = 0, days: float = 0) -> None:
        from datetime import timedelta

        self._at = self._at + timedelta(seconds=seconds, days=days)


SYSTEM_CLOCK = SystemClock()
