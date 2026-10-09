"""Rate limit and record quotas per organisation (FR-API-07, FR-API-06).

* `RateLimiter`: sliding-window counter per organisation, `api_rps` requests
  per `window_seconds`. The store is pluggable: Redis in production, memory in
  tests. Exceeding → 429 with `Retry-After`.
* `records_used`: daily and monthly record counts from `usage_log`, so the
  quota survives restarts and is auditable (FR-API-09). Exports count too
  ("разовые по запросу в пределах квоты", FR-EX-03).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payintel.core.errors import PayIntelError
from payintel.core.models.audit import UsageLog


class RateLimited(PayIntelError):
    code = "rate_limited"

    def __init__(self, retry_after_seconds: int, message: str = "") -> None:
        super().__init__(message or "rate limit exceeded", retry_after=retry_after_seconds)
        self.retry_after_seconds = retry_after_seconds


class QuotaExceeded(PayIntelError):
    code = "quota_exceeded"

    def __init__(self, scope: str, retry_after_seconds: int) -> None:
        super().__init__(f"{scope} record quota exceeded", scope=scope)
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds


class CounterStore(Protocol):
    """Minimal store for sliding windows."""

    def incr(self, key: str, ttl_seconds: int) -> int: ...


class MemoryCounterStore:
    def __init__(self) -> None:
        self._data: dict[str, tuple[int, float]] = {}
        self._now = 0.0

    def set_time(self, now: float) -> None:
        self._now = now

    def incr(self, key: str, ttl_seconds: int) -> int:
        count, expires = self._data.get(key, (0, 0.0))
        if expires <= self._now:
            count = 0
        count += 1
        self._data[key] = (count, self._now + ttl_seconds)
        return count


class RedisCounterStore:
    def __init__(self, client: object) -> None:
        self._client = client

    def incr(self, key: str, ttl_seconds: int) -> int:
        pipe = self._client.pipeline()  # type: ignore[attr-defined]
        pipe.incr(key)
        pipe.expire(key, ttl_seconds, nx=True)
        count, _ = pipe.execute()
        return int(count)


@dataclass
class RateLimiter:
    store: CounterStore
    window_seconds: float = 1.0

    def check(self, org_id: uuid.UUID, limit: int, *, now: datetime) -> None:
        """Fixed window keyed by (org, window index); bursts above `limit` → 429."""
        if limit <= 0:
            raise RateLimited(1)
        window = int(now.timestamp() // self.window_seconds)
        key = f"ratelimit:{org_id}:{window}"
        count = self.store.incr(key, max(1, math.ceil(self.window_seconds * 2)))
        if count > limit:
            next_window = (window + 1) * self.window_seconds
            raise RateLimited(max(1, math.ceil(next_window - now.timestamp())))


def _day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(now: datetime) -> datetime:
    return _day_start(now).replace(day=1)


def records_used(session: Session, org_id: uuid.UUID, *, since: datetime) -> int:
    value = session.execute(
        select(func.coalesce(func.sum(UsageLog.records), 0)).where(
            UsageLog.org_id == org_id, UsageLog.ts >= since
        )
    ).scalar_one()
    return int(value)


def check_record_quota(
    session: Session,
    org_id: uuid.UUID,
    *,
    daily_limit: int,
    monthly_limit: int,
    now: datetime,
    about_to_add: int = 0,
) -> None:
    day = _day_start(now)
    if records_used(session, org_id, since=day) + about_to_add > daily_limit:
        raise QuotaExceeded("daily", int((day + timedelta(days=1) - now).total_seconds()))
    month = _month_start(now)
    if records_used(session, org_id, since=month) + about_to_add > monthly_limit:
        nxt = (month + timedelta(days=32)).replace(day=1)
        raise QuotaExceeded("monthly", int((nxt - now).total_seconds()))
