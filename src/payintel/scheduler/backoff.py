"""Retry delays (FR-SC-05): 1 h, 6 h, 24 h, 72 h; then `unreachable` until the next cycle."""

from __future__ import annotations

from datetime import datetime, timedelta

from payintel.core.settings import ScanSettings


def retry_delay(fail_count: int, s: ScanSettings) -> timedelta | None:
    """Delay before the next attempt after `fail_count` consecutive failures (1-based).

    Returns None once the failure budget is exhausted: the caller marks the host
    unreachable and schedules the next *planned* cycle instead of a retry.
    """
    if fail_count >= s.max_failures_before_unreachable:
        return None
    hours = s.retry_backoff_hours[min(fail_count, len(s.retry_backoff_hours)) - 1]
    return timedelta(hours=hours)


def next_attempt(now: datetime, fail_count: int, s: ScanSettings, *, cycle: timedelta) -> datetime:
    delay = retry_delay(fail_count, s)
    return now + (cycle if delay is None else delay)
