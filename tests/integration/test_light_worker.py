"""Light scan end to end against a local HTTP server (FR-LS-01..07, FR-SC-06, NFR-R-05).

The server plays several shops on 127.0.0.1 (chosen by the Host header), the
fetcher is pointed at it through an httpx transport that rewrites the
connection target; nothing leaves the machine (pytest-socket). The politeness
limiter runs on a virtual clock so the 1 rps spacing is asserted without
sleeping.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from alembic import command as alembic_command
from clickhouse_connect.driver.client import Client
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from payintel.core.clock import FixedClock
from payintel.core.models.assets import HostJsAsset, JsAsset
from payintel.core.models.base import (
    Coverage,
    DomainSourceKind,
    DomainStatus,
    ScanStatus,
    ScanType,
)
from payintel.core.models.domains import Domain, DomainSource, Host
from payintel.core.models.scans import ScanPlan, ScanRun
from payintel.core.models.store import StoreProfile, StoreProvider
from payintel.core.reference_loader import load_reference, sync_reference
from payintel.core.s3 import ObjectStore
from payintel.core.settings import LightScanSettings, S3Settings, Settings
from payintel.crawl.light.runtime import build_context
from payintel.crawl.light.worker import LightScanner, LightScanOutcome, run_batch
from payintel.crawl.sanitize import EMAIL_MASK, PHONE_MASK
from payintel.discovery.ingest import ingest
from payintel.discovery.sources import SourceRecord
from payintel.scheduler import planner, queue
from payintel.scheduler.politeness import MemoryRateLimiter
from tests.conftest import alembic_config_for

pytestmark = pytest.mark.integration

SHOPS = Path(__file__).resolve().parents[1] / "fixtures" / "shops"
BIG_LIMIT = 200_000
HOST_DIRS = {
    "woo-shop.de": "woo-shop",
    "shopify-shop.com": "shopify-shop",
    "parked.de": "parked",
    "blog.fr": "blog",
    "pii-shop.com": "pii",
}
JS_BODY = b"/*! jQuery v3.7.1 */ window.jQuery = function () {};"


@dataclass
class ShopServer:
    port: int
    requests: list[tuple[str, str]] = field(default_factory=list)

    def paths(self, host: str) -> list[str]:
        return [p for h, p in self.requests if h == host]


def _handler(server: ShopServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:  # quiet
            return

        def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if self.headers.get("Host", "").startswith("woo-shop"):
                self.send_header("Set-Cookie", "woocommerce_items_in_cart=0; path=/")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            host = self.headers.get("Host", "").split(":")[0]
            path = self.path.split("?", 1)[0]
            server.requests.append((host, path))
            if host == "slow.de":
                if path == "/robots.txt":
                    self._send(404, b"")
                    return
                time.sleep(2.0)
                self._send(200, b"<html></html>")
                return
            if host == "forbidden.de":
                self._send(403 if path == "/" else 404, b"forbidden")
                return
            if host == "big.de":
                if path == "/robots.txt":
                    self._send(404, b"")
                    return
                body = b"<html><body>" + b"<p>x</p>" * (BIG_LIMIT // 4) + b"</body></html>"
                self._send(200, body)
                return
            if host == "bounce.de":
                if path == "/robots.txt":
                    self._send(404, b"")
                    return
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/admin")
                self.end_headers()
                return
            if path.endswith(".js"):
                self._send(200, JS_BODY, "application/javascript")
                return
            directory = HOST_DIRS.get(host)
            if directory is None:
                self._send(404, b"")
                return
            root = SHOPS / directory
            if path == "/robots.txt":
                f = root / "robots.txt"
                if f.exists():
                    self._send(200, f.read_bytes(), "text/plain")
                else:
                    self._send(404, b"")
                return
            if path == "/":
                self._send(200, (root / "index.html").read_bytes())
                return
            if path.startswith("/product/") and (root / "product" / "index.html").exists():
                self._send(200, (root / "product" / "index.html").read_bytes())
                return
            if path in {"/warenkorb/", "/cart/"} and (root / "cart" / "index.html").exists():
                self._send(200, (root / "cart" / "index.html").read_bytes())
                return
            self._send(404, b"<html><body>not found</body></html>")

    return Handler


@pytest.fixture(scope="module")
def shop_server() -> Iterator[ShopServer]:
    state = ShopServer(port=0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    state.port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()


class LoopbackTransport(httpx.AsyncHTTPTransport):
    """Send every request to the local server, keeping the original Host header."""

    def __init__(self, port: int) -> None:
        super().__init__()
        self._port = port

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        local = httpx.Request(
            request.method,
            request.url.copy_with(scheme="http", host="127.0.0.1", port=self._port),
            headers=request.headers,
            stream=request.stream,
            extensions=request.extensions,  # carries the per-request timeouts
        )
        return await super().handle_async_request(local)


@dataclass
class VirtualTime:
    now: float = 0.0
    waits: list[float] = field(default_factory=list)

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


async def _loopback(_hostname: str) -> list[str]:
    return ["127.0.0.1"]


async def _public(_hostname: str) -> list[str]:
    return ["93.184.216.34"]  # what production DNS would say; the transport still stays local


def _settings(s3: S3Settings | None = None) -> Settings:
    light = LightScanSettings(read_timeout_seconds=0.5, max_page_bytes=BIG_LIMIT)
    return Settings(light=light, s3=s3) if s3 else Settings(light=light)


@pytest.fixture
def scanner(
    shop_server: ShopServer, fixed_clock: FixedClock
) -> Iterator[tuple[LightScanner, VirtualTime]]:
    vt = VirtualTime()
    ctx = build_context(
        _settings(),
        clock=fixed_clock,
        worker_id="w-test",
        limiter=MemoryRateLimiter(now=lambda: vt.now),
        resolve=_loopback,
        allow_private=True,
        transport=LoopbackTransport(shop_server.port),
        sleep=vt.sleep,
        base_scheme="http",
    )
    yield LightScanner(ctx), vt
    asyncio.run(ctx.fetcher.aclose())


def _plan_for(session: Session, clock: FixedClock, hostname: str) -> ScanPlan:
    sync_reference(session, load_reference(), clock=clock)
    ingest(
        session,
        [SourceRecord(hostname, rank=100)],
        source=DomainSourceKind.TRANCO,
        origin="t",
        clock=clock,
    )
    planner.ensure_plans(session, ScanType.LIGHT, s=Settings().scan, clock=clock)
    host = session.execute(select(Host).where(Host.hostname == hostname)).scalar_one()
    plan = session.execute(
        select(ScanPlan).where(ScanPlan.host_id == host.id, ScanPlan.scan_type == ScanType.LIGHT)
    ).scalar_one()
    leased = queue.lease(
        session, ScanType.LIGHT, "w-test", limit=10, lease_seconds=600, clock=clock
    )
    assert plan in leased
    return plan


def _scan(scanner: LightScanner, session: Session, plan: ScanPlan) -> LightScanOutcome:
    return asyncio.run(scanner.scan(session, plan))


def test_woocommerce_shop_full_slice(
    db_session: Session,
    fixed_clock: FixedClock,
    scanner: tuple[LightScanner, VirtualTime],
    shop_server: ShopServer,
) -> None:
    sc, vt = scanner
    shop_server.requests.clear()
    plan = _plan_for(db_session, fixed_clock, "woo-shop.de")
    out = _scan(sc, db_session, plan)

    assert out.status == ScanStatus.OK and out.coverage == Coverage.CART
    assert [p.page_type for p in out.pages] == ["homepage", "product", "product", "cart"]
    paths = shop_server.paths("woo-shop.de")
    assert paths[0] == "/robots.txt" and paths[1] == "/"
    assert "/kasse/" not in paths and "/mein-konto/" not in paths  # robots + no checkout
    assert "/warenkorb/" in paths and sum(p.startswith("/product/") for p in paths) == 2
    # FR-SC-06: 1 rps per host - every request after the first waited ~1 s on the virtual clock
    assert len(vt.waits) >= len(paths) - 1 and all(0.99 <= w <= 1.01 for w in vt.waits)
    # detection: platform + providers from homepage/product/cart
    assert out.platform is not None and out.platform.target_id == "woocommerce"
    assert out.platform_version == "9.3.1"
    assert {p.target_id for p in out.providers} >= {"stripe", "paypal"}
    assert out.country is not None and out.country.country == "DE"
    assert out.classification is not None and out.classification.status == DomainStatus.ECOMMERCE
    # scripts deduplicated by sha256 (FR-LS-03): two local .js files, identical body → one asset
    assert len(out.scripts) == 2 and len(set(out.scripts.values())) == 1
    assert db_session.execute(select(JsAsset)).scalars().one().size_bytes == len(JS_BODY)
    assert len(db_session.execute(select(HostJsAsset)).scalars().all()) == 1
    # persistence
    run = db_session.get(ScanRun, out.scan_run_id)
    assert run is not None and run.status == ScanStatus.OK and run.coverage == Coverage.CART
    assert run.artifact_prefix == out.artifact_prefix and run.ruleset_version
    host = db_session.get(Host, out.host_id)
    assert host is not None
    profile = db_session.get(StoreProfile, host.id)
    assert profile is not None and profile.platform_id == "woocommerce"
    assert profile.country == "DE" and profile.currency == "EUR" and profile.traffic_rank == 100
    providers = {
        p.provider_id: p
        for p in db_session.execute(
            select(StoreProvider).where(StoreProvider.host_id == host.id)
        ).scalars()
    }
    assert {"stripe", "paypal"} <= providers.keys()
    assert all(not p.active_on_checkout for p in providers.values())
    domain = db_session.get(Domain, host.domain_id)
    assert domain is not None and domain.status == DomainStatus.ECOMMERCE
    assert domain.status_reason == "classifier" and domain.ecommerce_confidence is not None
    # FR-LS-06: external links fed back as crawl_link candidates
    assert out.new_link_candidates >= 1
    linked = {
        d.etld1
        for d in db_session.execute(
            select(Domain)
            .join(DomainSource)
            .where(DomainSource.source == DomainSourceKind.CRAWL_LINK)
        ).scalars()
    }
    assert linked == {"instagram.com"}  # partner-shop.test dropped: .test is not a PSL suffix
    # rescheduled, lease released
    db_session.refresh(plan)
    assert plan.locked_until is None and plan.next_scan_at == fixed_clock.now() + timedelta(days=7)
    # observations buffered for ClickHouse
    assert sc.ctx.buffer.pending >= 1 + len(out.providers)


def test_shopify_robots_blocks_cart(
    db_session: Session,
    fixed_clock: FixedClock,
    scanner: tuple[LightScanner, VirtualTime],
    shop_server: ShopServer,
) -> None:
    sc, _ = scanner
    shop_server.requests.clear()
    plan = _plan_for(db_session, fixed_clock, "shopify-shop.com")
    out = _scan(sc, db_session, plan)
    paths = shop_server.paths("shopify-shop.com")
    assert out.status == ScanStatus.OK
    assert "/cart" not in paths and "/checkout" not in paths
    assert "/products/tee" in paths  # allowed product page (404 here, still attempted once)
    assert out.coverage == Coverage.HOMEPAGE  # the product page did not render
    assert out.platform is not None and out.platform.target_id == "shopify"
    assert "paypal" in {p.target_id for p in out.providers}
    assert out.country is not None and out.country.country == "GB"


def test_parked_and_blog_set_domain_status(
    db_session: Session, fixed_clock: FixedClock, scanner: tuple[LightScanner, VirtualTime]
) -> None:
    sc, _ = scanner
    parked = _scan(sc, db_session, _plan_for(db_session, fixed_clock, "parked.de"))
    assert parked.status == ScanStatus.OK and parked.parked_by
    assert parked.domain_status == DomainStatus.PARKED and len(parked.pages) == 1
    blog = _scan(sc, db_session, _plan_for(db_session, fixed_clock, "blog.fr"))
    assert blog.status == ScanStatus.OK and blog.domain_status == DomainStatus.NOT_ECOMMERCE
    assert blog.platform is not None and blog.platform.target_id == "wordpress"
    assert blog.country is not None and blog.country.country == "FR"
    # a NOT_ECOMMERCE domain is rescheduled on the candidate cycle (30 d), not 7 d
    plan = db_session.execute(
        select(ScanPlan).join(Host).where(Host.hostname == "blog.fr")
    ).scalar_one()
    assert plan.next_scan_at == fixed_clock.now() + timedelta(days=30)


def test_artifacts_are_sanitised(
    db_session: Session,
    fixed_clock: FixedClock,
    shop_server: ShopServer,
    object_store: ObjectStore,
    s3_settings: S3Settings,
) -> None:
    settings = _settings(s3_settings)
    vt = VirtualTime()
    ctx = build_context(
        settings,
        clock=fixed_clock,
        store=object_store,
        limiter=MemoryRateLimiter(now=lambda: vt.now),
        resolve=_loopback,
        allow_private=True,
        transport=LoopbackTransport(shop_server.port),
        sleep=vt.sleep,
        base_scheme="http",
    )
    try:
        out = _scan(
            LightScanner(ctx), db_session, _plan_for(db_session, fixed_clock, "pii-shop.com")
        )
    finally:
        asyncio.run(ctx.fetcher.aclose())
    assert out.status == ScanStatus.OK
    bucket = settings.s3.bucket_artifacts
    manifest = json.loads(object_store.get_bytes(bucket, f"{out.artifact_prefix}manifest.json"))
    page = manifest["pages"][0]
    html = gzip.decompress(object_store.get_bytes(bucket, page["html_key"])).decode()
    assert "@" not in html and EMAIL_MASK in html and PHONE_MASK in html
    assert "7946" not in html and "415" not in html and "1234567" not in html
    assert "199.99" in html and "4006381333931" in html and "2026" in html  # not PII
    assert page["redacted"] == {"emails": 3, "phones": 5}  # incl. the tel: href
    assert "sales@" not in json.dumps(manifest)
    assert manifest["ruleset_version"] == ctx.ruleset.version
    assert page["headers"].get("set-cookie") is None  # cookies never stored


def test_errors_blocked_timeout_and_egress(
    db_session: Session,
    fixed_clock: FixedClock,
    scanner: tuple[LightScanner, VirtualTime],
    shop_server: ShopServer,
) -> None:
    sc, _ = scanner
    s = Settings().scan
    # 403 on the homepage → blocked, cooldown 30 d, no failure counted
    out = _scan(sc, db_session, _plan_for(db_session, fixed_clock, "forbidden.de"))
    assert out.status == ScanStatus.BLOCKED and out.stop_reason == "http_403"
    plan = db_session.execute(
        select(ScanPlan).join(Host).where(Host.hostname == "forbidden.de")
    ).scalar_one()
    assert plan.fail_count == 0 and plan.last_error == "http_403"
    assert plan.next_scan_at == fixed_clock.now() + timedelta(days=s.blocked_cooldown_days)
    run = db_session.get(ScanRun, out.scan_run_id)
    assert run is not None and run.status == ScanStatus.BLOCKED and run.artifact_prefix is None
    # read timeout (server sleeps 2 s, read timeout 0.5 s) → timeout, retry backoff 1 h
    out = _scan(sc, db_session, _plan_for(db_session, fixed_clock, "slow.de"))
    assert out.status == ScanStatus.TIMEOUT and out.stop_reason == "read_timeout"
    plan = db_session.execute(
        select(ScanPlan).join(Host).where(Host.hostname == "slow.de")
    ).scalar_one()
    assert plan.fail_count == 1 and plan.next_scan_at == fixed_clock.now() + timedelta(hours=1)
    # oversized homepage is cut at max_page_bytes (FR-LS-07) and still parsed
    out = _scan(sc, db_session, _plan_for(db_session, fixed_clock, "big.de"))
    assert out.status == ScanStatus.OK
    home = out.pages[0].fetch
    assert home.truncated and home.bytes_read > BIG_LIMIT >= len(home.body)
    # a redirect to a loopback target is refused by the egress guard with the production policy
    prod_ctx = build_context(
        _settings(),
        clock=fixed_clock,
        limiter=MemoryRateLimiter(now=lambda: 0.0),
        resolve=_public,
        allow_private=False,
        transport=LoopbackTransport(shop_server.port),
        sleep=sc.ctx.fetcher._sleep,
        base_scheme="http",
    )
    try:
        out = _scan(
            LightScanner(prod_ctx), db_session, _plan_for(db_session, fixed_clock, "bounce.de")
        )
    finally:
        asyncio.run(prod_ctx.fetcher.aclose())
    assert out.status == ScanStatus.ERROR and out.stop_reason is not None
    assert out.stop_reason.startswith("egress_blocked")
    assert ("bounce.de", "/") in shop_server.requests
    assert not any(path == "/admin" for _h, path in shop_server.requests)  # never followed


def test_run_batch_commits_and_flushes_to_clickhouse(
    fresh_database: str,
    fixed_clock: FixedClock,
    shop_server: ShopServer,
    ch_client: Client,
) -> None:
    """Real commits (one session per task), so this runs in a throw-away database."""
    alembic_command.upgrade(alembic_config_for(fresh_database), "head")
    engine = create_engine(fresh_database, future=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    names = ["woo-shop.de", "parked.de", "slow.de"]
    vt = VirtualTime()
    ctx = build_context(
        _settings(),
        clock=fixed_clock,
        worker_id="w-batch",
        ch_client=ch_client,
        limiter=MemoryRateLimiter(now=lambda: vt.now),
        resolve=_loopback,
        allow_private=True,
        transport=LoopbackTransport(shop_server.port),
        sleep=vt.sleep,
        base_scheme="http",
    )
    try:
        with factory() as setup:
            sync_reference(setup, load_reference(), clock=fixed_clock)
            ingest(
                setup,
                [SourceRecord(n, rank=i + 1) for i, n in enumerate(names)],
                source=DomainSourceKind.MANUAL,
                origin="batch",
                clock=fixed_clock,
            )
            planner.ensure_plans(setup, ScanType.LIGHT, s=Settings().scan, clock=fixed_clock)
            setup.commit()
        outcomes = asyncio.run(run_batch(factory, LightScanner(ctx), limit=10, concurrency=3))
        assert {o.etld1: o.status for o in outcomes} == {
            "woo-shop.de": ScanStatus.OK,
            "parked.de": ScanStatus.OK,
            "slow.de": ScanStatus.TIMEOUT,
        }
        assert ctx.buffer.pending == 0 and ctx.buffer.flushed_rows >= 3
        rows = ch_client.query(
            "SELECT etld1, status, platform_id FROM obs_scan ORDER BY etld1"
        ).result_rows
        assert [r[0] for r in rows] == sorted(names)
        assert {r[1] for r in rows} == {"ok", "timeout"}
        providers = ch_client.query(
            "SELECT DISTINCT provider_id FROM obs_provider WHERE etld1 = 'woo-shop.de'"
        ).result_rows
        assert {"stripe", "paypal"} <= {r[0] for r in providers}
        with factory() as check:
            runs = check.execute(select(ScanRun).where(ScanRun.worker_id == "w-batch")).scalars()
            assert len(list(runs)) == 3
            plans = check.execute(select(ScanPlan).join(Host).where(Host.hostname.in_(names)))
            assert all(p.locked_until is None for p in plans.scalars().all())
    finally:
        asyncio.run(ctx.fetcher.aclose())
        engine.dispose()
