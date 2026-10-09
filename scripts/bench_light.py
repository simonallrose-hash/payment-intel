"""NFR-P-01 load run for the light scanner (not in CI): `make bench-light`.

Serves the WooCommerce fixture shop for N synthetic hosts from a local HTTP
server, seeds those hosts into the configured Postgres (PAYINTEL_POSTGRES__DSN),
and runs the real worker loop with the production politeness rules (1 rps per
host, 5 rps per IP bucket keyed by the fake public address of each host) and
real sleeps. The result is domains per second; the ТЗ target is ≥20/s on one
crawl server.

Usage: python scripts/bench_light.py --domains 2000 --concurrency 200 --processes 4
Requires a reachable Postgres with migrations applied (`make migrate && make seed`).
ClickHouse and S3 are optional: pass --clickhouse / --s3 to include the sinks.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import multiprocessing as mp
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from payintel.core.ch import make_ch_client
from payintel.core.clock import SYSTEM_CLOCK
from payintel.core.db import get_engine
from payintel.core.models.base import DomainSourceKind, ScanType
from payintel.core.models.domains import Domain, Host
from payintel.core.models.scans import ScanPlan
from payintel.core.s3 import ObjectStore, make_s3_client
from payintel.core.settings import get_settings
from payintel.crawl.light.runtime import build_context
from payintel.crawl.light.worker import LightScanner, run_pipeline
from payintel.discovery.ingest import ingest
from payintel.discovery.sources import SourceRecord
from payintel.scheduler import planner

ROOT = Path(__file__).resolve().parents[1]
SHOP = ROOT / "tests" / "fixtures" / "shops" / "woo-shop"
JS = b"/*! bench */ window.bench = 1;"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        return

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/robots.txt":
            body, ctype = (SHOP / "robots.txt").read_bytes(), "text/plain"
        elif path == "/":
            body, ctype = (SHOP / "index.html").read_bytes(), "text/html; charset=utf-8"
        elif path.startswith("/product/"):
            body, ctype = (SHOP / "product" / "index.html").read_bytes(), "text/html"
        elif path == "/warenkorb/":
            body, ctype = (SHOP / "cart" / "index.html").read_bytes(), "text/html"
        elif path.endswith(".js"):
            body, ctype = JS, "application/javascript"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QuietServer(ThreadingHTTPServer):
    """The scanner closes pooled connections mid-response under load; that is the
    client's business, not an error of the fixture server."""

    def handle_error(self, request: object, client_address: object) -> None:
        return


class LoopbackTransport(httpx.AsyncHTTPTransport):
    def __init__(self, port: int) -> None:
        super().__init__(limits=httpx.Limits(max_connections=64, max_keepalive_connections=0))
        self._port = port

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        local = httpx.Request(
            request.method,
            request.url.copy_with(scheme="http", host="127.0.0.1", port=self._port),
            headers=request.headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        return await super().handle_async_request(local)


def fake_public_ip(hostname: str) -> list[str]:
    """One distinct public address per host, so the per-IP bucket behaves as in production."""
    n = int(hashlib.sha256(hostname.encode()).hexdigest()[:4], 16)
    return [str(ipaddress.IPv4Address(int(ipaddress.IPv4Address("93.184.0.0")) + n))]


async def resolve(hostname: str) -> list[str]:
    return fake_public_ip(hostname)


def _worker(
    concurrency: int,
    clickhouse: bool,
    s3: bool,
    worker_id: str,
    out: mp.Queue[tuple[int, list[int]]],
) -> None:
    """One scanner process: its own fixture server, pools and event loop (as in production,
    one `scanner-light` container per CPU)."""
    httpd = QuietServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    settings = get_settings()
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    store = None
    if s3:
        store = ObjectStore(make_s3_client(settings.s3))
        store.ensure_bucket(settings.s3.bucket_artifacts)
    ctx = build_context(
        settings,
        worker_id=worker_id,
        ch_client=make_ch_client(settings.clickhouse) if clickhouse else None,
        store=store,
        resolve=resolve,
        allow_private=False,
        transport=lambda: LoopbackTransport(port),  # one pool per shard, as in production
        base_scheme="http",
    )
    scanner = LightScanner(ctx)
    durations: list[int] = []

    async def run() -> int:
        try:
            return await run_pipeline(
                factory,
                scanner,
                concurrency=concurrency,
                stop_when_empty=True,
                on_outcome=lambda o: durations.append(o.duration_ms),
            )
        finally:
            await ctx.fetcher.aclose()

    total = asyncio.run(run())
    httpd.shutdown()
    out.put((total, durations))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", type=int, default=500)
    ap.add_argument("--concurrency", type=int, default=200, help="in-flight scans per process")
    ap.add_argument("--processes", type=int, default=1, help="scanner processes (one per CPU)")
    ap.add_argument("--clickhouse", action="store_true", help="write observations")
    ap.add_argument("--s3", action="store_true", help="write artefacts")
    args = ap.parse_args()

    settings = get_settings()
    names = [f"bench-{i}.de" for i in range(args.domains)]
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with factory() as session:
        r = ingest(
            session,
            [SourceRecord(n, rank=i + 1) for i, n in enumerate(names)],
            source=DomainSourceKind.MANUAL,
            origin="bench",
        )
        planner.ensure_plans(session, ScanType.LIGHT, s=settings.scan, limit=args.domains)
        # Repeat runs: a scanned bench host is not due again for 7 days, so make
        # every bench plan due now (bench hosts only; nothing else in the queue moves).
        bench_hosts = select(Host.id).join(Domain).where(Domain.etld1.in_(names))
        session.execute(
            update(ScanPlan)
            .where(ScanPlan.host_id.in_(bench_hosts), ScanPlan.scan_type == ScanType.LIGHT)
            .values(next_scan_at=SYSTEM_CLOCK.now(), locked_until=None, locked_by=None)
        )
        session.commit()
    print(f"seeded {r.domains_new} new / {r.domains_updated} existing bench domains")

    # `spawn`: each worker starts a fresh interpreter (no inherited engine, event loop
    # or thread state from the seeding step), exactly like a separate container.
    ctx = mp.get_context("spawn")
    out: mp.Queue[tuple[int, list[int]]] = ctx.Queue()
    procs = [
        ctx.Process(
            target=_worker,
            args=(args.concurrency, args.clickhouse, args.s3, f"bench-{i}", out),
            daemon=False,
        )
        for i in range(args.processes)
    ]
    t0 = time.monotonic()
    for p in procs:
        p.start()
    total = 0
    durations: list[int] = []
    received = 0
    while received < len(procs):
        try:
            n, d = out.get(timeout=5)
        except queue.Empty:
            failed = [p.exitcode for p in procs if p.exitcode not in (None, 0)]
            if failed:
                raise SystemExit(f"worker(s) exited with {failed}") from None
            continue
        received += 1
        total += n
        durations.extend(d)
    for p in procs:
        p.join()
    elapsed = time.monotonic() - t0
    rate = total / elapsed if elapsed else 0.0
    verdict = "PASS" if rate >= 20 else "FAIL"
    if durations:
        d = sorted(durations)
        print(
            f"scan duration ms: p50 {d[len(d) // 2]}, p95 {d[int(len(d) * 0.95)]}, max {d[-1]} "
            f"(floor ≈ 6000 with 1 rps per host and 7 requests per site)"
        )
    print(
        f"NFR-P-01: {total} domains in {elapsed:.1f}s = {rate:.1f} domains/s "
        f"with {args.processes} process(es) x {args.concurrency} -> {verdict}"
    )
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
