"""Open-loop HTTP load generator for the NFR-P-03/P-04/P-05 thresholds (AC-12).

Requests are *scheduled* at a fixed rate (request *i* starts at ``t0 + i / rps``)
whatever the server does, so a slow server piles up concurrent requests and the
measured latency includes queueing: that is the number the thresholds talk
about. Everything here is pure asyncio + httpx; no external load tools.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True)
class Scenario:
    """One endpoint family under one rate, with its thresholds (ms)."""

    name: str
    requirement: str
    urls: Sequence[str]
    rps: float
    duration_s: float
    p95_ms: float
    p99_ms: float | None = None
    expected_status: int = 200


@dataclass
class ScenarioResult:
    name: str
    requirement: str
    requests: int
    errors: int
    rps_target: float
    rps_achieved: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    threshold_p95_ms: float
    threshold_p99_ms: float | None
    passed: bool
    latencies_ms: list[float] = field(default_factory=list, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "requirement": self.requirement,
            "requests": self.requests,
            "errors": self.errors,
            "rps_target": self.rps_target,
            "rps_achieved": round(self.rps_achieved, 2),
            "p50_ms": round(self.p50_ms, 1),
            "p95_ms": round(self.p95_ms, 1),
            "p99_ms": round(self.p99_ms, 1),
            "max_ms": round(self.max_ms, 1),
            "threshold_p95_ms": self.threshold_p95_ms,
            "threshold_p99_ms": self.threshold_p99_ms,
            "passed": self.passed,
        }


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100) over an unsorted sequence."""
    if not values:
        return math.nan
    if not 0 <= q <= 100:
        raise ValueError("q must be within 0..100")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(
    scenario: Scenario, latencies_ms: Sequence[float], errors: int, elapsed_s: float
) -> ScenarioResult:
    n = len(latencies_ms) + errors
    p95 = percentile(latencies_ms, 95)
    p99 = percentile(latencies_ms, 99)
    passed = (
        errors == 0
        and n > 0
        and p95 <= scenario.p95_ms
        and (scenario.p99_ms is None or p99 <= scenario.p99_ms)
    )
    return ScenarioResult(
        name=scenario.name,
        requirement=scenario.requirement,
        requests=n,
        errors=errors,
        rps_target=scenario.rps,
        rps_achieved=n / elapsed_s if elapsed_s > 0 else 0.0,
        p50_ms=percentile(latencies_ms, 50),
        p95_ms=p95,
        p99_ms=p99,
        max_ms=max(latencies_ms) if latencies_ms else math.nan,
        threshold_p95_ms=scenario.p95_ms,
        threshold_p99_ms=scenario.p99_ms,
        passed=passed,
        latencies_ms=list(latencies_ms),
    )


async def run_scenario(
    client: httpx.AsyncClient,
    scenario: Scenario,
    *,
    sleep: Callable[[float], Any] = asyncio.sleep,
    clock: Callable[[], float] = time.perf_counter,
) -> ScenarioResult:
    """Fire `rps * duration_s` requests on a fixed schedule; wait for all of them."""
    total = max(1, int(scenario.rps * scenario.duration_s))
    interval = 1.0 / scenario.rps
    latencies: list[float] = []
    errors = 0
    tasks: list[asyncio.Task[None]] = []

    async def one(url: str) -> None:
        nonlocal errors
        started = clock()
        try:
            response = await client.get(url)
        except httpx.HTTPError:
            errors += 1
            return
        elapsed = (clock() - started) * 1000.0
        if response.status_code != scenario.expected_status:
            errors += 1
            return
        latencies.append(elapsed)

    t0 = clock()
    for i in range(total):
        due = t0 + i * interval
        delay = due - clock()
        if delay > 0:
            await sleep(delay)
        tasks.append(asyncio.create_task(one(scenario.urls[i % len(scenario.urls)])))
    await asyncio.gather(*tasks)
    return summarize(scenario, latencies, errors, clock() - t0)


async def run_all(
    base_url: str,
    api_key: str,
    scenarios: Sequence[Scenario],
    *,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[ScenarioResult]:
    headers = {"Authorization": f"Bearer {api_key}"}
    limits = httpx.Limits(max_connections=512, max_keepalive_connections=64)
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=timeout_s,
        limits=limits,
        transport=transport,
    ) as client:
        return [await run_scenario(client, s) for s in scenarios]


async def sample_domains(
    base_url: str,
    api_key: str,
    *,
    pages: int = 3,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """Distinct domains from the first page of each sort order: a spread over the dataset."""
    headers = {"Authorization": f"Bearer {api_key}"}
    found: dict[str, None] = {}
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, timeout=120.0, transport=transport
    ) as client:
        for sort in ("traffic_rank", "updated_at", "domain")[:pages]:
            response = await client.get("/v1/stores", params={"limit": 1000, "sort": sort})
            response.raise_for_status()
            for item in response.json()["items"]:
                found[item["domain"]] = None
    return list(found)


def default_scenarios(
    domains: Sequence[str],
    *,
    countries: Sequence[str],
    platforms: Sequence[str],
    providers: Sequence[str],
    duration_s: float,
    lookup_rps: float = 20.0,
    search_rps: float = 1.0,
    stats_rps: float = 1.0,
) -> list[Scenario]:
    """The ТЗ thresholds: P-03 at 20 rps, P-04 1000-row pages, P-05 market share."""
    if not domains:
        raise ValueError("no domains to look up")
    search_urls = ["/v1/stores?limit=1000", "/v1/stores?limit=1000&sort=traffic_rank"]
    search_urls += [f"/v1/stores?limit=1000&country={c}" for c in countries]
    search_urls += [f"/v1/stores?limit=1000&platform={p}" for p in platforms]
    search_urls += [f"/v1/stores?limit=1000&provider={p}" for p in providers]
    search_urls += [
        f"/v1/stores?limit=1000&country={c}&platform={p}&sort=updated_at"
        for c in countries[:2]
        for p in platforms[:2]
    ]
    stats_urls = ["/v1/stats/market-share"]
    stats_urls += [f"/v1/stats/market-share?country={c}" for c in countries]
    stats_urls += [
        f"/v1/stats/market-share?country={c}&platform={p}"
        for c in countries[:2]
        for p in platforms[:2]
    ]
    stats_urls += ["/v1/stats/market-share?role=gateway"]
    return [
        Scenario(
            name="store_lookup",
            requirement="NFR-P-03",
            urls=[f"/v1/stores/{d}" for d in domains],
            rps=lookup_rps,
            duration_s=duration_s,
            p95_ms=300.0,
            p99_ms=800.0,
        ),
        Scenario(
            name="store_search_1000",
            requirement="NFR-P-04",
            urls=search_urls,
            rps=search_rps,
            duration_s=duration_s,
            p95_ms=2_000.0,
        ),
        Scenario(
            name="market_share",
            requirement="NFR-P-05",
            urls=stats_urls,
            rps=stats_rps,
            duration_s=duration_s,
            p95_ms=3_000.0,
        ),
    ]
