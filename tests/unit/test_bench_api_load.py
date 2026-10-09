"""Load generator for NFR-P-03/04/05: schedule, percentiles, thresholds (no network)."""

from __future__ import annotations

import asyncio
import math

import httpx
import pytest

from payintel.bench.api_load import (
    Scenario,
    default_scenarios,
    percentile,
    run_all,
    run_scenario,
    sample_domains,
    summarize,
)


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0
    assert percentile(values, 99) == 99.0
    assert percentile(values, 100) == 100.0
    assert percentile([7.0], 99) == 7.0
    assert math.isnan(percentile([], 95))
    with pytest.raises(ValueError):
        percentile(values, 101)


def _scenario(**kw: object) -> Scenario:
    base: dict[str, object] = {
        "name": "s",
        "requirement": "NFR-P-03",
        "urls": ["/v1/stores/a.de"],
        "rps": 10.0,
        "duration_s": 1.0,
        "p95_ms": 300.0,
        "p99_ms": 800.0,
    }
    base.update(kw)
    return Scenario(**base)  # type: ignore[arg-type]


def test_summarize_applies_both_thresholds_and_errors() -> None:
    ok = summarize(_scenario(), [10.0] * 98 + [500.0] * 2, errors=0, elapsed_s=10.0)
    assert ok.passed and ok.p95_ms == 10.0 and ok.p99_ms == 500.0 and ok.rps_achieved == 10.0
    slow_tail = summarize(_scenario(), [10.0] * 98 + [900.0] * 2, errors=0, elapsed_s=10.0)
    assert not slow_tail.passed
    slow_p95 = summarize(_scenario(), [350.0] * 100, errors=0, elapsed_s=10.0)
    assert not slow_p95.passed
    with_error = summarize(_scenario(), [10.0] * 10, errors=1, elapsed_s=1.0)
    assert not with_error.passed and with_error.requests == 11
    no_p99 = summarize(_scenario(p99_ms=None), [10.0] * 98 + [5000.0] * 2, errors=0, elapsed_s=1.0)
    assert no_p99.passed and no_p99.threshold_p99_ms is None
    assert set(no_p99.as_dict()) >= {"name", "requirement", "p95_ms", "passed", "rps_achieved"}


def test_run_scenario_is_open_loop_and_counts_statuses() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("boom"):
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    sleeps: list[float] = []
    virtual = [0.0]

    def clock() -> float:
        virtual[0] += 0.001  # every look at the clock costs a millisecond
        return virtual[0]

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        virtual[0] += delay

    async def go() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://bench"
        ) as client:
            result = await run_scenario(
                client,
                _scenario(urls=["/v1/stores/a.de", "/v1/stores/boom"], rps=4.0, duration_s=2.0),
                sleep=fake_sleep,
                clock=clock,
            )
        assert result.requests == 8 and result.errors == 4 and not result.passed
        assert seen.count("/v1/stores/boom") == 4
        # requests are scheduled at 1/rps intervals, not after the previous response
        assert len(sleeps) >= 7 and all(0 < d <= 0.25 for d in sleeps)

    asyncio.run(go())


def test_run_all_and_sample_domains_with_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer pik_test"
        if request.url.path == "/v1/stores":
            sort = request.url.params.get("sort", "domain")
            return httpx.Response(
                200, json={"items": [{"domain": f"{sort}-{i}.de"} for i in range(3)]}
            )
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    domains = asyncio.run(sample_domains("http://bench", "pik_test", transport=transport))
    assert len(domains) == 9 and domains[0] == "traffic_rank-0.de"
    scenarios = default_scenarios(
        domains, countries=["DE"], platforms=["shopify"], providers=["stripe"], duration_s=0.2
    )
    assert [s.requirement for s in scenarios] == ["NFR-P-03", "NFR-P-04", "NFR-P-05"]
    assert scenarios[0].rps == 20.0 and scenarios[0].p99_ms == 800.0
    assert "/v1/stores?limit=1000&country=DE" in scenarios[1].urls
    assert "/v1/stats/market-share?country=DE&platform=shopify" in scenarios[2].urls
    results = asyncio.run(run_all("http://bench", "pik_test", scenarios, transport=transport))
    assert [r.passed for r in results] == [True, True, True]
    with pytest.raises(ValueError):
        default_scenarios([], countries=[], platforms=[], providers=[], duration_s=1)
