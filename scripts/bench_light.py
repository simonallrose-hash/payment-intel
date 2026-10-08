"""NFR-P-01 load run for the light scanner (not in CI): `make bench-light`.

Serves the WooCommerce fixture shop for N synthetic hosts from a local HTTP
server, seeds those hosts into the configured Postgres (PAYINTEL_POSTGRES__DSN),
and runs the real worker loop with the production politeness rules (1 rps per
host, 5 rps per IP bucket keyed by the fake public address of each host) and
real sleeps. The result is domains per second; the ТЗ target is ≥20/s on one
crawl server.

Usage: python scripts/bench_light.py --domains 500 --concurrency 200
Requires a reachable Postgres with migrations applied (`make migrate && make seed`).
ClickHouse and S3 are optional: pass --clickhouse / --s3 to include the sinks.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from sqlalchemy.orm import sessionmaker

from payintel.core.ch import make_ch_client
from payintel.core.db import get_engine
from payintel.core.models.base import DomainSourceKind, ScanType
from payintel.core.s3 import ObjectStore, make_s3_client
from payintel.core.settings import get_settings
from payintel.crawl.light.runtime import build_context
from payintel.crawl.light.worker import LightScanner, run_batch
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


class LoopbackTransport(httpx.AsyncHTTPTransport):
    def __init__(self, port: int) -> None:
        super().__init__()
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
    n = int(hostname.split("-")[1].split(".")[0])
    return [str(ipaddress.IPv4Address(int(ipaddress.IPv4Address("93.184.0.0")) + n))]


async def resolve(hostname: str) -> list[str]:
    return fake_public_ip(hostname)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", type=int, default=500)
    ap.add_argument("--concurrency", type=int, default=200)
    ap.add_argument("--clickhouse", action="store_true", help="write observations")
    ap.add_argument("--s3", action="store_true", help="write artefacts")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

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
        session.commit()
    print(f"seeded {r.domains_new} new / {r.domains_updated} existing bench domains")

    store = None
    if args.s3:
        store = ObjectStore(make_s3_client(settings.s3))
        store.ensure_bucket(settings.s3.bucket_artifacts)
    ctx = build_context(
        settings,
        worker_id="bench",
        ch_client=make_ch_client(settings.clickhouse) if args.clickhouse else None,
        store=store,
        resolve=resolve,
        allow_private=False,
        transport=LoopbackTransport(port),
        base_scheme="http",
    )
    scanner = LightScanner(ctx)

    async def run() -> tuple[int, float]:
        total, t0 = 0, time.monotonic()
        try:
            while True:
                outcomes = await run_batch(
                    factory, scanner, limit=args.concurrency, concurrency=args.concurrency
                )
                if not outcomes:
                    break
                total += len(outcomes)
                bad = [o for o in outcomes if o.status.value != "ok"]
                print(
                    f"  batch {len(outcomes)} ok={len(outcomes) - len(bad)} "
                    f"elapsed={time.monotonic() - t0:.1f}s"
                )
        finally:
            await ctx.fetcher.aclose()
        return total, time.monotonic() - t0

    total, elapsed = asyncio.run(run())
    rate = total / elapsed if elapsed else 0.0
    verdict = "PASS" if rate >= 20 else "FAIL"
    print(f"NFR-P-01: {total} domains in {elapsed:.1f}s = {rate:.1f} domains/s -> {verdict}")
    httpd.shutdown()
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
