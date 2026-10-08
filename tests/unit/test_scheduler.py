"""FR-SC-02 priority formula, FR-SC-05 backoff, FR-SC-06 token buckets, LR-22 exclusions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from payintel.core.models.base import DomainStatus, ScanType
from payintel.core.settings import ScanSettings
from payintel.scheduler.backoff import next_attempt, retry_delay
from payintel.scheduler.planner import heavy_scan_excluded, interval_for
from payintel.scheduler.politeness import MemoryRateLimiter, etld1_key, host_key, ip_key
from payintel.scheduler.priority import (
    MANUAL_PRIORITY,
    PriorityInputs,
    compute_priority,
    rank_score,
    staleness,
)

S = ScanSettings()


def test_rank_score_and_staleness() -> None:
    assert rank_score(None) == 0.0 and rank_score(0) == 0.0
    assert rank_score(1) == 1.0
    assert rank_score(1_000_000) == 0.0
    assert 0.49 < rank_score(1000) < 0.51
    assert staleness(None, 7) == 3.0
    assert staleness(3.5, 7) == 0.5
    assert staleness(100, 7) == 3.0


def test_priority_orders_as_spec_expects() -> None:
    base = PriorityInputs(False, None, False, False, 0.0, 30)
    shop = PriorityInputs(True, None, False, False, 0.0, 7)
    top = PriorityInputs(True, 1, True, True, None, 7)
    assert compute_priority(base, S) == 1.0
    assert compute_priority(shop, S) == 2.0
    assert compute_priority(top, S) == 2 * 2 * 2 * 2 * 4
    stale = PriorityInputs(True, None, False, False, 14.0, 7)
    assert compute_priority(stale, S) > compute_priority(shop, S)
    # weights are tunable: zeroing the watchlist weight removes its effect
    s0 = ScanSettings(priority_weight_watchlist=0.0)
    assert compute_priority(PriorityInputs(True, None, False, True, 0.0, 7), s0) == 2.0
    assert MANUAL_PRIORITY > compute_priority(top, S)


def test_backoff_sequence() -> None:
    assert [retry_delay(n, S) for n in (1, 2, 3)] == [
        timedelta(hours=1),
        timedelta(hours=6),
        timedelta(hours=24),
    ]
    assert retry_delay(4, S) is None  # 4th failure → unreachable, next planned cycle
    now = datetime(2026, 10, 8, tzinfo=UTC)
    assert next_attempt(now, 2, S, cycle=timedelta(days=7)) == now + timedelta(hours=6)
    assert next_attempt(now, 4, S, cycle=timedelta(days=7)) == now + timedelta(days=7)
    s5 = ScanSettings(max_failures_before_unreachable=5)
    assert retry_delay(4, s5) == timedelta(hours=72)


def test_intervals_and_heavy_scan_exclusions() -> None:
    assert interval_for(ScanType.LIGHT, DomainStatus.ECOMMERCE, on_watchlist=False, s=S).days == 7
    assert interval_for(ScanType.LIGHT, DomainStatus.CANDIDATE, on_watchlist=False, s=S).days == 30
    assert (
        interval_for(ScanType.CHECKOUT, DomainStatus.ECOMMERCE, on_watchlist=False, s=S).days == 30
    )
    assert interval_for(ScanType.CHECKOUT, DomainStatus.ECOMMERCE, on_watchlist=True, s=S).days == 7
    assert heavy_scan_excluded("shop.ru", S) and heavy_scan_excluded("shop.xn--p1ai", S)
    assert heavy_scan_excluded("shop.cn", S) and not heavy_scan_excluded("shop.de", S)
    assert not heavy_scan_excluded("shop.ru", ScanSettings(excluded_heavy_scan_tlds=()))


def test_memory_rate_limiter_token_bucket() -> None:
    t = [100.0]
    lim = MemoryRateLimiter(now=lambda: t[0])

    async def run() -> list[float]:
        waits = [await lim.acquire(host_key("Shop.DE"), rate=1.0) for _ in range(3)]
        t[0] += 3.0  # the three queued requests go out at t+0, t+1, t+2
        waits.append(await lim.acquire(host_key("shop.de"), rate=1.0))
        waits.append(await lim.acquire(ip_key("192.0.2.1"), rate=5.0, burst=5))
        return waits

    waits = asyncio.run(run())
    assert waits[0] == 0.0 and waits[1] == pytest.approx(1.0) and waits[2] == pytest.approx(2.0)
    assert waits[3] == 0.0  # bucket paid its debt back after 3 s at 1 rps
    assert waits[4] == 0.0


def test_memory_slots_per_etld1() -> None:
    lim = MemoryRateLimiter()

    async def run() -> tuple[bool, bool, bool]:
        a = await lim.take_slot(etld1_key("brand.de"), 1, 60)
        b = await lim.take_slot(etld1_key("brand.de"), 1, 60)
        await lim.release_slot(etld1_key("brand.de"))
        c = await lim.take_slot(etld1_key("brand.de"), 1, 60)
        return a, b, c

    assert asyncio.run(run()) == (True, False, True)
