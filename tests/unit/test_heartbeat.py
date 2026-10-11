"""FR-ADM-03: worker heartbeat — host snapshot and the rate-limited writer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from payintel.core import heartbeat as hb

MEMINFO = """MemTotal:        8000000 kB
MemFree:          500000 kB
MemAvailable:    3000000 kB
Buffers:          100000 kB
"""


def test_snapshot_reads_load_cpus_and_meminfo(tmp_path: Path) -> None:
    path = tmp_path / "meminfo"
    path.write_text(MEMINFO)
    load = hb.snapshot(meminfo=path)
    assert load.cpu_count >= 1 and load.pid > 0 and load.hostname
    assert load.load1 >= 0 and load.load5 >= 0
    assert (load.mem_total_mb, load.mem_used_mb) == (8000000 // 1024, 5000000 // 1024)
    assert 0.62 < load.mem_share < 0.63  # used = total − MemAvailable


def test_snapshot_without_meminfo_reports_zero_memory(tmp_path: Path) -> None:
    load = hb.snapshot(meminfo=tmp_path / "absent")
    assert (load.mem_total_mb, load.mem_used_mb, load.mem_share) == (0, 0, 0.0)


class _Session:
    def __init__(self, log: list[Any]) -> None:
        self.log = log

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, stmt: Any) -> None:
        self.log.append(stmt)

    def commit(self) -> None:
        self.log.append("commit")


def test_writer_beats_at_most_once_per_interval_and_once_more_on_stop() -> None:
    clock_now = [100.0]
    log: list[Any] = []
    writer = hb.Heartbeat(
        lambda: _Session(log),  # type: ignore[arg-type]
        worker_id="w-1",
        kind="light",
        concurrency=4,
        interval_seconds=30.0,
        monotonic=lambda: clock_now[0],
        load=lambda: hb.HostLoad("host", 1, 0.5, 0.4, 4, 8000, 4000),
    )

    async def drive() -> list[bool]:
        out = [await writer.maybe_beat(in_flight=1, done=0)]  # first call writes
        clock_now[0] += 10
        out.append(await writer.maybe_beat(in_flight=2, done=1))  # 10 s later: skipped
        clock_now[0] += 25
        out.append(await writer.maybe_beat(in_flight=3, done=2))  # 35 s after the first: writes
        await writer.stop(done=3)
        return out

    assert asyncio.run(drive()) == [True, False, True]
    assert writer.beats == 3 and log.count("commit") == 3
    stop_stmt = log[-2]
    params = stop_stmt.compile().params
    assert params["stopped_at"] is not None and params["in_flight"] == 0
    assert params["scans_done"] == 3 and params["hostname"] == "host"
