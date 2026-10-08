"""Politeness limits (FR-SC-06, LR-05): token buckets per host, per hosting IP, per eTLD+1 session.

`RateLimiter.acquire(key, rate, burst)` returns the number of seconds the
caller must wait before its request may go out (0 when a token is available).
The bucket may go into debt, so N callers arriving at once are spaced 1/rate
apart instead of all waiting the same second.
`RedisRateLimiter` is the production implementation shared by all workers
(atomic Lua script); `MemoryRateLimiter` is the single-process fallback used in
tests and `make bench-light`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis

_LUA_BUCKET_SCRIPT = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = burst; ts = now end
tokens = math.min(burst, tokens + (now - ts) * rate)
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait = (1 - tokens) / rate
  tokens = tokens - 1
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return tostring(wait)
"""

_LUA_SLOT = """
local key = KEYS[1]
local max = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local current = tonumber(redis.call('GET', key) or '0')
if current >= max then return 0 end
redis.call('INCR', key)
redis.call('EXPIRE', key, ttl)
return 1
"""


class RateLimiter(Protocol):
    async def acquire(self, key: str, rate: float, burst: float = 1.0) -> float: ...
    async def take_slot(self, key: str, max_slots: int, ttl_seconds: int) -> bool: ...
    async def release_slot(self, key: str) -> None: ...


@dataclass
class _Bucket:
    tokens: float
    ts: float


class MemoryRateLimiter:
    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._buckets: dict[str, _Bucket] = {}
        self._slots: dict[str, int] = {}

    async def acquire(self, key: str, rate: float, burst: float = 1.0) -> float:
        now = self._now()
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(tokens=burst, ts=now)
            self._buckets[key] = b
        b.tokens = min(burst, b.tokens + (now - b.ts) * rate)
        b.ts = now
        if b.tokens >= 1:
            b.tokens -= 1
            return 0.0
        wait = (1 - b.tokens) / rate
        b.tokens -= 1  # debt: later callers queue behind this one
        return wait

    async def take_slot(self, key: str, max_slots: int, ttl_seconds: int) -> bool:
        if self._slots.get(key, 0) >= max_slots:
            return False
        self._slots[key] = self._slots.get(key, 0) + 1
        return True

    async def release_slot(self, key: str) -> None:
        if self._slots.get(key, 0) > 0:
            self._slots[key] -= 1


class RedisRateLimiter:
    def __init__(self, redis: Redis, *, prefix: str = "payintel:polite:") -> None:
        self._r = redis
        self._prefix = prefix
        self._bucket = redis.register_script(_LUA_BUCKET_SCRIPT)
        self._slot = redis.register_script(_LUA_SLOT)

    async def acquire(self, key: str, rate: float, burst: float = 1.0) -> float:
        ttl = max(2, int(burst / rate) + 2)
        raw = await self._bucket(keys=[self._prefix + key], args=[rate, burst, time.time(), ttl])
        return float(raw)

    async def take_slot(self, key: str, max_slots: int, ttl_seconds: int) -> bool:
        raw = await self._slot(keys=[self._prefix + "slot:" + key], args=[max_slots, ttl_seconds])
        return int(raw) == 1

    async def release_slot(self, key: str) -> None:
        k = self._prefix + "slot:" + key
        value = await self._r.decr(k)
        if value <= 0:
            await self._r.delete(k)


def host_key(hostname: str) -> str:
    return "host:" + hostname.lower()


def ip_key(ip: str) -> str:
    return "ip:" + ip


def etld1_key(etld1: str) -> str:
    return "etld1:" + etld1.lower()
