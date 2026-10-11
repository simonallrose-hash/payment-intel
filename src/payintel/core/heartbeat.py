"""Worker heartbeats with host load (FR-ADM-03 «загрузка серверов»).

Every crawl worker (light, checkout, exporter, …) upserts one `worker_heartbeat`
row every `interval_seconds`: the host it runs on, the 1/5-minute load average,
CPU count, RAM used/total, how many tasks it has in flight and has finished.
The admin crawler page lists the rows and marks a worker *stale* when its last
beat is older than three intervals and *stopped* after a clean shutdown. The
numbers come from the kernel (`os.getloadavg`, `/proc/meminfo`): no agent, no
monitoring stack is needed for the first look; Prometheus (`core/metrics.py`)
remains the source for alerting (runbook §9).
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.models.scans import WorkerHeartbeat

MEMINFO = Path("/proc/meminfo")
STALE_INTERVALS = 3


@dataclass(frozen=True)
class HostLoad:
    hostname: str
    pid: int
    load1: float
    load5: float
    cpu_count: int
    mem_total_mb: int
    mem_used_mb: int

    @property
    def mem_share(self) -> float:
        return self.mem_used_mb / self.mem_total_mb if self.mem_total_mb else 0.0


def _meminfo(path: Path) -> tuple[int, int]:
    """(total, used) in MB from /proc/meminfo; used = total − MemAvailable."""
    values: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                values[key.strip()] = int(parts[0])  # kB
    except OSError:
        return 0, 0
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return total // 1024, max(0, total - available) // 1024


def snapshot(*, meminfo: Path = MEMINFO) -> HostLoad:
    """Host load of the current process; zeros where the platform gives nothing."""
    try:
        load1, load5, _ = os.getloadavg()
    except OSError:
        load1 = load5 = 0.0
    total, used = _meminfo(meminfo)
    return HostLoad(
        hostname=socket.gethostname(),
        pid=os.getpid(),
        load1=round(load1, 2),
        load5=round(load5, 2),
        cpu_count=os.cpu_count() or 0,
        mem_total_mb=total,
        mem_used_mb=used,
    )


def beat(
    session: Session,
    *,
    worker_id: str,
    kind: str,
    concurrency: int,
    in_flight: int,
    done: int,
    load: HostLoad | None = None,
    stopped: bool = False,
    clock: Clock = SYSTEM_CLOCK,
) -> None:
    """Upsert the worker's row (one per worker id); `started_at` is kept from the first beat."""
    load = load or snapshot()
    now = clock.now()
    values = {
        "worker_id": worker_id,
        "kind": kind,
        "hostname": load.hostname,
        "pid": load.pid,
        "load1": load.load1,
        "load5": load.load5,
        "cpu_count": load.cpu_count,
        "mem_total_mb": load.mem_total_mb,
        "mem_used_mb": load.mem_used_mb,
        "concurrency": concurrency,
        "in_flight": in_flight,
        "scans_done": done,
        "started_at": now,
        "updated_at": now,
        "stopped_at": now if stopped else None,
    }
    ins = pg_insert(WorkerHeartbeat).values(values)
    session.execute(
        ins.on_conflict_do_update(
            index_elements=[WorkerHeartbeat.worker_id],
            set_={k: v for k, v in values.items() if k not in ("worker_id", "started_at")},
        )
    )


@dataclass(frozen=True)
class WorkerView:
    row: WorkerHeartbeat
    state: str  # live | stale | stopped
    age_seconds: int


def workers(
    session: Session, *, now: datetime, interval_seconds: float = 30.0, keep_days: int = 7
) -> list[WorkerView]:
    """Heartbeats of the last `keep_days`, newest first, each with its liveness state."""
    rows = session.execute(
        select(WorkerHeartbeat)
        .where(WorkerHeartbeat.updated_at >= now - timedelta(days=keep_days))
        .order_by(WorkerHeartbeat.kind, WorkerHeartbeat.worker_id)
    ).scalars()
    out: list[WorkerView] = []
    for row in rows:
        age = max(0, int((now - row.updated_at).total_seconds()))
        if row.stopped_at is not None:
            state = "stopped"
        elif age > STALE_INTERVALS * interval_seconds:
            state = "stale"
        else:
            state = "live"
        out.append(WorkerView(row, state, age))
    return out


class Heartbeat:
    """Rate-limited heartbeat writer for a worker loop: `maybe_beat` is cheap to call
    on every iteration and writes at most once per `interval_seconds` (in a thread, so
    the event loop is never blocked by the database)."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        worker_id: str,
        kind: str,
        concurrency: int,
        interval_seconds: float = 30.0,
        clock: Clock = SYSTEM_CLOCK,
        monotonic: Callable[[], float] = time.monotonic,
        load: Callable[[], HostLoad] = snapshot,
    ) -> None:
        self._factory = factory
        self.worker_id = worker_id
        self.kind = kind
        self.concurrency = concurrency
        self.interval = interval_seconds
        self._clock = clock
        self._monotonic = monotonic
        self._load = load
        self._last: float | None = None
        self.beats = 0

    def _write(self, in_flight: int, done: int, *, stopped: bool) -> None:
        with self._factory() as session:
            beat(
                session,
                worker_id=self.worker_id,
                kind=self.kind,
                concurrency=self.concurrency,
                in_flight=in_flight,
                done=done,
                load=self._load(),
                stopped=stopped,
                clock=self._clock,
            )
            session.commit()
        self.beats += 1

    def due(self) -> bool:
        now = self._monotonic()
        return self._last is None or now - self._last >= self.interval

    async def maybe_beat(self, *, in_flight: int, done: int) -> bool:
        if not self.due():
            return False
        self._last = self._monotonic()
        await asyncio.to_thread(self._write, in_flight, done, stopped=False)
        return True

    async def stop(self, *, done: int) -> None:
        """Final beat on a clean shutdown: the admin page shows the worker as stopped."""
        await asyncio.to_thread(self._write, 0, done, stopped=True)
